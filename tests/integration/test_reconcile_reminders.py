"""`reconcile_reminders` against a real Postgres — the planner matrix, persisted.

`tests/unit/test_planner.py` proves the decision logic; this module proves the SQL half
does what those decisions say, and that the whole thing is idempotent against a real
unique constraint rather than an in-memory stand-in. The two together are the claim
`reconcile_reminders` makes: safe to call on every upsert, forever.

Needs `docker compose up -d db`; without it every test here skips. No test calls a live
service, and none reads the system clock — `now` is a literal at every call site.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Item, Reminder
from app.db.repositories import items as items_repo
from app.db.repositories import reminders as repo
from app.domain.planner import (
    FlipToPending,
    InsertReminder,
    MarkSkipped,
    MarkSuperseded,
    PlannerPolicy,
    iso_utc,
)
from app.domain.types import ItemKind, NormalizedItem, SourceDb

pytestmark = pytest.mark.integration

POLICY = PlannerPolicy(completed_status_value="Completed")

# Due 2026-10-03 23:59 Eastern (EDT) = 2026-10-04 03:59 UTC.
DUE = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
T48 = DUE - timedelta(hours=48)  # 2026-10-02 03:59 UTC
T24 = DUE - timedelta(hours=24)  # 2026-10-03 03:59 UTC
T120 = DUE - timedelta(hours=120)  # 2026-09-29 03:59 UTC

EARLY = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
# After T48, before T24: the "added between the two targets" case.
BETWEEN = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

NEW_DUE = DUE + timedelta(days=7)

SOURCE_DB: dict[ItemKind, SourceDb] = {
    "assignment_reading": "assignments_readings",
    "exam_project": "exams_projects",
}


async def store_item(
    session: AsyncSession,
    *,
    page_id: str = "page-reconcile-1",
    due_at: datetime | None = DUE,
    status: str = "Not started",
    done: bool = False,
    in_trash: bool = False,
    kind: ItemKind = "assignment_reading",
) -> Item:
    """Upsert one item through the real path, then re-read it.

    The `refresh` is not cosmetic: SQLAlchemy refreshes only *unloaded* attributes when a
    SELECT matches an object already in the session's identity map, and `upsert` writes
    through a Core statement that does not expire it. Without this, a second `store_item`
    for the same page would hand back the previous due date and every assertion below
    would be about the wrong item.
    """
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
    item = await items_repo.upsert(session, normalized)
    await session.refresh(item)
    return item


async def reminders_of(session: AsyncSession, item: Item) -> list[Reminder]:
    return await repo.load_for_item(session, item.id)


async def audit_events(session: AsyncSession, item_id: UUID, event_type: str) -> list[AuditLog]:
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.event_type == event_type, AuditLog.item_id == item_id)
        .order_by(AuditLog.id)
    )
    return list(result.scalars().all())


async def audit_total(session: AsyncSession, item_id: UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.item_id == item_id)
    )
    total: int = result.scalar_one()
    return total


def schedule_by_type(rows: list[Reminder]) -> dict[str, Reminder]:
    return {row.reminder_type: row for row in rows}


# ── the fresh item ──────────────────────────────────────────────────────────


class TestFreshItem:
    async def test_creates_two_pending_reminders_and_two_audit_rows(
        self, session: AsyncSession
    ) -> None:
        item = await store_item(session)

        actions = await repo.reconcile_reminders(session, item, EARLY, POLICY)

        assert [type(action) for action in actions] == [InsertReminder, InsertReminder]
        rows = await reminders_of(session, item)
        assert {(row.reminder_type, row.status) for row in rows} == {
            ("assignment_48h", "pending"),
            ("assignment_24h", "pending"),
        }
        by_type = schedule_by_type(rows)
        assert by_type["assignment_48h"].target_at == T48
        assert by_type["assignment_24h"].target_at == T24
        assert {row.due_at_snapshot for row in rows} == {DUE}
        assert {row.skip_reason for row in rows} == {None}
        assert {row.idempotency_key for row in rows} == {
            f"page-reconcile-1:assignment_48h:{iso_utc(DUE)}",
            f"page-reconcile-1:assignment_24h:{iso_utc(DUE)}",
        }
        assert await audit_total(session, item.id) == 2
        assert len(await audit_events(session, item.id, "reminder_created")) == 2

    async def test_an_exam_gets_its_own_schedule(self, session: AsyncSession) -> None:
        item = await store_item(session, kind="exam_project")

        await repo.reconcile_reminders(session, item, EARLY, POLICY)

        rows = await reminders_of(session, item)
        assert {(row.reminder_type, row.target_at) for row in rows} == {
            ("exam_120h", T120),
            ("exam_48h", T48),
        }

    async def test_a_second_call_is_completely_silent(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)
        rows_before = await reminders_of(session, item)

        actions = await repo.reconcile_reminders(session, item, EARLY, POLICY)

        assert actions == []
        rows_after = await reminders_of(session, item)
        assert [row.id for row in rows_after] == [row.id for row in rows_before]
        assert await audit_total(session, item.id) == 2  # no new audit rows either

    async def test_an_item_with_no_due_date_gets_nothing(self, session: AsyncSession) -> None:
        item = await store_item(session, due_at=None)

        assert await repo.reconcile_reminders(session, item, EARLY, POLICY) == []
        assert await reminders_of(session, item) == []
        assert await audit_total(session, item.id) == 0


class TestMissedWindows:
    """FR-4: a target that already passed is recorded, never sent."""

    async def test_added_between_the_two_targets(self, session: AsyncSession) -> None:
        item = await store_item(session)

        await repo.reconcile_reminders(session, item, BETWEEN, POLICY)

        by_type = schedule_by_type(await reminders_of(session, item))
        assert (by_type["assignment_48h"].status, by_type["assignment_48h"].skip_reason) == (
            "skipped",
            "missed_window",
        )
        assert by_type["assignment_24h"].status == "pending"

    async def test_a_missed_window_is_never_claimable(self, session: AsyncSession) -> None:
        item = await store_item(session)

        await repo.reconcile_reminders(session, item, BETWEEN, POLICY)

        # The 48h window is gone; the 24h one is still live and is the one claimed.
        claimed = await repo.claim_next(session, T24 + timedelta(minutes=1))
        assert claimed is not None and claimed.reminder_type == "assignment_24h"
        assert await repo.claim_next(session, T24 + timedelta(minutes=1)) is None

    async def test_reconciling_later_does_not_revive_it(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, BETWEEN, POLICY)

        assert await repo.reconcile_reminders(session, item, T24 + timedelta(hours=1), POLICY) == []


# ── the planner matrix, persisted ───────────────────────────────────────────


class TestDueDateChange:
    """FR-8 Option A: a new due date supersedes the old rows and gets a fresh schedule."""

    async def test_supersedes_the_old_pending_rows_and_inserts_a_fresh_schedule(
        self, session: AsyncSession
    ) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)
        original = {row.reminder_type: row.id for row in await reminders_of(session, item)}

        moved = await store_item(session, due_at=NEW_DUE)
        actions = await repo.reconcile_reminders(session, moved, EARLY, POLICY)

        assert [type(action) for action in actions] == [
            MarkSuperseded,
            MarkSuperseded,
            InsertReminder,
            InsertReminder,
        ]

        rows = await reminders_of(session, moved)
        assert len(rows) == 4  # sent history is kept; the old rows are not deleted
        old = [row for row in rows if row.due_at_snapshot == DUE]
        new = [row for row in rows if row.due_at_snapshot == NEW_DUE]
        assert {row.status for row in old} == {"superseded"}
        assert {row.skip_reason for row in old} == {None}
        assert {row.id for row in old} == set(original.values())
        assert {row.status for row in new} == {"pending"}
        assert {row.target_at for row in new} == {
            NEW_DUE - timedelta(hours=48),
            NEW_DUE - timedelta(hours=24),
        }

        assert len(await audit_events(session, moved.id, "reminder_superseded")) == 2
        assert len(await audit_events(session, moved.id, "reminder_created")) == 4

    async def test_the_new_schedule_is_settled_after_one_pass(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)
        moved = await store_item(session, due_at=NEW_DUE)
        await repo.reconcile_reminders(session, moved, EARLY, POLICY)
        audit_after_first = await audit_total(session, moved.id)

        assert await repo.reconcile_reminders(session, moved, EARLY, POLICY) == []
        assert await audit_total(session, moved.id) == audit_after_first


class TestCompletionAndActivity:
    """FR-3 / FR-9: completed and archived items send nothing."""

    async def test_completing_marks_the_pending_rows_skipped(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)

        completed = await store_item(session, status="Completed")
        actions = await repo.reconcile_reminders(session, completed, EARLY, POLICY)

        assert [type(action) for action in actions] == [MarkSkipped, MarkSkipped]
        rows = await reminders_of(session, completed)
        assert {row.status for row in rows} == {"skipped"}
        assert {row.skip_reason for row in rows} == {"item_completed"}
        assert len(await audit_events(session, completed.id, "reminder_skipped")) == 2

        # Nothing a completed item owns can be claimed.
        assert await repo.claim_next(session, T48) is None

    async def test_done_true_with_status_not_started_is_still_complete(
        self, session: AsyncSession
    ) -> None:
        """FR-3: `Done` and `Status` are checked independently, never as a pair."""
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)

        done = await store_item(session, status="Not started", done=True)
        await repo.reconcile_reminders(session, done, EARLY, POLICY)

        rows = await reminders_of(session, done)
        assert {row.skip_reason for row in rows} == {"item_completed"}

    async def test_archiving_marks_the_pending_rows_skipped(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)

        archived = await store_item(session, in_trash=True)
        await repo.reconcile_reminders(session, archived, EARLY, POLICY)

        rows = await reminders_of(session, archived)
        assert {row.skip_reason for row in rows} == {"item_inactive"}


class TestResurrection:
    """The symmetric skip -> pending transition (§2.3.2.B step 2)."""

    async def test_uncompleting_flips_a_still_future_row_back_to_pending(
        self, session: AsyncSession
    ) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)
        completed = await store_item(session, status="Completed")
        await repo.reconcile_reminders(session, completed, EARLY, POLICY)
        skipped_ids = {row.id for row in await reminders_of(session, completed)}

        reopened = await store_item(session, status="In progress")
        actions = await repo.reconcile_reminders(session, reopened, EARLY, POLICY)

        assert [type(action) for action in actions] == [FlipToPending, FlipToPending]
        rows = await reminders_of(session, reopened)
        assert {row.id for row in rows} == skipped_ids
        assert {row.status for row in rows} == {"pending"}
        assert {row.skip_reason for row in rows} == {None}
        assert {row.claimed_at for row in rows} == {None}
        assert {row.next_attempt_at for row in rows} == {None}

    async def test_unarchiving_resurrects_a_still_future_row(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)
        archived = await store_item(session, in_trash=True)
        await repo.reconcile_reminders(session, archived, EARLY, POLICY)

        back = await store_item(session, in_trash=False)
        await repo.reconcile_reminders(session, back, EARLY, POLICY)

        assert {row.status for row in await reminders_of(session, back)} == {"pending"}

    async def test_a_missed_window_never_comes_back(self, session: AsyncSession) -> None:
        """Only a completion/inactive skip is revivable; a passed window is gone for good."""
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, BETWEEN, POLICY)
        completed = await store_item(session, status="Completed")
        await repo.reconcile_reminders(session, completed, EARLY, POLICY)

        reopened = await store_item(session, status="Not started")
        await repo.reconcile_reminders(session, reopened, EARLY, POLICY)

        by_type = schedule_by_type(await reminders_of(session, reopened))
        assert (by_type["assignment_48h"].status, by_type["assignment_48h"].skip_reason) == (
            "skipped",
            "missed_window",
        )

    async def test_the_full_round_trip_settles(self, session: AsyncSession) -> None:
        item = await store_item(session)
        await repo.reconcile_reminders(session, item, EARLY, POLICY)

        completed = await store_item(session, status="Completed")
        await repo.reconcile_reminders(session, completed, EARLY, POLICY)
        assert await repo.reconcile_reminders(session, completed, EARLY, POLICY) == []

        reopened = await store_item(session, status="In progress")
        await repo.reconcile_reminders(session, reopened, EARLY, POLICY)
        assert await repo.reconcile_reminders(session, reopened, EARLY, POLICY) == []

        rows = await reminders_of(session, reopened)
        assert len(rows) == 2
        assert {row.status for row in rows} == {"pending"}
