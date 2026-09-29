"""`NotionWriter`, pinned property by property (§2.3.4, CLAUDE.md constraint 6).

Unit only: `FakeNotionClient` in memory, no database, no network. The fake really applies a
`PATCH` to the page it stores, which is what makes the read-back step worth anything — its
`drop_update` knob is the only way to test "Notion accepted the write and the page
disagrees" (Appendix B case 13) without a live service.

One seam is worth explaining: **`_update_body`.** The three PATCH payload shapes are
asserted against the pure function that builds them, because no test may construct
`NotionRestClient` (constraint 5) and a wrong nesting there would otherwise never surface.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.integrations.notion.client import _update_body
from app.integrations.notion.writer import NotionValues, NotionWriter, WriteOutcome
from fixtures.clock import FrozenClock
from fixtures.fakes import FakeNotionClient, make_container
from fixtures.pages import make_assignment

PAGE = "page-1"
ASSIGNMENTS = "assignments_readings"
DEFAULT_DUE = date(2026, 10, 3)
MOVED_DUE = date(2026, 10, 10)


# ── Arranging a write ───────────────────────────────────────────────────────
#
# `tests/fixtures/fakes.py` raises `FakeNotionTransient` / `FakeNotionNotFound`, which
# subclass the adapter's real `NotionTransientError` / `NotionNotFoundError` — so the fake's
# simulated failures exercise the writer's actual retry and terminal rules, and not a
# lookalike of them.


def _writer(settings: Settings, clock: FrozenClock, notion: FakeNotionClient) -> NotionWriter:
    """A real `NotionWriter` over a container of fakes — nothing here touches Notion."""
    return NotionWriter(make_container(settings, clock=clock, notion=notion))


def _without_done(settings: Settings, source_db: str = ASSIGNMENTS) -> Settings:
    """Settings for a database whose `HAS_DONE` flag is false (§2.2)."""
    assert source_db == ASSIGNMENTS, "the helper only models the assignments database"
    return settings.model_copy(update={"notion_assignments_readings_has_done": False})


class _PageVanishesOnWrite(FakeNotionClient):
    """A page that reads fine and is gone by the time the `PATCH` lands.

    `FakeNotionClient` alone cannot express this: its `update_page` only raises
    `FakeNotionNotFound` for a page that `get_page` would already have reported as missing,
    so the writer would take the "page is gone" path before ever attempting a write.
    """

    async def update_page(
        self,
        page_id: str,
        status_property: str,
        status_name: str | None,
        done_property: str | None,
        done_value: bool | None,
        due_property: str,
        due_date: date | None,
    ) -> None:
        self.pages = [page for page in self.pages if page.page_id != page_id]
        await super().update_page(
            page_id,
            status_property,
            status_name,
            done_property,
            done_value,
            due_property,
            due_date,
        )


class _DoneIgnored(FakeNotionClient):
    """A `PATCH` that lands every property except `Done`, which must be caught by the read-back."""

    async def update_page(
        self,
        page_id: str,
        status_property: str,
        status_name: str | None,
        done_property: str | None,
        done_value: bool | None,
        due_property: str,
        due_date: date | None,
    ) -> None:
        await super().update_page(
            page_id, status_property, status_name, None, None, due_property, due_date
        )


# ── The change set, and the write surface ───────────────────────────────────


async def test_a_status_change_writes_status_and_done_in_one_patch(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.succeeded
    assert outcome.result == "verified"
    # Exactly one write per call (the retry path is a failure path, tested below).
    assert len(notion.update_page_calls) == 1
    call = notion.update_page_calls[0]
    assert call.page_id == PAGE
    # The write surface, by configured name — never a literal in the writer.
    assert call.status_property == settings.notion_prop_status == "Status"
    assert call.status_name == "Completed"
    assert call.done_property == settings.notion_prop_done == "Done"
    assert call.done_value is True
    # `Due Date` was not part of this change, so it is not part of the write.
    assert call.due_property == settings.notion_prop_due == "Due Date"
    assert call.due_date is None
    assert outcome.wrote_done is True
    assert outcome.due_date_was_range is False
    # What the confirmation email quotes is what the read-back returned.
    assert outcome.before == NotionValues("Not started", False, DEFAULT_DUE)
    assert outcome.after == NotionValues("Completed", True, DEFAULT_DUE)

    # Nothing else on the page was touched: not the title, not `Type`, not the `Course`
    # relation (constraint 6).
    page = await notion.get_page(PAGE)
    assert page is not None
    assert page.properties["Name"]["title"][0]["plain_text"] == "Problem Set 4"
    assert page.properties["Type"]["select"]["name"] == "Assignment"
    assert page.properties["Course"]["relation"] == [{"id": "course-1"}]


async def test_a_due_date_change_writes_only_the_due_date(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status=None, due_date=MOVED_DUE
    )

    assert outcome.succeeded
    assert len(notion.update_page_calls) == 1
    call = notion.update_page_calls[0]
    assert call.status_property == settings.notion_prop_status
    assert call.status_name is None
    # `Status` did not move, so `Done` must not move with it.
    assert call.done_property is None
    assert call.done_value is None
    assert call.due_property == settings.notion_prop_due
    assert call.due_date == MOVED_DUE
    assert outcome.wrote_done is False
    assert outcome.after == NotionValues("Not started", False, MOVED_DUE)


async def test_a_combined_change_writes_all_three_properties(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=MOVED_DUE
    )

    assert outcome.succeeded
    assert len(notion.update_page_calls) == 1
    call = notion.update_page_calls[0]
    assert (call.status_name, call.done_value, call.due_date) == ("Completed", True, MOVED_DUE)
    assert outcome.wrote_done is True
    assert outcome.after == NotionValues("Completed", True, MOVED_DUE)


async def test_a_database_without_done_writes_only_status(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    """`HAS_DONE=false` means `Done` is never written — not written as false, not written."""
    has_done_false = _without_done(settings)
    notion = FakeNotionClient([make_assignment(page_id=PAGE, include_done=False)])
    writer = _writer(has_done_false, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.succeeded
    call = notion.update_page_calls[0]
    assert call.status_name == "Completed"
    assert call.done_property is None
    assert call.done_value is None
    assert outcome.wrote_done is False
    page = await notion.get_page(PAGE)
    assert page is not None
    assert "Done" not in page.properties


async def test_in_progress_moves_done_back_to_false(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment(page_id=PAGE, status="Completed", done=True)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="In progress", due_date=None
    )

    assert outcome.succeeded
    assert notion.update_page_calls[0].done_value is False
    assert outcome.after == NotionValues("In progress", False, DEFAULT_DUE)


async def test_values_that_are_already_set_write_nothing(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    """The "already set" reply, and the reason no empty PATCH is ever sent."""
    notion = FakeNotionClient([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Not started", due_date=DEFAULT_DUE
    )

    assert outcome.result == "no_change"
    assert outcome.no_change and not outcome.succeeded and not outcome.failed
    assert outcome.after == outcome.before == NotionValues("Not started", False, DEFAULT_DUE)
    assert notion.update_page_calls == []
    # One fetch to see the current values; no read-back, because nothing was written.
    assert len(notion.get_page_calls) == 1


async def test_nothing_to_change_at_all_is_also_a_no_op(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(page_id=PAGE, source_db=ASSIGNMENTS, status=None, due_date=None)

    assert outcome.no_change
    assert notion.update_page_calls == []


# ── Failures ────────────────────────────────────────────────────────────────


async def test_a_missing_page_fails_before_any_write(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient()
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id="ghost", source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.failed
    assert outcome.page_missing
    assert not outcome.succeeded
    assert outcome.after is None and outcome.before is None
    assert outcome.reason is not None and "ghost" in outcome.reason
    assert notion.update_page_calls == []


async def test_a_write_that_does_not_land_is_a_failure_never_a_success_claim(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    """Appendix B case 13: the read-back is the whole point, and it is the authority."""
    notion = FakeNotionClient([make_assignment(page_id=PAGE)], drop_update=True)
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert len(notion.update_page_calls) == 1  # the PATCH was sent...
    assert outcome.result == "failed"
    assert outcome.failed and not outcome.succeeded
    # ...and the outcome reports what the page actually holds, not what was asked for.
    assert outcome.after == NotionValues("Not started", False, DEFAULT_DUE)
    assert outcome.reason is not None
    assert "Status" in outcome.reason


async def test_a_read_back_that_disagrees_on_done_alone_is_still_a_failure(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    """`Done` is part of the comparison, not a detail the read-back may skip."""
    notion = _DoneIgnored([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.failed
    assert outcome.wrote_done is True
    assert outcome.after == NotionValues("Completed", False, DEFAULT_DUE)
    assert outcome.reason is not None and settings.notion_prop_done in outcome.reason


async def test_a_transient_failure_is_retried_once_and_then_verified(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment(page_id=PAGE)], fail_update_times=1)
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.succeeded
    assert outcome.retried
    assert outcome.attempts == 2
    assert len(notion.update_page_calls) == 2
    assert outcome.after == NotionValues("Completed", True, DEFAULT_DUE)


async def test_two_transient_failures_stop_there(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    """One retry is the whole budget: no blind retry loop of writes (§2.3.4)."""
    notion = FakeNotionClient([make_assignment(page_id=PAGE)], fail_update_times=9)
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.failed
    assert outcome.attempts == 2
    assert len(notion.update_page_calls) == 2
    assert outcome.reason is not None and "twice" in outcome.reason


async def test_a_404_on_the_write_is_terminal_and_not_retried(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = _PageVanishesOnWrite([make_assignment(page_id=PAGE)])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status="Completed", due_date=None
    )

    assert outcome.failed
    assert outcome.attempts == 1
    assert len(notion.update_page_calls) == 1  # a 404 is never retried
    assert outcome.reason is not None and "404" in outcome.reason


async def test_a_due_date_range_is_reported_so_the_audit_log_can_note_it(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    """§2.3.4: only `start` moves on a range; the caller has to be told the `end` stayed."""
    page = make_assignment(page_id=PAGE)
    ranged = page.properties["Due Date"]
    ranged["date"]["end"] = "2026-10-05"
    notion = FakeNotionClient([page])
    writer = _writer(settings, frozen_clock, notion)

    outcome = await writer.apply(
        page_id=PAGE, source_db=ASSIGNMENTS, status=None, due_date=MOVED_DUE
    )

    assert outcome.succeeded
    assert outcome.due_date_was_range is True


# ── The PATCH body itself (§2.3.4 payload shapes) ───────────────────────────


def test_update_body_writes_status_by_option_name() -> None:
    body = _update_body(
        status_property="Status",
        status_name="Completed",
        done_property=None,
        done_value=None,
        due_property="Due Date",
        due_date=None,
    )
    assert body == {"properties": {"Status": {"status": {"name": "Completed"}}}}


def test_update_body_writes_done_as_a_checkbox() -> None:
    for value in (True, False):
        body = _update_body(
            status_property="Status",
            status_name=None,
            done_property="Done",
            done_value=value,
            due_property="Due Date",
            due_date=None,
        )
        assert body == {"properties": {"Done": {"checkbox": value}}}


def test_update_body_writes_a_date_only_due_date() -> None:
    body = _update_body(
        status_property="Status",
        status_name=None,
        done_property=None,
        done_value=None,
        due_property="Due Date",
        due_date=MOVED_DUE,
    )
    assert body == {"properties": {"Due Date": {"date": {"start": "2026-10-10"}}}}


def test_update_body_carries_all_three_properties_in_one_patch() -> None:
    body = _update_body(
        status_property="Status",
        status_name="Completed",
        done_property="Done",
        done_value=True,
        due_property="Due Date",
        due_date=MOVED_DUE,
    )
    assert body == {
        "properties": {
            "Status": {"status": {"name": "Completed"}},
            "Done": {"checkbox": True},
            "Due Date": {"date": {"start": "2026-10-10"}},
        }
    }


def test_update_body_refuses_an_empty_write() -> None:
    """An empty body is accepted by Notion and changes nothing — it must never be sent."""
    with pytest.raises(ValueError, match="nothing to change"):
        _update_body(
            status_property="Status",
            status_name=None,
            done_property=None,
            done_value=None,
            due_property="Due Date",
            due_date=None,
        )


def test_update_body_refuses_a_done_value_for_a_database_without_done() -> None:
    """`HAS_DONE=false` plus a `done_value` is a caller bug, not a value to drop silently."""
    with pytest.raises(ValueError, match="done_value"):
        _update_body(
            status_property="Status",
            status_name="Completed",
            done_property=None,
            done_value=True,
            due_property="Due Date",
            due_date=None,
        )


# ── The outcome type the caller composes emails from ────────────────────────


def test_write_outcome_flags() -> None:
    verified = WriteOutcome(result="verified", page_id=PAGE, source_db=ASSIGNMENTS, attempts=1)
    no_change = WriteOutcome(result="no_change", page_id=PAGE, source_db=ASSIGNMENTS)
    missing = WriteOutcome(result="page_not_found", page_id=PAGE, source_db=ASSIGNMENTS)
    failed = WriteOutcome(result="failed", page_id=PAGE, source_db=ASSIGNMENTS, reason="nope")

    assert verified.succeeded and not verified.failed and not verified.no_change
    assert verified.retried is False
    assert no_change.no_change and not no_change.succeeded and not no_change.failed
    assert missing.failed and missing.page_missing and not missing.succeeded
    assert failed.failed and not failed.page_missing and not failed.succeeded
