"""`NotionWriter` — the one place the app writes to Notion (§2.3.4, FR-10).

Three properties, verified, or nothing: `Status`, `Done`, `Due Date` (CLAUDE.md constraint
6). The shape of this module follows from that.

**`Done` is derived here and nowhere else.** The Validator hands over a `status` and a
`due_date` and has no opinion on `Done` at all (§2.3.4); this module turns `Completed` into
`Done = true` and `Not started` / `In progress` into `Done = false`, in the *same* `PATCH`.
That replicates Notion's "Mark as done" button, which the API cannot invoke, and it is the
one deliberate deviation from "write exactly what was asked for". It happens only for a
database that has the property (`NOTION_*_HAS_DONE`) and only when `Status` is part of the
change, because letting the two drift apart would reintroduce the exact ambiguity
(`Status = Not started` on a finished item) that FR-3's `is_complete` exists to route around.

**A write is never reported as a success it cannot prove.** The pipeline is live-fetch → one
`PATCH` carrying only the changed properties → read-back → compare *every* changed property,
`Done` included. Anything else is a failure with an honest reason ("the change was NOT
made"), never a success claim (§2.3.4, Appendix B case 13). So `WriteOutcome` reports what
Notion actually holds afterwards, read back, rather than what was asked for — the
confirmation email can state the actual resulting values without anybody reading the page
again.

**No blind retries.** Exactly one retry on a *transient* failure (429/5xx), followed by the
usual re-verification; a 404 is terminal. The adapter's transport already retries transient
failures once per request (`client._request`), so this outer retry exists for the case where
the call died outright — and even then it ends in a read-back, never in an assumption.

The decision logic deliberately lives here and not in the adapter: `client.update_page`
assembles whatever it is handed and cannot invent a property, while *which* properties
changed, what `Done` becomes, and whether the read-back agreed are all decided in this file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast

from app.container import AppContainer
from app.domain.types import NotionPage, SourceDb
from app.integrations.notion.client import (
    NotionError,
    NotionNotFoundError,
    NotionTransientError,
)
from app.integrations.notion.normalize import normalize_page
from app.logging import get_logger

# What one `apply` call produced. `verified` is the only outcome that means "the change is in
# Notion"; `no_change` means there was nothing to write. `page_not_found` is kept separate
# from `failed` because "the item is gone" is a different thing to tell the owner than
# "Notion would not apply it".
WriteResult = Literal["verified", "no_change", "page_not_found", "failed"]


@dataclass(frozen=True, slots=True)
class NotionValues:
    """The three writable properties as one live page holds them.

    Read through `normalize_page`, so a value here is the same value the sync pipeline would
    store for that page: the `Not started` fallback for a missing or empty `Status`, and
    `False` for a database with no `Done` property at all.
    """

    status: str
    done: bool
    due_date: date | None


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Everything the caller needs for a confirmation, a no-op reply, or a failure email.

    `before` and `after` are reads, not intentions:

    * `after` is what the **read-back** returned. It is None exactly when no read-back
      happened — the page was gone, or an error ended the attempt first — so a caller that
      sees `after is None` knows it is quoting a failure rather than a result.
    * On `no_change`, `after` equals `before`: nothing was written, and the values a reply
      would state are the unchanged ones.

    `reason` is present on every failure and is written to be shown to the owner verbatim:
    it either names the property that disagreed or the error that ended the attempt.
    """

    result: WriteResult
    page_id: str
    source_db: str
    before: NotionValues | None = None
    after: NotionValues | None = None
    reason: str | None = None
    wrote_done: bool = False
    due_date_was_range: bool = False
    attempts: int = 0

    @property
    def succeeded(self) -> bool:
        """The write was applied **and** the read-back agreed on every changed property."""
        return self.result == "verified"

    @property
    def no_change(self) -> bool:
        """The requested values were already set, so nothing was written at all."""
        return self.result == "no_change"

    @property
    def failed(self) -> bool:
        """Neither verified nor a no-op — the §2.3.4 failure email's case."""
        return self.result not in ("verified", "no_change")

    @property
    def page_missing(self) -> bool:
        """The page could not be read at all: gone, or invisible to this token (404)."""
        return self.result == "page_not_found"

    @property
    def retried(self) -> bool:
        """Whether the single permitted retry on a transient failure was spent."""
        return self.attempts > 1


class NotionWriter:
    """Live-fetch, one `PATCH`, read-back — the §2.3.4 write pipeline."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._log = get_logger(__name__)

    async def apply(
        self,
        *,
        page_id: str,
        source_db: str,
        status: str | None,
        due_date: date | None,
    ) -> WriteOutcome:
        """Apply one validated change and report what Notion actually holds afterwards.

        `status` and `due_date` are each None when that part of the item is not part of this
        change — the Validator's `ApplyChange` carries both fields optionally (§2.3.4). There
        is no way to *clear* a due date: this app never does, and the port's `None` means
        "unchanged" (see `client.update_page`).

        Never raises for a Notion failure: every one of them comes back as a `WriteOutcome`
        whose `reason` is fit to show the owner. An exception that is not a `NotionError` is
        a bug and is left to propagate.
        """
        settings = self._container.settings

        try:
            page = await self._container.notion.get_page(page_id)
        except NotionError as exc:
            # A read that failed is *not* reported as "the page is gone": the two mean
            # different things and the owner should not be told the item was deleted.
            return self._failure(page_id, source_db, f"could not read the page: {_describe(exc)}")

        if page is None:
            self._log.warning("notion_update_page_missing", page_id=page_id)
            return WriteOutcome(
                result="page_not_found",
                page_id=page_id,
                source_db=source_db,
                reason=(
                    f"page {page_id} is not visible in Notion — it was deleted, or it no "
                    f"longer exists in this workspace"
                ),
            )

        before = self._read(page, source_db)
        status_changed = status is not None and status != before.status
        due_changed = due_date is not None and due_date != before.due_date
        if not status_changed and not due_changed:
            # Nothing to write, so nothing is written and no `PATCH` is attempted — which is
            # also what keeps an empty `properties` object from ever being sent.
            self._log.info("notion_update_no_change", page_id=page_id)
            return WriteOutcome(
                result="no_change",
                page_id=page_id,
                source_db=source_db,
                before=before,
                after=before,
                reason="the requested values are already set",
            )

        # `Done` mirrors `Status`, but only for a database that has the property, and only
        # when `Status` is part of this change (§2.3.4). Any status that is not the configured
        # completed value is an active one — the Validator restricts `status` to the allowed
        # set, so "not completed" is exactly "done = false".
        wrote_done = status_changed and settings.has_done_property(source_db)
        done_value = (status == settings.notion_status_completed) if wrote_done else None
        due_date_was_range = due_changed and _due_date_is_range(page, settings.notion_prop_due)

        attempts, error = await self._patch(
            page_id=page_id,
            status_name=status if status_changed else None,
            done_property=settings.notion_prop_done if wrote_done else None,
            done_value=done_value,
            due_date=due_date if due_changed else None,
        )
        if error is not None:
            self._log.warning("notion_update_failed", page_id=page_id, reason=error)
            return WriteOutcome(
                result="failed",
                page_id=page_id,
                source_db=source_db,
                before=before,
                reason=error,
                wrote_done=wrote_done,
                due_date_was_range=due_date_was_range,
                attempts=attempts,
            )

        try:
            after_page = await self._container.notion.get_page(page_id)
        except NotionError as exc:
            return self._failure(
                page_id,
                source_db,
                f"the change was sent to Notion but could not be verified: {_describe(exc)}",
                before=before,
                wrote_done=wrote_done,
                due_date_was_range=due_date_was_range,
                attempts=attempts,
            )
        if after_page is None:
            return self._failure(
                page_id,
                source_db,
                "the change was sent to Notion but the page could not be re-read to verify it",
                before=before,
                wrote_done=wrote_done,
                due_date_was_range=due_date_was_range,
                attempts=attempts,
            )

        after = self._read(after_page, source_db)
        mismatches = _mismatches(
            after=after,
            status=status if status_changed else None,
            done=done_value if wrote_done else None,
            due_date=due_date if due_changed else None,
            prop_status=settings.notion_prop_status,
            prop_done=settings.notion_prop_done,
            prop_due=settings.notion_prop_due,
        )
        if mismatches:
            # Appendix B case 13: Notion accepted the PATCH and the page disagrees with it.
            # Claiming success here would be the single failure this whole pipeline exists to
            # prevent, so it is reported as a failure — with the values that were actually
            # read back, which is what makes the report checkable by hand.
            reason = "Notion did not apply the change: " + "; ".join(mismatches)
            self._log.warning("notion_update_failed", page_id=page_id, reason=reason)
            return WriteOutcome(
                result="failed",
                page_id=page_id,
                source_db=source_db,
                before=before,
                after=after,
                reason=reason,
                wrote_done=wrote_done,
                due_date_was_range=due_date_was_range,
                attempts=attempts,
            )

        self._log.info(
            "notion_update_verified",
            page_id=page_id,
            status=after.status,
            done=after.done,
            due_date=after.due_date.isoformat() if after.due_date else None,
            attempts=attempts,
        )
        return WriteOutcome(
            result="verified",
            page_id=page_id,
            source_db=source_db,
            before=before,
            after=after,
            wrote_done=wrote_done,
            due_date_was_range=due_date_was_range,
            attempts=attempts,
        )

    # ── Pieces ──────────────────────────────────────────────────────────────

    async def _patch(
        self,
        *,
        page_id: str,
        status_name: str | None,
        done_property: str | None,
        done_value: bool | None,
        due_date: date | None,
    ) -> tuple[int, str | None]:
        """Send the one `PATCH`, with the single retry §2.3.4 allows.

        Returns `(attempts, error)`, where `error is None` means Notion accepted the request.
        Accepting is not the same as applying it — that is what the read-back is for, and it
        is why this method deliberately stops at "the request was taken".
        """
        attempts = 1
        try:
            await self._update_once(
                page_id=page_id,
                status_name=status_name,
                done_property=done_property,
                done_value=done_value,
                due_date=due_date,
            )
        except NotionNotFoundError as exc:
            # Terminal. The page is gone or invisible, and a second identical request cannot
            # change that, so this is the one failure that is never retried.
            return attempts, f"Notion answered 404 for the write: {_describe(exc)}"
        except NotionTransientError as exc:
            first = _describe(exc)
            attempts += 1
            try:
                await self._update_once(
                    page_id=page_id,
                    status_name=status_name,
                    done_property=done_property,
                    done_value=done_value,
                    due_date=due_date,
                )
            except NotionError as retry_exc:
                return attempts, (
                    f"Notion failed the write twice "
                    f"(first attempt: {first}; retry: {_describe(retry_exc)})"
                )
        except NotionError as exc:
            return attempts, f"Notion rejected the write: {_describe(exc)}"
        return attempts, None

    async def _update_once(
        self,
        *,
        page_id: str,
        status_name: str | None,
        done_property: str | None,
        done_value: bool | None,
        due_date: date | None,
    ) -> None:
        """One call to the port. The adapter builds the body from the non-`None` arguments.

        Keyword arguments, not positional: the point of this seam is that a property the
        caller did not ask for is passed as `None`, and a mistake in argument *order* would
        silently write the wrong property instead of failing.
        """
        settings = self._container.settings
        await self._container.notion.update_page(
            page_id=page_id,
            status_property=settings.notion_prop_status,
            status_name=status_name,
            done_property=done_property,
            done_value=done_value,
            due_property=settings.notion_prop_due,
            due_date=due_date,
        )

    def _read(self, page: NotionPage, source_db: str) -> NotionValues:
        """The three writable properties of a live page, through the normalizer's readers.

        `normalize_page` rather than hand-parsed property JSON: a value read by the write
        path and a value read by the sync path must not be able to disagree, and the
        normalizer already owns that reading — including the `Not started` fallback.
        """
        settings = self._container.settings
        item = normalize_page(
            page,
            source_db=cast(SourceDb, source_db),
            tz=settings.tz,
            default_due_time=settings.default_due_time,
            prop_course=settings.notion_prop_course,
            prop_type=settings.notion_prop_type,
            prop_due=settings.notion_prop_due,
            prop_status=settings.notion_prop_status,
            prop_done=settings.notion_prop_done,
        )
        return NotionValues(status=item.status, done=item.done, due_date=item.due_date)

    def _failure(
        self,
        page_id: str,
        source_db: str,
        reason: str,
        *,
        before: NotionValues | None = None,
        wrote_done: bool = False,
        due_date_was_range: bool = False,
        attempts: int = 0,
    ) -> WriteOutcome:
        """A failure, logged once, so the caller only has to deliver the reason."""
        self._log.warning("notion_update_failed", page_id=page_id, reason=reason)
        return WriteOutcome(
            result="failed",
            page_id=page_id,
            source_db=source_db,
            before=before,
            reason=reason,
            wrote_done=wrote_done,
            due_date_was_range=due_date_was_range,
            attempts=attempts,
        )


# ── Pure helpers ────────────────────────────────────────────────────────────


def _mismatches(
    *,
    after: NotionValues,
    status: str | None,
    done: bool | None,
    due_date: date | None,
    prop_status: str,
    prop_done: str,
    prop_due: str,
) -> list[str]:
    """One readable line per changed property whose read-back value disagrees.

    A `None` here means that property was not part of the write, so it is not checked: this
    only ever reports on what was asked for, never on what the item happened to look like.
    """
    problems: list[str] = []
    if status is not None and after.status != status:
        problems.append(f"{prop_status} is {after.status!r}, not {status!r}")
    if done is not None and after.done != done:
        problems.append(f"{prop_done} is {after.done}, not {done}")
    if due_date is not None and after.due_date != due_date:
        problems.append(f"{prop_due} is {after.due_date}, not {due_date}")
    return problems


def _due_date_is_range(page: NotionPage, prop_name: str) -> bool:
    """Whether the page's due date is a Notion *range* — its `end` is set.

    `normalize` exposes only `start`, because the rest of the app has no use for a range.
    The writer does: §2.3.4 requires the audit log to note when a range's `start` was moved
    while its `end` was left alone, so the caller has to be told it happened.
    """
    entry = page.properties.get(prop_name)
    if not isinstance(entry, Mapping):
        return False
    value = entry.get("date")
    if not isinstance(value, Mapping):
        return False
    end = value.get("end")
    return isinstance(end, str) and bool(end.strip())


def _describe(exc: BaseException) -> str:
    """An exception as one short, non-secret line fit for an owner-facing reason."""
    return f"{type(exc).__name__}: {exc}"
