"""`ReminderService` — the scheduler tick, the send, and stale-claim recovery (§2.3.2.C/D).

This module is a transliteration of §2.3.2.C's loop, and its whole job is to keep three
promises that are easy to state and easy to break:

**Exactly once (FR-7).** The claim itself is the only place a reminder's fate begins:
`claim_next` moves one row `pending -> claimed` under `FOR UPDATE SKIP LOCKED` and bumps
`attempt_count`, so two workers (or one worker run twice) can never both own a row. Nothing
here re-implements that; it consumes the repository's claim and never selects a reminder by
hand.

**Nothing is sent once it is due (FR-4), and nothing is sent for an item that is done or
gone (FR-3, FR-9).** Four guards run *before* any email, in the order below, and each one
continues the loop rather than aborting the tick — one stale row must not stop the rest.

**A reminder is never silently lost.** A send failure backs off and retries; a crash between
claim and send is resolved by `recover_stale_claims`, which asks Gmail what actually
happened instead of guessing. The design prefers a duplicate email over a missing one, and
the failure alert after the last attempt is what makes a genuinely undeliverable reminder
visible rather than forgotten.

Two deliberate choices worth knowing before editing:

* **The attempt budget is checked before the send, not after the failure.** The budget
  counts *claims* (`claim_next` increments on the claim, so a crash between claim and send
  still spends one), which means the fifth claim is the one with no attempt left. Going
  terminal there — rather than spending the fifth claim on a send that would be the sixth
  attempt — is what keeps the ladder at four real sends, which is what the 30-second base is
  sized for: 30s, 60s, 120s, 240s + one final claim to fail is under ten minutes, so the "sent
  within about 5 minutes of its target" criterion is not blown open by a Gmail blip.
* **The outbound cap is checked before the network, not before the send.** A capped tick
  stops sending immediately, and the claimed reminder is returned to `pending` with
  `next_attempt_at = now`, so it is picked up by the very next tick. `release_with_backoff`
  is the honest call for that: it is the only repository operation that returns a claimed row
  to `pending` without ending its life. It leaves `last_error` explaining why, and it does
  not touch `attempt_count` — the cap is an operator kill switch, not a send failure, so it
  must not walk a reminder toward `failed`.

Everything that reads the time reads it from the injected `Clock` (CLAUDE.md constraint 4),
and every instant is timezone-aware. The count stored against the cap is keyed on the
**UTC** hour, so an hour boundary is the same instant for every process regardless of the
configured zone.
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.container import AppContainer
from app.db.models import Item
from app.db.repositories import audit as audit_repo
from app.db.repositories import items as items_repo
from app.db.repositories import reminders as reminders_repo
from app.db.repositories import state as state_repo
from app.db.repositories import threads as threads_repo
from app.domain.planner import PlannerPolicy
from app.domain.types import (
    ItemKind,
    NormalizedItem,
    ReminderType,
    SentMessage,
    SkipReason,
    SourceDb,
)
from app.integrations.gmail.compose import render_reminder
from app.integrations.notion.normalize import normalize_page
from app.logging import get_logger
from app.services.alert_service import AlertService

# The whole attempt budget, counted in claims (§2.3.2.C: "after 5 attempts -> failed").
MAX_SEND_ATTEMPTS = 5

# 30s, doubling, capped at 10 minutes, ±20% jitter. The base is deliberately small: five
# claims at 30/60/120/240s stay inside the "about 5 minutes" success criterion, whereas a
# conventional 1-minute base would not.
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 600.0
BACKOFF_JITTER = 0.2

# A claim held longer than this was interrupted between the claim and its outcome. §2.3.2.C
# resolves each one against Gmail rather than assuming either way.
STALE_CLAIM_AFTER = timedelta(minutes=10)

# `system_state` key prefix for the per-UTC-hour outbound count (§2.3.2.D).
OUTBOUND_COUNTER_PREFIX = "outbound_sends:"

# Recorded in `last_error` when the cap is what stopped the send, so the reason is legible
# on the row itself and not only in an alert email.
_OUTBOUND_CAP_ERROR = "outbound cap reached (MAX_OUTBOUND_EMAILS_PER_HOUR); not sent"
_STALE_CLAIM_NOTHING_SENT = "stale claim: no sent message carried this reminder's ref_token"


def outbound_counter_key(now: datetime) -> str:
    """The `system_state` key holding this UTC hour's outbound send count.

    UTC rather than the configured zone: the counter is an operator safety limit, and an
    hour boundary that moves with a DST change would make the cap's meaning drift twice a
    year. Public because the admin surface and the tests both read the same key.
    """
    return f"{OUTBOUND_COUNTER_PREFIX}{now.astimezone(UTC).strftime('%Y-%m-%dT%H')}"


def backoff_delay(attempt: int) -> timedelta:
    """`min(30s * 2**(attempt-1), 10min)` with ±20% jitter, for a 1-based `attempt`.

    `attempt` is the claim's `attempt_count`, which is already incremented: the first
    failure is attempt 1 -> ~30s, then ~60s, ~120s, ~240s. The jitter exists so a Gmail
    outage does not make every pending reminder retry in lockstep against the same API.

    Jitter cannot reorder the ladder: 1.2 x 30s (36s) is still less than 0.8 x 60s (48s),
    so a later attempt's delay is always strictly longer than an earlier one's.
    """
    base = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
    return timedelta(seconds=base * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER))


class _Outcome(enum.Enum):
    """What one claimed reminder's handling means for the rest of the tick."""

    SENT = "sent"  # an email went out
    DONE = "done"  # handled without sending (skipped, deferred, or terminally failed)
    STOP = "stop"  # the outbound cap is reached: stop sending for this tick


@dataclass(frozen=True, slots=True)
class _Claim:
    """One claimed reminder, copied out of the ORM before its session closes.

    The row is read once, in the session that claimed it, and nothing after that touches a
    detached entity — later phases open their own short sessions, so no connection is held
    across a Notion or Gmail round trip.
    """

    id: UUID
    item_id: UUID
    reminder_type: str
    due_at_snapshot: datetime
    target_at: datetime
    ref_token: str
    attempt_count: int


class ReminderService:
    """One scheduler pass: claim, guard, check, send, record (§2.3.2.C)."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._alerts = AlertService(container)
        self._log = get_logger(__name__)

    # ── The tick ────────────────────────────────────────────────────────────

    async def tick(self) -> int:
        """Handle every reminder that is due right now. Returns how many were sent.

        Claims one row at a time and keeps going until `claim_next` says there is nothing
        left — including after a skip, so a single completed item cannot stall the queue.
        The one early exit is the outbound cap, where continuing would only repeat the same
        refusal.

        The caller is responsible for the advisory lock (`app.jobs.run_job_once`), so this
        does not defend itself against a concurrent tick: a second instance's claim is
        skipped by `FOR UPDATE SKIP LOCKED`, which is the correct answer anyway.
        """
        sent = 0
        while True:
            now = self._container.clock.now()
            claim = await self._claim_next(now)
            if claim is None:
                break
            outcome = await self._process(claim, now)
            if outcome is _Outcome.SENT:
                sent += 1
            elif outcome is _Outcome.STOP:
                self._log.info("reminder_tick_stopped_by_cap", sent=sent)
                break
        return sent

    async def _claim_next(self, now: datetime) -> _Claim | None:
        """Claim the single most-due reminder, committing the claim before any I/O.

        Committing immediately is the point: the claim is the record that this row is in
        flight, and it must survive a crash in the middle of the Notion or Gmail call that
        follows. That is exactly the state `recover_stale_claims` exists to resolve.
        """
        async with self._container.session_factory() as session:
            row = await reminders_repo.claim_next(session, now)
            await session.commit()
        if row is None:
            return None
        return _Claim(
            id=row.id,
            item_id=row.item_id,
            reminder_type=row.reminder_type,
            due_at_snapshot=row.due_at_snapshot,
            target_at=row.target_at,
            ref_token=row.ref_token,
            attempt_count=row.attempt_count,
        )

    async def _process(self, claim: _Claim, now: datetime) -> _Outcome:
        """Run one claimed reminder through the guards, the pre-send check, and the send.

        `now` is fixed for the whole reminder: the guards must not be able to disagree with
        each other because a few milliseconds passed between them, and a frozen clock in a
        test must produce one answer.
        """
        # ── Guards 1-3: the local row decides, with no network at all. ──────
        async with self._container.session_factory() as session:
            item = await items_repo.get_by_id(session, claim.item_id)
            if item is None:
                # Unreachable through the foreign key (ON DELETE CASCADE), and therefore a
                # bug worth a log line rather than an exception: nothing is sent.
                self._log.warning("reminder_item_missing", reminder_id=str(claim.id))
                await self._skip(session, claim, "item_inactive", now, notion_page_id=None)
                return _Outcome.DONE

            # Guard 1 — done or gone (FR-3, FR-9). Inactive is checked first so the reason
            # matches what `reconcile_reminders` would write for the same row (§2.3.2.B
            # reports completion as inactivity when both are true).
            if not item.is_active:
                await self._skip(session, claim, "item_inactive", now, item.notion_page_id)
                return _Outcome.DONE
            if item.done or item.status == self._completed_status:
                await self._skip(session, claim, "item_completed", now, item.notion_page_id)
                return _Outcome.DONE

            # Guard 2 — never send once due (FR-4). A missing due date is treated the same
            # way: there is no instant at which this reminder is still timely.
            if item.due_at is None or now >= item.due_at:
                await self._skip(session, claim, "past_due", now, item.notion_page_id)
                return _Outcome.DONE

            # Guard 3 — a later reminder for the same item *and due date* is also ready, so
            # this one is superseded by it (§2.3.2.C). This is what stops two emails for one
            # item arriving seconds apart after the process was down past two targets.
            if await reminders_repo.has_later_claimable(
                session,
                reminder_id=claim.id,
                item_id=claim.item_id,
                due_at_snapshot=claim.due_at_snapshot,
                target_at=claim.target_at,
                now=now,
            ):
                await self._skip(session, claim, "superseded_by_later", now, item.notion_page_id)
                return _Outcome.DONE

            snapshot = _as_normalized(item)

        # ── Guard 4: the attempt budget. Checked before the send. ───────────
        #
        # The budget counts *claims*, and `claim_next` has just spent this one, so
        # `>= MAX_SEND_ATTEMPTS` means all four send attempts that came before it failed and
        # this claim has none left. Going terminal here — rather than spending the last
        # claim on a fifth send — is what makes the ladder four real attempts, which is the
        # width the 30-second base was sized for.
        if claim.attempt_count >= MAX_SEND_ATTEMPTS:
            await self._fail(
                claim,
                snapshot,
                error=(
                    f"send failed on all {MAX_SEND_ATTEMPTS - 1} attempts; "
                    f"claim {claim.attempt_count} exceeded the budget"
                ),
            )
            return _Outcome.DONE

        # ── Guard 5: the global outbound cap (FR-13, §2.3.2.D). ─────────────
        if await self._cap_reached(now):
            await self._hold_for_cap(claim, now)
            await self._alert_safely(
                "outbound_cap_exceeded",
                "Outbound email cap reached",
                (
                    f"MAX_OUTBOUND_EMAILS_PER_HOUR "
                    f"({self._container.settings.max_outbound_emails_per_hour}) was reached, so "
                    f"the reminder scheduler has stopped sending for this tick.\n\n"
                    f"The claimed reminder ({claim.reminder_type}, reminder "
                    f"{claim.id}) was returned to `pending` and will be retried on the next "
                    f"tick. No reminder has been lost, and nothing will be sent late: FR-4 "
                    f"still suppresses anything whose due time has passed.\n\n"
                    f"If this was not expected, the cap is a kill switch worth checking "
                    f"before it is raised."
                ),
            )
            return _Outcome.STOP

        # ── Guard 6: the best-effort live check. ────────────────────────────
        if await self._presend_changed(claim, snapshot, now):
            return _Outcome.DONE

        # ── Send. One item, one new thread (FR-6). ──────────────────────────
        settings = self._container.settings
        subject, body = render_reminder(
            snapshot,
            cast(ReminderType, claim.reminder_type),
            settings.tz,
            claim.ref_token,
        )
        try:
            sent = await self._container.mail.send(
                to=settings.reminder_recipient,
                subject=subject,
                body=body,
                # A new thread per reminder: the V1 reply pipeline maps a reply back to its
                # item through this thread (§2.3.2.E step 3a).
                thread_id=None,
                in_reply_to=None,
            )
        except Exception as exc:
            # Any failure — auth, quota, transport — is retried rather than classified.
            # Guessing which exceptions are "permanent" here would be the difference
            # between a retry and a lost reminder.
            await self._send_failed(claim, now, exc)
            return _Outcome.DONE

        await self._record_sent(claim, snapshot, now, sent, subject)
        return _Outcome.SENT

    # ── Outcomes ────────────────────────────────────────────────────────────

    async def _skip(
        self,
        session: AsyncSession,
        claim: _Claim,
        reason: SkipReason,
        now: datetime,
        notion_page_id: str | None,
    ) -> None:
        """Cancel one claimed reminder with a reason, and record that it was cancelled.

        The audit payload mirrors the planner's `reminder_skipped` shape and adds
        `source: scheduler`, so an auditor can tell a decision the scheduler made at send
        time from one `reconcile_reminders` made during a sync. The row's own `skip_reason`
        carries the same reason; §2.3.5 has no resurrection event, so a later reconcile that
        flips this row back to `pending` deliberately writes nothing (see `reminders.py`).
        """
        await reminders_repo.mark_skipped(session, claim.id, skip_reason=reason, now=now)
        await audit_repo.append(
            session,
            "reminder_skipped",
            item_id=claim.item_id,
            notion_page_id=notion_page_id,
            payload={
                "reminder_id": str(claim.id),
                "reminder_type": claim.reminder_type,
                "skip_reason": reason,
                "source": "scheduler",
            },
        )
        await session.commit()
        self._log.info(
            "reminder_skipped",
            reminder_id=str(claim.id),
            reminder_type=claim.reminder_type,
            skip_reason=reason,
        )

    async def _send_failed(self, claim: _Claim, now: datetime, exc: BaseException) -> None:
        """Put a reminder whose send failed back in the queue, no sooner than its backoff.

        No audit row is written here, and that is a deliberate reading of §2.3.5: the
        vocabulary's only send-failure event, `reminder_failed`, means *terminal* failure,
        and a retry that succeeds must not have claimed it failed. The attempt is recorded
        where it belongs — `last_error`, `attempt_count`, `next_attempt_at` on the row —
        and the send that eventually succeeds writes `reminder_sent`.
        """
        error = f"{type(exc).__name__}: {exc}"
        delay = backoff_delay(claim.attempt_count)
        async with self._container.session_factory() as session:
            await reminders_repo.release_with_backoff(
                session, claim.id, next_attempt_at=now + delay, error=error
            )
            await session.commit()
        self._log.warning(
            "reminder_send_retrying",
            reminder_id=str(claim.id),
            attempt=claim.attempt_count,
            retry_in_seconds=round(delay.total_seconds(), 1),
            error=error,
        )

    async def _fail(self, claim: _Claim, snapshot: NormalizedItem, *, error: str) -> None:
        """Terminal failure: the attempt budget is spent. Audited and alerted (§2.3.2.C).

        No `now` is taken: `mark_failed` deliberately leaves `updated_at` to the database,
        and the alert describes the reminder's *target* rather than the instant the decision
        was made. Nothing here decides anything from the current time, so nothing here needs
        to read the clock.
        """
        async with self._container.session_factory() as session:
            await reminders_repo.mark_failed(session, claim.id, error=error)
            await audit_repo.append(
                session,
                "reminder_failed",
                item_id=claim.item_id,
                notion_page_id=snapshot.notion_page_id,
                payload={
                    "reminder_id": str(claim.id),
                    "reminder_type": claim.reminder_type,
                    "attempt_count": claim.attempt_count,
                    "target_at": claim.target_at.astimezone(UTC).isoformat(),
                },
                error=error,
            )
            await session.commit()
        self._log.error("reminder_failed", reminder_id=str(claim.id), error=error)
        await self._alert_safely(
            "reminder_send_failed",
            f"Reminder could not be sent: {snapshot.name}",
            (
                f"{snapshot.name} ({claim.reminder_type}) did not send after "
                f"{claim.attempt_count} claims, so it is now marked `failed`. It will not be "
                f"retried automatically.\n\nLast error: {error}\n\n"
                f"Item: {snapshot.notion_page_id}\n"
                f"Target: {claim.target_at.astimezone(self._container.settings.tz):%Y-%m-%d %H:%M}"
                f" {self._container.settings.timezone}"
            ),
        )

    async def _record_sent(
        self,
        claim: _Claim,
        snapshot: NormalizedItem,
        now: datetime,
        sent: SentMessage,
        subject: str,
    ) -> None:
        """Commit everything a successful send means, in one transaction.

        One commit, deliberately: the reminder's `sent` state, the thread that makes a reply
        answerable, the outbound record, the audit entry, and the cap counter are one fact —
        "this email exists". Splitting them would leave a crash able to produce a sent
        reminder with no thread (unanswerable) or a counted send with no record.
        """
        async with self._container.session_factory() as session:
            await reminders_repo.mark_sent(
                session,
                claim.id,
                now=now,
                provider_message_id=sent.provider_message_id,
                provider_thread_id=sent.provider_thread_id,
            )
            thread = await threads_repo.create_thread(
                session,
                item_id=claim.item_id,
                reminder_id=claim.id,
                provider_thread_id=sent.provider_thread_id,
                subject=subject,
                root_rfc_message_id=sent.rfc_message_id,
            )
            await threads_repo.add_outbound(
                session,
                thread_id=thread.id,
                kind="reminder",
                provider_message_id=sent.provider_message_id,
                rfc_message_id=sent.rfc_message_id,
            )
            await audit_repo.append(
                session,
                "reminder_sent",
                item_id=claim.item_id,
                notion_page_id=snapshot.notion_page_id,
                provider_message_id=sent.provider_message_id,
                provider_thread_id=sent.provider_thread_id,
                payload={
                    "reminder_id": str(claim.id),
                    "reminder_type": claim.reminder_type,
                    "attempt_count": claim.attempt_count,
                    "target_at": claim.target_at.astimezone(UTC).isoformat(),
                },
            )
            await self._bump_outbound_counter(session, now)
            await session.commit()
        self._log.info(
            "reminder_sent",
            reminder_id=str(claim.id),
            reminder_type=claim.reminder_type,
            provider_message_id=sent.provider_message_id,
        )

    # ── The outbound cap ────────────────────────────────────────────────────

    async def _cap_reached(self, now: datetime) -> bool:
        """Whether this UTC hour has already sent `MAX_OUTBOUND_EMAILS_PER_HOUR` emails.

        Only reminder sends are counted (see `_bump_outbound_counter`), so an alert can
        never be the thing that trips the kill switch and then be silenced by it.
        """
        key = outbound_counter_key(now)
        async with self._container.session_factory() as session:
            stored = await state_repo.get_json(session, key, 0)
        count = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
        cap = self._container.settings.max_outbound_emails_per_hour
        if count >= cap:
            self._log.warning("outbound_cap_reached", count=count, cap=cap, key=key)
            return True
        return False

    async def _hold_for_cap(self, claim: _Claim, now: datetime) -> None:
        """Return the capped reminder to `pending` so the next tick can send it.

        `release_with_backoff` with `next_attempt_at = now` is the honest call: it is the
        only repository operation that puts a claimed row back in the queue, it clears
        `claimed_at` (so the stale-claim sweep does not look at a row that is simply
        waiting), and it does not touch `attempt_count` — a capped reminder has not failed
        to send, it has not been tried.

        `last_error` is set to the cap message, which is the one thing this does overwrite:
        the row now carries the most recent reason it was not sent, and the attempt count
        and backoff history are what preserve the earlier one.
        """
        async with self._container.session_factory() as session:
            await reminders_repo.release_with_backoff(
                session, claim.id, next_attempt_at=now, error=_OUTBOUND_CAP_ERROR
            )
            await session.commit()

    async def _bump_outbound_counter(self, session: AsyncSession, now: datetime) -> None:
        """Increment this UTC hour's send count, in the caller's transaction.

        Counted here and nowhere else: an alert is an outbound email too, but counting it
        would let a saturated hour suppress the alert that reports the saturation. The
        counter is advisory — the real control is that `tick` refuses to send at or above
        the cap — so a crash between the send and this commit can under-count by one, never
        over-count, and never lose a reminder.
        """
        key = outbound_counter_key(now)
        stored = await state_repo.get_json(session, key, 0)
        count = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
        await state_repo.set_json(session, key, count + 1)

    # ── Pre-send check (best effort) ────────────────────────────────────────

    async def _presend_changed(
        self, claim: _Claim, snapshot: NormalizedItem, now: datetime
    ) -> bool:
        """Fetch the live page and report whether the local item is now out of date.

        True means "do not send": either the page is gone, or it is done/completed, or its
        due date moved. In each case the local row is brought up to date and
        `reconcile_reminders` is run — which is also what cancels this claimed reminder
        (§2.3.2.B step 1), so no separate skip is written here.

        False means "proceed", and it is deliberately also the answer when Notion cannot be
        reached at all. The check is *best effort* by specification: local data is at most an
        hour stale, and a Notion outage must never silently swallow a reminder. The outage is
        audited as `presend_check_skipped` so the degraded decision is visible afterwards.
        """
        try:
            page = await self._container.notion.get_page(snapshot.notion_page_id)
        except Exception as exc:
            await self._audit_presend_skipped(claim, snapshot, exc)
            return False

        if page is None:
            # The page is gone (404): FR-9. There is nothing to re-upsert, so the local item
            # is deactivated and reconciled; the planner then skips this reminder as
            # `item_inactive` and any still-future sibling with the same reason.
            await self._deactivate_local(snapshot, now)
            return True

        settings = self._container.settings
        live = normalize_page(
            page,
            source_db=snapshot.source_db,
            tz=settings.tz,
            default_due_time=settings.default_due_time,
            prop_course=settings.notion_prop_course,
            prop_type=settings.notion_prop_type,
            prop_due=settings.notion_prop_due,
            prop_status=settings.notion_prop_status,
            prop_done=settings.notion_prop_done,
        )
        # `normalize_page` cannot resolve the Course relation's display name (that is the
        # sync service's job, via the courses cache), so the stored name is carried over
        # whenever the relation itself is unchanged. Otherwise a pre-send check would erase
        # a course name that the hourly sync would only restore later.
        if live.course_page_id == snapshot.course_page_id:
            live = replace(live, course=snapshot.course)

        if not (
            live.in_trash
            or live.is_complete(self._completed_status)
            or live.due_at != snapshot.due_at
        ):
            return False

        async with self._container.session_factory() as session:
            stored = await items_repo.upsert(session, live)
            await reminders_repo.reconcile_reminders(session, stored, now, self._policy)
            await session.commit()
        self._log.info(
            "reminder_suppressed_by_presend_check",
            reminder_id=str(claim.id),
            notion_page_id=snapshot.notion_page_id,
        )
        return True

    async def _deactivate_local(self, snapshot: NormalizedItem, now: datetime) -> None:
        """Mark a page-less item inactive locally and reconcile its reminders (FR-9)."""
        async with self._container.session_factory() as session:
            item = await items_repo.get_by_page_id(session, snapshot.notion_page_id)
            if item is not None:
                # `deactivate` is guarded on `is_active`, so a page that has already been
                # deactivated by the daily full reconcile is not deactivated twice.
                await items_repo.deactivate(session, item)
                await reminders_repo.reconcile_reminders(session, item, now, self._policy)
            await session.commit()
        self._log.warning("presend_check_page_missing", notion_page_id=snapshot.notion_page_id)

    async def _audit_presend_skipped(
        self, claim: _Claim, snapshot: NormalizedItem, exc: BaseException
    ) -> None:
        """Record that the live check could not run, before sending on local data anyway."""
        error = f"{type(exc).__name__}: {exc}"
        async with self._container.session_factory() as session:
            await audit_repo.append(
                session,
                "presend_check_skipped",
                item_id=claim.item_id,
                notion_page_id=snapshot.notion_page_id,
                payload={
                    "reminder_id": str(claim.id),
                    "reminder_type": claim.reminder_type,
                    "reason": type(exc).__name__,
                },
                error=error,
            )
            await session.commit()
        self._log.warning(
            "presend_check_skipped",
            reminder_id=str(claim.id),
            notion_page_id=snapshot.notion_page_id,
            error=error,
        )

    # ── Stale-claim recovery ────────────────────────────────────────────────

    async def recover_stale_claims(self) -> int:
        """Resolve rows left `claimed` by a crash between the claim and its outcome.

        The question this answers is "did the email actually go out?", and Gmail answers it:
        every reminder body carries `ref: <ref_token>`, so a Sent search for that token
        settles it. Found → the row is marked `sent` with the ids Gmail returned (the email
        exists, and the row must say so). Not found → the row goes back to `pending`
        immediately, because no email exists.

        A lookup that *fails* leaves the row exactly as it is: an unreachable Gmail is not
        evidence that nothing was sent, and guessing in either direction is how a reminder
        gets either lost or duplicated. The row stays `claimed`, this method reports one
        fewer recovery than it looked at, and the next tick tries again.

        Returns how many rows were confirmed sent.
        """
        now = self._container.clock.now()
        async with self._container.session_factory() as session:
            stale = await reminders_repo.list_stale_claimed(
                session, now=now, stale_after=STALE_CLAIM_AFTER
            )
            claims = [
                _Claim(
                    id=row.id,
                    item_id=row.item_id,
                    reminder_type=row.reminder_type,
                    due_at_snapshot=row.due_at_snapshot,
                    target_at=row.target_at,
                    ref_token=row.ref_token,
                    attempt_count=row.attempt_count,
                )
                for row in stale
            ]

        recovered = 0
        for claim in claims:
            try:
                found = await self._container.mail.find_sent_by_token(claim.ref_token)
            except Exception as exc:
                self._log.warning(
                    "stale_claim_lookup_failed",
                    reminder_id=str(claim.id),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            if found is None:
                async with self._container.session_factory() as session:
                    await reminders_repo.release_with_backoff(
                        session,
                        claim.id,
                        # `now`, not a backoff: the send never happened, so there is nothing
                        # to wait for. `attempt_count` is untouched — the interrupted claim
                        # already spent one (a crash between claim and send is exactly the
                        # case that budget is sized for).
                        next_attempt_at=now,
                        error=_STALE_CLAIM_NOTHING_SENT,
                    )
                    await session.commit()
                self._log.info("stale_claim_returned_to_pending", reminder_id=str(claim.id))
                continue

            async with self._container.session_factory() as session:
                await reminders_repo.mark_sent(
                    session,
                    claim.id,
                    now=now,
                    provider_message_id=found.provider_message_id,
                    provider_thread_id=found.provider_thread_id,
                )
                await audit_repo.append(
                    session,
                    "reminder_sent",
                    item_id=claim.item_id,
                    provider_message_id=found.provider_message_id,
                    provider_thread_id=found.provider_thread_id,
                    payload={
                        "reminder_id": str(claim.id),
                        "reminder_type": claim.reminder_type,
                        "recovered_from_stale_claim": True,
                    },
                )
                await session.commit()
            recovered += 1
            self._log.warning(
                "stale_claim_recovered_as_sent",
                reminder_id=str(claim.id),
                provider_message_id=found.provider_message_id,
            )
        return recovered

    # ── Shared bits ─────────────────────────────────────────────────────────

    @property
    def _completed_status(self) -> str:
        """The Notion `Status` value that means complete, from config (never hardcoded)."""
        return str(self._container.settings.notion_status_completed)

    @property
    def _policy(self) -> PlannerPolicy:
        """The planner policy for a reconcile driven from here — same source as the sync's."""
        return PlannerPolicy(
            completed_status_value=self._container.settings.notion_status_completed
        )

    async def _alert_safely(self, alert_type: str, subject: str, body: str) -> None:
        """Send an alert, and never let a failed alert damage the reminder path.

        The reminder's own state is already committed by the time this runs. An alert that
        cannot be delivered is logged here and nothing more: raising would abort the tick and
        leave later reminders unsent, which is a strictly worse outcome than a missing
        email about it (the alert is itself rate-limited, and the next failure retries it).
        """
        try:
            await self._alerts.alert(alert_type, subject, body)
        except Exception:
            self._log.exception("alert_send_failed", alert_type=alert_type)


def _as_normalized(item: Item) -> NormalizedItem:
    """The stored item as the composer's input type.

    A read of the local row, not a re-normalization: the guards approved *this* state, so
    the email must describe this state. `in_trash` is derived from `is_active` because
    normalizing the other way round is what put the item in this shape.
    """
    return NormalizedItem(
        notion_page_id=item.notion_page_id,
        notion_data_source_id=item.notion_data_source_id,
        source_db=cast(SourceDb, item.source_db),
        item_kind=cast(ItemKind, item.item_kind),
        notion_type=item.notion_type,
        name=item.name,
        course_page_id=item.course_page_id,
        course=item.course,
        status=item.status,
        done=item.done,
        due_date=item.due_date,
        due_at=item.due_at,
        due_has_time=item.due_has_time,
        timezone=item.timezone,
        notion_url=item.notion_url,
        notion_last_edited_time=item.notion_last_edited_at,
        in_trash=not item.is_active,
    )
