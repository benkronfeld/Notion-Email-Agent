"""The frozen-clock end-to-end dry run.

This is the MVP's own acceptance test. It drives the real application — real FastAPI app,
real scheduler wiring, real Postgres, real composer — with **fake Notion and Gmail
adapters** and an injected `FrozenClock`, then advances time across a due date and asserts
what the system actually did.

No network call is made and no email is sent: the fake mail client records what it was
asked to send. That is the only way to prove the reminder flow end to end without mailing a
real person (CLAUDE.md constraints 1 and 5).

What it demonstrates, against the MVP success criteria in §1.2:
  1. an item is discovered by the sync,
  2. targets are computed correctly and a missed window is never sent late,
  3. a completed or archived item sends nothing,
  4. each reminder is sent exactly once,
  5. every event is in the audit log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, EmailThread, Item, OutboundMessage, Reminder
from fixtures.pages import make_assignment, make_course_page

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

PAGE_ID = "page-assignment-1"
COURSE_PAGE_ID = "course-micro"

# One assignment due Saturday 2026-10-03, which is 2026-10-04T03:59Z in UTC (EDT).
#   48h target -> 2026-10-02T03:59Z
#   24h target -> 2026-10-03T03:59Z
#   due_at     -> 2026-10-04T03:59Z
T_AFTER_48H = "2026-10-02T12:00:00+00:00"
T_AFTER_DUE = "2026-10-04T12:00:00+00:00"


async def _fresh(session: Any) -> Any:
    """End the current transaction so the next query sees the app's committed writes.

    The app commits through its own sessions; this test's session would otherwise keep
    reading from the snapshot it opened and would miss everything that happened.
    """
    await session.rollback()
    return session


async def _reminders(session: Any) -> list[Reminder]:
    session = await _fresh(session)
    rows = (await session.execute(select(Reminder).order_by(Reminder.target_at))).scalars().all()
    return list(rows)


async def _items(session: Any) -> list[Item]:
    session = await _fresh(session)
    return list((await session.execute(select(Item))).scalars().all())


async def _all(session: Any, model: Any) -> list[Any]:
    session = await _fresh(session)
    return list((await session.execute(select(model))).scalars().all())


async def _audit(session: Any, event_type: str) -> list[AuditLog]:
    session = await _fresh(session)
    rows = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == event_type).order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _sync(client: Any, headers: dict[str, str]) -> None:
    response = await client.post("/admin/sync", json={"full": False}, headers=headers)
    assert response.status_code == 200, response.text


async def _tick(client: Any, headers: dict[str, str]) -> None:
    response = await client.post("/admin/scheduler/run", headers=headers)
    assert response.status_code == 200, response.text


def _seed(notion: Any, **item: Any) -> None:
    """One assignment plus the course page its `Course` relation points at."""
    notion.add(make_course_page(page_id=COURSE_PAGE_ID, title="Microeconomics"))
    notion.add(
        make_assignment(
            page_id=PAGE_ID, course_page_id=COURSE_PAGE_ID, name="Problem Set 4", **item
        )
    )


class TestTheHappyPath:
    """Sync, send the first reminder once, then leave the second to miss its window."""

    async def test_one_reminder_is_never_sent_twice_and_a_past_due_one_is_skipped(
        self,
        client: Any,
        admin_headers: dict[str, str],
        notion: Any,
        mail: Any,
        frozen_clock: Any,
        session: Any,
    ) -> None:
        _seed(notion, due_start="2026-10-03")

        # ── 1. Sync discovers the item and schedules both reminders ──────────
        await _sync(client, admin_headers)

        items = await _items(session)
        assert len(items) == 1
        # A date-only due date means 11:59 PM Eastern, stored as UTC (FR-1).
        assert items[0].due_at is not None
        assert items[0].due_at.isoformat() == "2026-10-04T03:59:00+00:00"
        assert items[0].course == "Microeconomics"  # resolved through the Course relation

        reminders = await _reminders(session)
        assert [(r.reminder_type, r.status) for r in reminders] == [
            ("assignment_48h", "pending"),
            ("assignment_24h", "pending"),
        ]

        # ── 2. Cross the 48h target: exactly one email ──────────────────────
        frozen_clock.advance(timedelta(hours=36))  # -> 2026-10-02T12:00Z
        await _tick(client, admin_headers)

        assert len(mail.sent) == 1
        sent = mail.sent[0]
        assert sent.subject == (
            "[Reminder] Microeconomics: Problem Set 4, due Sat Oct 3 (in 48 hours)"
        )
        assert "ref: " in sent.body
        assert sent.thread_id is None, "every reminder starts a new thread (one item per email)"

        reminders = await _reminders(session)
        by_type = {r.reminder_type: r for r in reminders}
        assert by_type["assignment_48h"].status == "sent"
        assert by_type["assignment_48h"].provider_message_id
        assert by_type["assignment_48h"].provider_thread_id
        assert by_type["assignment_24h"].status == "pending"  # still in the future

        # The thread mapping V1's reply flow depends on, stored at send time
        assert len(await _all(session, EmailThread)) == 1
        outbound = await _all(session, OutboundMessage)
        assert len(outbound) == 1
        assert outbound[0].kind == "reminder"
        assert outbound[0].rfc_message_id  # the In-Reply-To fallback needs this

        # ── 3. Ticking again must not send a second copy (FR-7) ─────────────
        await _tick(client, admin_headers)
        await _tick(client, admin_headers)
        assert len(mail.sent) == 1

        # ── 4. Cross the due date: the remaining reminder is skipped, not sent ──
        frozen_clock.advance(timedelta(hours=48))  # 2026-10-04T12:00Z, past due_at
        await _tick(client, admin_headers)

        reminders = await _reminders(session)
        by_type = {r.reminder_type: r for r in reminders}
        assert (by_type["assignment_24h"].status, by_type["assignment_24h"].skip_reason) == (
            "skipped",
            "past_due",
        )
        assert len(mail.sent) == 1, "nothing may be sent once the due date has passed"

        # ── 5. The audit trail records every event ──────────────────────────
        # One `notion_sync` per configured database, not per sync run.
        sync_events = await _audit(session, "notion_sync")
        assert {row.payload.get("source_db") for row in sync_events} == {
            "assignments_readings",
            "exams_projects",
        }
        assert len(await _audit(session, "reminder_created")) == 2
        assert len(await _audit(session, "reminder_sent")) == 1
        skipped = await _audit(session, "reminder_skipped")
        assert [row.payload.get("skip_reason") for row in skipped] == ["past_due"]


class TestMissedWindow:
    """An item discovered inside its 48h window: that target is never sent late (FR-4)."""

    async def test_the_missed_target_is_recorded_not_sent(
        self,
        client: Any,
        admin_headers: dict[str, str],
        notion: Any,
        mail: Any,
        frozen_clock: Any,
        session: Any,
    ) -> None:
        _seed(notion, due_start="2026-10-03")

        # 2026-10-02T12:00Z is after the 48h target and before the 24h one.
        frozen_clock.advance(timedelta(hours=36))
        await _sync(client, admin_headers)

        reminders = await _reminders(session)
        assert [(r.reminder_type, r.status, r.skip_reason) for r in reminders] == [
            ("assignment_48h", "skipped", "missed_window"),
            ("assignment_24h", "pending", None),
        ]
        assert mail.sent == [], "the missed window must never be sent retroactively"

        # The still-live target does fire when its time comes.
        frozen_clock.advance(timedelta(hours=16))  # 2026-10-03T04:00Z, past the 24h target
        await _tick(client, admin_headers)
        assert len(mail.sent) == 1
        assert "(in 24 hours)" in mail.sent[0].subject


class TestCompletedItem:
    """A completed item sends nothing at all (FR-3)."""

    async def test_a_completed_item_schedules_nothing(
        self,
        client: Any,
        admin_headers: dict[str, str],
        notion: Any,
        mail: Any,
        frozen_clock: Any,
        session: Any,
    ) -> None:
        _seed(notion, due_start="2026-10-03", status="Completed", done=True)

        await _sync(client, admin_headers)

        assert await _reminders(session) == []
        frozen_clock.advance(timedelta(hours=36))
        await _tick(client, admin_headers)
        assert mail.sent == []


class TestSupersededByLater:
    """After downtime, only the reminder closest to the due date goes out."""

    async def test_only_one_email_after_a_long_outage(
        self,
        client: Any,
        admin_headers: dict[str, str],
        notion: Any,
        mail: Any,
        frozen_clock: Any,
        session: Any,
    ) -> None:
        _seed(notion, due_start="2026-10-03")
        await _sync(client, admin_headers)

        # Both targets are now in the past but the due date is not: this is what an
        # outage across the whole window looks like.
        frozen_clock.advance(timedelta(hours=72))  # 2026-10-04T00:00Z
        await _tick(client, admin_headers)

        assert len(mail.sent) == 1, "the owner must not receive two emails at once"
        reminders = await _reminders(session)
        by_type = {r.reminder_type: r for r in reminders}
        assert by_type["assignment_48h"].status == "skipped"
        assert by_type["assignment_48h"].skip_reason == "superseded_by_later"
        assert by_type["assignment_24h"].status == "sent"


class TestDueDateChange:
    """FR-8 Option A: a moved due date gets a fresh schedule; sent history is kept."""

    async def test_moving_the_due_date_supersedes_the_old_schedule(
        self,
        client: Any,
        admin_headers: dict[str, str],
        notion: Any,
        mail: Any,
        frozen_clock: Any,
        session: Any,
    ) -> None:
        _seed(notion, due_start="2026-10-03")
        await _sync(client, admin_headers)
        assert len(await _reminders(session)) == 2

        # The assignment is pushed out a week. `last_edited_time` must move past the stored
        # cursor, or the incremental sync correctly returns nothing and the change is
        # invisible — which is the whole point of a cursor-based sync.
        notion.add(
            make_assignment(
                page_id=PAGE_ID,
                course_page_id=COURSE_PAGE_ID,
                name="Problem Set 4",
                due_start="2026-10-10",
                last_edited_time=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
            )
        )
        await _sync(client, admin_headers)

        reminders = await _reminders(session)
        old = [r for r in reminders if r.due_at_snapshot.isoformat() == "2026-10-04T03:59:00+00:00"]
        new = [r for r in reminders if r.due_at_snapshot.isoformat() == "2026-10-11T03:59:00+00:00"]
        assert {r.status for r in old} == {"superseded"}
        assert {r.status for r in new} == {"pending"}
        assert len(new) == 2, "the new due date needs its own full schedule"
        assert mail.sent == []
