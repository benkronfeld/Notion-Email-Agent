"""`app.db.repositories.reminders` — claims, idempotency controls, state transitions.

These need a real Postgres (`docker compose up -d db`); without one every test in this
module skips. The claim tests are the ones that cannot be proved any other way:
`FOR UPDATE SKIP LOCKED` is a property of two overlapping transactions, so the SKIP LOCKED
proof opens two sessions on two connections and claims from both while neither has
committed.

No test calls a live Notion, Gmail, or DeepSeek endpoint (CLAUDE.md constraint 5), and no
test reads the system clock (constraint 4) — every instant here is a literal.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import AuditLog, Item, Reminder
from app.db.repositories import items as items_repo
from app.db.repositories import reminders as repo
from app.db.session import create_session_factory
from app.domain.planner import (
    FlipToPending,
    InsertReminder,
    MarkSkipped,
    MarkSuperseded,
    iso_utc,
)
from app.domain.types import (
    ItemKind,
    NormalizedItem,
    ReminderStatus,
    ReminderType,
    SkipReason,
    SourceDb,
)

pytestmark = pytest.mark.integration

# Due 2026-10-03 23:59 Eastern (EDT) = 2026-10-04 03:59 UTC — the same instant the planner
# unit tests use, so a target computed here means the same thing there.
DUE = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
T48 = DUE - timedelta(hours=48)  # 2026-10-02 03:59 UTC
T24 = DUE - timedelta(hours=24)  # 2026-10-03 03:59 UTC

# Comfortably before every target.
EARLY = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
# After both targets, before the due instant: everything seeded below is claimable.
LATE = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

SOURCE_DB: dict[ItemKind, SourceDb] = {
    "assignment_reading": "assignments_readings",
    "exam_project": "exams_projects",
}


# ── seeding ─────────────────────────────────────────────────────────────────


async def seed_item(
    session: AsyncSession,
    *,
    page_id: str = "page-repo-1",
    due_at: datetime | None = DUE,
    status: str = "Not started",
    done: bool = False,
    in_trash: bool = False,
    kind: ItemKind = "assignment_reading",
) -> Item:
    """Store one item through the real `items.upsert` path and return its row."""
    normalized = NormalizedItem(
        notion_page_id=page_id,
        notion_data_source_id="ds-1",
        source_db=SOURCE_DB[kind],
        item_kind=kind,
        notion_type="Assignment" if kind == "assignment_reading" else "Exam",
        name="Problem Set 4",
        course_page_id=None,
        course=None,
        status=status,
        done=done,
        due_date=due_at.date() if due_at is not None else None,
        due_at=due_at,
        due_has_time=False,
        timezone="America/New_York",
        notion_url=None,
        notion_last_edited_time=None,
        in_trash=in_trash,
    )
    return await items_repo.upsert(session, normalized)


async def seed_reminder(
    session: AsyncSession,
    item: Item,
    *,
    reminder_type: ReminderType = "assignment_48h",
    due_at_snapshot: datetime = DUE,
    target_at: datetime = T48,
    status: ReminderStatus = "pending",
    skip_reason: SkipReason | None = None,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    claimed_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> UUID:
    """Insert one reminder row directly, for the states the planner would never create."""
    result = await session.execute(
        insert(Reminder)
        .values(
            item_id=item.id,
            reminder_type=reminder_type,
            due_at_snapshot=due_at_snapshot,
            target_at=target_at,
            status=status,
            skip_reason=skip_reason,
            idempotency_key=(
                idempotency_key
                if idempotency_key is not None
                else f"{item.notion_page_id}:{reminder_type}:{iso_utc(due_at_snapshot)}"
            ),
            ref_token=repo.generate_ref_token(),
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            claimed_at=claimed_at,
        )
        .returning(Reminder.id)
    )
    row = result.first()
    assert row is not None
    new_id: UUID = row[0]
    return new_id


async def audit_count(session: AsyncSession, event_type: str, item_id: UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.event_type == event_type, AuditLog.item_id == item_id)
    )
    total: int = result.scalar_one()
    return total


async def stored(session: AsyncSession, reminder_id: UUID) -> Reminder:
    row = await repo.get_by_id(session, reminder_id)
    assert row is not None
    return row


def insert_action(
    *,
    reminder_type: ReminderType = "assignment_48h",
    target_at: datetime = T48,
    status: Literal["pending", "skipped"] = "pending",
    skip_reason: SkipReason | None = None,
    due_at_snapshot: datetime = DUE,
    idempotency_key: str = "page-repo-1:assignment_48h:2026-10-04T03:59:00Z",
) -> InsertReminder:
    return InsertReminder(
        reminder_type=reminder_type,
        due_at_snapshot=due_at_snapshot,
        target_at=target_at,
        status=status,
        skip_reason=skip_reason,
        idempotency_key=idempotency_key,
    )


# ── claim_next (§2.3.2.C) ───────────────────────────────────────────────────


class TestClaimNext:
    async def test_an_empty_queue_returns_none(self, session: AsyncSession) -> None:
        assert await repo.claim_next(session, LATE) is None

    async def test_claims_the_due_row_and_stamps_it(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item)

        claimed = await repo.claim_next(session, LATE)

        assert claimed is not None
        assert claimed.id == reminder_id
        assert claimed.status == "claimed"
        assert claimed.claimed_at == LATE
        assert claimed.attempt_count == 1

    async def test_the_attempt_budget_counts_claims(self, session: AsyncSession) -> None:
        """`attempt_count` increments on the claim, so a crash after it still spends one."""
        item = await seed_item(session)
        await seed_reminder(session, item)

        first = await repo.claim_next(session, LATE)
        assert first is not None and first.attempt_count == 1

        await repo.release_with_backoff(
            session, first.id, next_attempt_at=LATE, error="smtp said no"
        )
        second = await repo.claim_next(session, LATE)

        assert second is not None
        assert second.id == first.id
        assert second.attempt_count == 2

    async def test_ignores_a_row_that_is_backing_off(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        backing_off = await seed_reminder(
            session, item, next_attempt_at=LATE + timedelta(minutes=1)
        )

        assert await repo.claim_next(session, LATE) is None

        # ...and it becomes claimable the moment the backoff expires.
        ready = await repo.claim_next(session, LATE + timedelta(minutes=1))
        assert ready is not None and ready.id == backing_off

    async def test_ignores_a_row_whose_target_is_in_the_future(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        await seed_reminder(session, item, target_at=LATE + timedelta(seconds=1))

        assert await repo.claim_next(session, LATE) is None
        assert await repo.claim_next(session, LATE + timedelta(seconds=1)) is not None

    @pytest.mark.parametrize("status", ["sent", "skipped", "failed", "superseded", "claimed"])
    async def test_ignores_rows_that_are_not_pending(
        self, session: AsyncSession, status: ReminderStatus
    ) -> None:
        item = await seed_item(session)
        await seed_reminder(session, item, status=status)

        assert await repo.claim_next(session, LATE) is None

    async def test_claims_the_earliest_target_first(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        await seed_reminder(session, item, reminder_type="assignment_24h", target_at=T24)

        claimed = await repo.claim_next(session, LATE)
        assert claimed is not None and claimed.id == earlier

    async def test_two_claimants_never_get_the_same_row(
        self, session: AsyncSession, engine: AsyncEngine
    ) -> None:
        """The SKIP LOCKED proof (§2.3.2.C, §2.3.2.F).

        Two sessions on two connections claim while session A's transaction is still open
        and holding its row lock. Without `SKIP LOCKED`, B would block on that lock and the
        timeout below would fire — so this fails loudly rather than merely asserting the
        happy path.
        """
        item = await seed_item(session)
        first_id = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        second_id = await seed_reminder(
            session, item, reminder_type="assignment_24h", target_at=T24
        )
        await session.commit()

        factory = create_session_factory(engine)
        async with factory() as claimant_a, factory() as claimant_b:
            async with asyncio.timeout(15):
                a = await repo.claim_next(claimant_a, LATE)
                b = await repo.claim_next(claimant_b, LATE)

            assert a is not None and b is not None
            assert a.id != b.id, "SKIP LOCKED failed: both workers claimed the same reminder"
            assert {a.id, b.id} == {first_id, second_id}
            assert {a.attempt_count, b.attempt_count} == {1}

            await claimant_a.commit()
            await claimant_b.commit()

        async with factory() as third:
            assert await repo.claim_next(third, LATE) is None


# ── Idempotency controls (§2.3.2.F) ─────────────────────────────────────────


class TestIdempotencyControls:
    async def test_a_duplicate_idempotency_key_raises(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        await seed_reminder(
            session, item, reminder_type="assignment_48h", idempotency_key="duplicate-key"
        )

        with pytest.raises(IntegrityError):
            await seed_reminder(
                session, item, reminder_type="assignment_24h", idempotency_key="duplicate-key"
            )

        await session.rollback()

    async def test_a_repeated_insert_is_absorbed_and_audited_once(
        self, session: AsyncSession
    ) -> None:
        """`ON CONFLICT DO NOTHING` plus the RETURNING check: one row, one audit row."""
        item = await seed_item(session)
        action = insert_action()

        assert await repo.apply_plan(session, item_id=item.id, actions=[action], now=EARLY) == 1
        assert await repo.apply_plan(session, item_id=item.id, actions=[action], now=EARLY) == 0

        rows = await repo.load_for_item(session, item.id)
        assert [row.status for row in rows] == ["pending"]
        assert await audit_count(session, "reminder_created", item.id) == 1

    async def test_a_replayed_cancel_changes_nothing(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item)
        actions = [MarkSuperseded(reminder_id)]

        assert await repo.apply_plan(session, item_id=item.id, actions=actions, now=EARLY) == 1
        assert await repo.apply_plan(session, item_id=item.id, actions=actions, now=EARLY) == 0

        assert await audit_count(session, "reminder_superseded", item.id) == 1

    async def test_a_replayed_resurrection_changes_nothing(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(
            session, item, status="skipped", skip_reason="item_completed"
        )
        actions = [FlipToPending(reminder_id)]

        assert await repo.apply_plan(session, item_id=item.id, actions=actions, now=EARLY) == 1
        assert await repo.apply_plan(session, item_id=item.id, actions=actions, now=EARLY) == 0


# ── apply_plan (§2.3.2.B) ───────────────────────────────────────────────────


class TestApplyPlan:
    async def test_a_missed_window_is_inserted_skipped_and_never_pending(
        self, session: AsyncSession
    ) -> None:
        """FR-4: recorded for the audit trail, never sent."""
        item = await seed_item(session)
        action = insert_action(status="skipped", skip_reason="missed_window")

        changed = await repo.apply_plan(session, item_id=item.id, actions=[action], now=EARLY)

        assert changed == 1
        row = (await repo.load_for_item(session, item.id))[0]
        assert (row.status, row.skip_reason) == ("skipped", "missed_window")
        assert await repo.claim_next(session, LATE) is None

    async def test_mark_skipped_records_the_reason(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item)

        changed = await repo.apply_plan(
            session,
            item_id=item.id,
            actions=[MarkSkipped(reminder_id, "item_completed")],
            now=EARLY,
        )

        assert changed == 1
        row = await stored(session, reminder_id)
        assert (row.status, row.skip_reason) == ("skipped", "item_completed")
        assert row.updated_at is not None
        assert await audit_count(session, "reminder_skipped", item.id) == 1

    async def test_mark_superseded_clears_the_reason(self, session: AsyncSession) -> None:
        # In-flight rows only: the planner emits MarkSuperseded for `pending`/`claimed`
        # rows whose due date moved, never for one that is already settled.
        item = await seed_item(session)
        reminder_id = await seed_reminder(
            session, item, status="pending", skip_reason="item_completed"
        )

        changed = await repo.apply_plan(
            session, item_id=item.id, actions=[MarkSuperseded(reminder_id)], now=EARLY
        )

        assert changed == 1
        row = await stored(session, reminder_id)
        assert (row.status, row.skip_reason) == ("superseded", None)
        assert await audit_count(session, "reminder_superseded", item.id) == 1

    async def test_mark_superseded_never_touches_a_sent_row(self, session: AsyncSession) -> None:
        """Sent history is kept (FR-8 Option A).

        `apply_plan` restricts its transitions to in-flight rows, so a `sent` row cannot be
        clobbered even if a caller hands it this action — which is what stops a due-date
        change from erasing the record that a reminder already went out.
        """
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item, status="sent")

        changed = await repo.apply_plan(
            session, item_id=item.id, actions=[MarkSuperseded(reminder_id)], now=EARLY
        )

        assert changed == 0
        row = await stored(session, reminder_id)
        assert row.status == "sent"
        assert await audit_count(session, "reminder_superseded", item.id) == 0

    async def test_flip_to_pending_clears_the_skip_and_the_backoff(
        self, session: AsyncSession
    ) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(
            session,
            item,
            status="skipped",
            skip_reason="item_inactive",
            next_attempt_at=LATE,
            claimed_at=LATE,
        )

        changed = await repo.apply_plan(
            session, item_id=item.id, actions=[FlipToPending(reminder_id)], now=EARLY
        )

        assert changed == 1
        row = await stored(session, reminder_id)
        assert row.status == "pending"
        assert row.skip_reason is None
        assert row.next_attempt_at is None
        assert row.claimed_at is None

    async def test_a_resurrection_writes_no_audit_event(self, session: AsyncSession) -> None:
        """The deliberate choice documented in the repository module docstring.

        The vocabulary in §2.3.5 is closed and has no resurrection event; `reminder_created`
        would claim a row was created that was not. Nothing is written, and the row itself
        carries the transition.
        """
        item = await seed_item(session)
        reminder_id = await seed_reminder(
            session, item, status="skipped", skip_reason="item_completed"
        )

        await repo.apply_plan(
            session, item_id=item.id, actions=[FlipToPending(reminder_id)], now=EARLY
        )

        result = await session.execute(
            select(AuditLog.event_type).where(AuditLog.item_id == item.id)
        )
        assert list(result.scalars().all()) == []

    async def test_a_cancel_never_touches_a_sent_row(self, session: AsyncSession) -> None:
        """The guard is a real rail: a reminder the scheduler just sent must not be unsent."""
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item, status="sent")

        changed = await repo.apply_plan(
            session,
            item_id=item.id,
            actions=[MarkSkipped(reminder_id, "item_completed")],
            now=EARLY,
        )

        assert changed == 0
        assert (await stored(session, reminder_id)).status == "sent"
        assert await audit_count(session, "reminder_skipped", item.id) == 0


# ── has_later_claimable (§2.3.2.C) ──────────────────────────────────────────


class TestHasLaterClaimable:
    async def test_true_only_for_a_strictly_later_claimable_row(
        self, session: AsyncSession
    ) -> None:
        item = await seed_item(session)
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        later = await seed_reminder(session, item, reminder_type="assignment_24h", target_at=T24)

        async def later_claimable(reminder_id: UUID, target_at: datetime) -> bool:
            return await repo.has_later_claimable(
                session,
                reminder_id=reminder_id,
                item_id=item.id,
                due_at_snapshot=DUE,
                target_at=target_at,
                now=LATE,
            )

        assert await later_claimable(earlier, T48) is True
        assert await later_claimable(later, T24) is False  # nothing is later than the last

    async def test_false_when_the_later_row_is_not_pending(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        await seed_reminder(
            session,
            item,
            reminder_type="assignment_24h",
            target_at=T24,
            status="skipped",
            skip_reason="item_completed",
        )

        assert (
            await repo.has_later_claimable(
                session,
                reminder_id=earlier,
                item_id=item.id,
                due_at_snapshot=DUE,
                target_at=T48,
                now=LATE,
            )
            is False
        )

    async def test_false_when_the_later_row_is_still_backing_off(
        self, session: AsyncSession
    ) -> None:
        item = await seed_item(session)
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        await seed_reminder(
            session,
            item,
            reminder_type="assignment_24h",
            target_at=T24,
            next_attempt_at=LATE + timedelta(minutes=5),
        )

        assert (
            await repo.has_later_claimable(
                session,
                reminder_id=earlier,
                item_id=item.id,
                due_at_snapshot=DUE,
                target_at=T48,
                now=LATE,
            )
            is False
        )

    async def test_false_when_the_later_row_is_not_yet_due(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        await seed_reminder(session, item, reminder_type="assignment_24h", target_at=T24)

        assert (
            await repo.has_later_claimable(
                session,
                reminder_id=earlier,
                item_id=item.id,
                due_at_snapshot=DUE,
                target_at=T48,
                now=T48,  # T24 is still in the future
            )
            is False
        )

    async def test_false_across_a_due_date_change(self, session: AsyncSession) -> None:
        """A reminder for a new due date must not suppress a live one for the old date."""
        item = await seed_item(session)
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        await seed_reminder(
            session,
            item,
            reminder_type="assignment_24h",
            target_at=T24,
            due_at_snapshot=DUE + timedelta(days=7),
        )

        assert (
            await repo.has_later_claimable(
                session,
                reminder_id=earlier,
                item_id=item.id,
                due_at_snapshot=DUE,
                target_at=T48,
                now=LATE,
            )
            is False
        )

    async def test_false_for_a_later_row_on_another_item(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        other = await seed_item(session, page_id="page-repo-2")
        earlier = await seed_reminder(session, item, reminder_type="assignment_48h", target_at=T48)
        await seed_reminder(session, other, reminder_type="assignment_24h", target_at=T24)

        assert (
            await repo.has_later_claimable(
                session,
                reminder_id=earlier,
                item_id=item.id,
                due_at_snapshot=DUE,
                target_at=T48,
                now=LATE,
            )
            is False
        )


# ── stale-claim recovery and the remaining transitions ──────────────────────


class TestStaleClaims:
    async def test_finds_only_claims_older_than_the_threshold(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        stale = await seed_reminder(
            session, item, status="claimed", claimed_at=LATE - timedelta(minutes=11)
        )
        await seed_reminder(
            session,
            item,
            reminder_type="assignment_24h",
            status="claimed",
            claimed_at=LATE - timedelta(minutes=9),
        )

        rows = await repo.list_stale_claimed(session, now=LATE, stale_after=timedelta(minutes=10))

        assert [row.id for row in rows] == [stale]


class TestTransitions:
    async def test_mark_sent_records_the_provider_ids(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item, status="claimed", claimed_at=LATE)

        await repo.mark_sent(
            session,
            reminder_id,
            now=LATE,
            provider_message_id="gmail-msg-1",
            provider_thread_id="gmail-thread-1",
        )

        row = await stored(session, reminder_id)
        assert row.status == "sent"
        assert row.sent_at == LATE
        assert row.provider_message_id == "gmail-msg-1"
        assert row.provider_thread_id == "gmail-thread-1"

    async def test_mark_failed_records_the_error(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item, status="claimed", claimed_at=LATE)

        await repo.mark_failed(session, reminder_id, error="smtp: mailbox unavailable")

        row = await stored(session, reminder_id)
        assert row.status == "failed"
        assert row.last_error == "smtp: mailbox unavailable"

    async def test_mark_skipped_records_the_reason(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item, status="claimed", claimed_at=LATE)

        await repo.mark_skipped(session, reminder_id, skip_reason="past_due", now=LATE)

        row = await stored(session, reminder_id)
        assert (row.status, row.skip_reason) == ("skipped", "past_due")

    async def test_release_with_backoff_returns_the_row_to_the_queue(
        self, session: AsyncSession
    ) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(session, item, status="claimed", claimed_at=LATE)

        await repo.release_with_backoff(
            session, reminder_id, next_attempt_at=LATE + timedelta(minutes=1), error="timeout"
        )

        row = await stored(session, reminder_id)
        assert row.status == "pending"
        assert row.next_attempt_at == LATE + timedelta(minutes=1)
        assert row.claimed_at is None
        assert row.last_error == "timeout"

    async def test_mark_superseded_clears_the_reason(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        reminder_id = await seed_reminder(
            session, item, status="pending", skip_reason="item_completed"
        )

        await repo.mark_superseded(session, reminder_id)

        row = await stored(session, reminder_id)
        assert (row.status, row.skip_reason) == ("superseded", None)


class TestRefToken:
    def test_is_short_alphanumeric_and_unpredictable(self) -> None:
        tokens = {repo.generate_ref_token() for _ in range(200)}
        assert len(tokens) == 200
        assert all(len(token) == repo.REF_TOKEN_LENGTH for token in tokens)
        # It is pasted into an email footer and searched for with Gmail's `q=`, which
        # tokenises on punctuation — so no punctuation may appear.
        assert all(token.isalnum() for token in tokens)


class TestPlannerConversions:
    async def test_orm_rows_convert_into_planner_dtos(self, session: AsyncSession) -> None:
        item = await seed_item(session)
        await seed_reminder(session, item)

        planner_item = repo.to_planner_item(item)
        assert planner_item.item_id == item.id
        assert planner_item.item_kind == "assignment_reading"
        assert planner_item.due_at == DUE
        assert planner_item.is_active is True

        row = (await repo.load_for_item(session, item.id))[0]
        planner_reminder = repo.to_planner_reminder(row)
        assert planner_reminder.reminder_id == row.id
        assert planner_reminder.reminder_type == "assignment_48h"
        assert planner_reminder.status == "pending"
        assert planner_reminder.skip_reason is None
