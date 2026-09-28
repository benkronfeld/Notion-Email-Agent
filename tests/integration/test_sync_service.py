"""`SyncService` against a real Postgres and fake adapters (spec §2.3.2.A).

What this module is for: the sync service is orchestration, and orchestration is where the
rules that are *not* written down anywhere else live — which pages get read, when a cursor
is allowed to move, which of two disagreeing classifiers wins, and which of two deletion
signals actually fires in production. Every one of those is asserted here against a real
database, because the interesting failures are persistence failures: a cursor committed
after a partially-failed run, a reminder planned against a due date that is no longer
stored, an item that never goes inactive because the reconcile only looked at `in_trash`.

Three deliberate choices:

* **The fake Notion is the real contract.** `FakeNotionClient.list_changed_pages` excludes
  trashed pages exactly as Notion's data-source query does, while `list_all_pages` includes
  them. That asymmetry is not a convenience: it is the reason FR-9 is testable at all, and
  a fake that returned trashed pages from both would make this file prove nothing.
* **No clock.** Every instant comes from `frozen_clock`, never `datetime.now()` — including
  the page timestamps, so "is this page inside the 5-minute overlap?" has one answer.
* **Setup the service cannot produce goes through the repository.** A stale course cache
  entry, a page deleted behind the sync's back, a due date read back from a second upsert:
  none of those are reachable through `run_incremental`, so they are arranged directly and
  the comment says why.

CLAUDE.md constraint 5: no test here contacts Notion, Gmail, or DeepSeek.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import FrozenClock
from app.config import Settings
from app.container import AppContainer
from app.db.models import AuditLog, Course, Item, Reminder
from app.db.repositories import courses as courses_repo
from app.db.repositories import items as items_repo
from app.db.repositories import state as state_repo
from app.domain.planner import iso_utc
from app.domain.types import NormalizedItem
from app.integrations.notion.normalize import normalize_page
from app.services.sync_service import SyncService
from fixtures.fakes import FakeNotionClient
from fixtures.pages import make_assignment, make_course_page, make_exam, make_notion_page

pytestmark = pytest.mark.integration

# 2026-10-03 is inside EDT, so a date-only due date means 11:59 PM Eastern = 03:59 UTC the
# next day (FR-1, CLAUDE.md constraint 3). Written as a literal rather than computed, so
# the expectation is visibly the spec's and not a restatement of the implementation.
DUE = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
# A later due date, for the moved-due-date case: 2026-10-10 23:59 EDT.
NEW_DUE = datetime(2026, 10, 11, 3, 59, tzinfo=UTC)


# ── Read helpers ────────────────────────────────────────────────────────────
#
# `populate_existing=True` on every one of these is load-bearing, not decoration. The
# service writes through a session of its own while these reads go through the fixture's
# session: without it, a *second* read of a row this session already loaded would hand back
# the pre-write object, and the assertion would be about a snapshot rather than about the
# database.


async def item_for(session: AsyncSession, page_id: str) -> Item:
    """The stored item for a Notion page id. Fails loudly when it is absent."""
    result = await session.execute(
        select(Item).where(Item.notion_page_id == page_id).execution_options(populate_existing=True)
    )
    item = result.scalar_one_or_none()
    assert item is not None, f"no item stored for {page_id!r}"
    return item


async def all_items(session: AsyncSession) -> list[Item]:
    result = await session.execute(
        select(Item).order_by(Item.notion_page_id).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def reminders_for(session: AsyncSession, item_id: UUID) -> list[Reminder]:
    result = await session.execute(
        select(Reminder)
        .where(Reminder.item_id == item_id)
        .order_by(Reminder.reminder_type)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def audit_events(session: AsyncSession, event_type: str) -> list[AuditLog]:
    result = await session.execute(
        select(AuditLog).where(AuditLog.event_type == event_type).order_by(AuditLog.id)
    )
    return list(result.scalars().all())


async def audit_count(session: AsyncSession, event_type: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.event_type == event_type)
    )
    total: int = result.scalar_one()
    return total


async def stored_cursor(session: AsyncSession, source_db: str) -> str | None:
    return await state_repo.get_str(session, f"notion_cursor:{source_db}")


async def cached_course_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Course))
    total: int = result.scalar_one()
    return total


# ── Pages land correctly ────────────────────────────────────────────────────


class TestPagesLand:
    async def test_each_database_produces_its_own_kind_and_a_utc_due_instant(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        notion.add(make_assignment(page_id="p-a"))
        notion.add(make_exam(page_id="p-e", data_source_id="ds-2"))

        await SyncService(app_container).run_incremental()

        assignment = await item_for(session, "p-a")
        assert assignment.item_kind == "assignment_reading"
        assert assignment.source_db == "assignments_readings"
        assert assignment.notion_data_source_id == "ds-1"
        # FR-1: date-only -> 11:59 PM in the configured zone, stored as UTC.
        assert assignment.due_at == DUE
        assert assignment.due_date == date(2026, 10, 3)
        assert assignment.due_has_time is False
        assert assignment.is_active is True

        exam = await item_for(session, "p-e")
        assert exam.item_kind == "exam_project"
        assert exam.source_db == "exams_projects"
        assert exam.notion_data_source_id == "ds-2"
        assert exam.due_at == DUE

        # The MVP never writes to Notion (CLAUDE.md constraint 6).
        assert notion.update_page_calls == []

    async def test_a_synced_item_gets_its_reminder_schedule(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        notion.add(make_assignment(page_id="p-a"))

        await SyncService(app_container).run_incremental()

        item = await item_for(session, "p-a")
        rows = await reminders_for(session, item.id)
        assert {(row.reminder_type, row.status) for row in rows} == {
            ("assignment_48h", "pending"),
            ("assignment_24h", "pending"),
        }
        assert {row.due_at_snapshot for row in rows} == {DUE}

    @pytest.mark.parametrize(
        ("status", "done"),
        [("Completed", False), ("Not started", True)],
        ids=["status-completed", "done-checked"],
    )
    async def test_either_completion_signal_alone_is_stored_and_suppresses_reminders(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        status: str,
        done: bool,
    ) -> None:
        """FR-3: `Done` and `Status` are stored separately and checked independently."""
        notion.add(make_notion_page(page_id="p-done", status=status, done=done))

        await SyncService(app_container).run_incremental()

        item = await item_for(session, "p-done")
        assert (item.status, item.done) == (status, done)
        assert await reminders_for(session, item.id) == []


# ── The cursor ──────────────────────────────────────────────────────────────


class TestCursor:
    """§2.3.2.A: the 5-minute overlap, and a cursor that only ever moves on success."""

    async def test_the_first_sync_passes_no_cursor_and_the_second_overlaps_by_five_minutes(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        # `session` is requested for its truncation, not for its queries: without it this
        # test would inherit the cursors the tests above it left behind, and the first run
        # here would not be a first run at all.
        assert await stored_cursor(session, "assignments_readings") is None

        notion.add(make_assignment(page_id="p-a"))
        service = SyncService(app_container)

        await service.run_incremental()
        # `since=None` unfilters. A filtered first query would silently miss every page
        # older than the run's own start time, so "no cursor" must mean "no filter".
        assert notion.changed_since_args() == [None, None]

        await service.run_incremental()
        overlap = frozen_clock.now() - timedelta(minutes=5)
        assert notion.changed_since_args() == [None, None, overlap, overlap]

    async def test_the_cursor_advances_to_the_sync_start_time(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        notion.add(make_assignment(page_id="p-a"))

        await SyncService(app_container).run_incremental()

        expected = iso_utc(frozen_clock.now())
        assert await stored_cursor(session, "assignments_readings") == expected
        assert await stored_cursor(session, "exams_projects") == expected

    async def test_the_data_source_id_is_resolved_once_then_reused(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        """§2.3.2.A: resolution happens once and is cached in `system_state`."""
        service = SyncService(app_container)

        await service.run_incremental()
        assert notion.resolve_calls == ["db-assignments", "db-exams"]

        await service.run_incremental()
        assert notion.resolve_calls == ["db-assignments", "db-exams"]

        assert await state_repo.get_str(session, "notion_datasource:db-assignments") == "ds-1"
        assert await state_repo.get_str(session, "notion_datasource:db-exams") == "ds-2"

    async def test_a_page_that_raises_leaves_that_cursor_alone_and_is_audited(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        """A partially-failed sync must never be able to skip pages on the next run.

        The malformed due date is the failure vehicle on purpose: it is a real Notion data
        error that `normalize_page` raises on, so the exception travels the whole real path
        instead of being injected into a mock.
        """
        notion.add(make_assignment(page_id="p-good"))
        notion.add(make_notion_page(page_id="p-bad", due_start="not-a-date"))

        with pytest.raises(ValueError):
            await SyncService(app_container).run_incremental()

        # The cursor did not move, so the next run re-reads the same window — including the
        # page that had already been written before the failure, which rolled back with it.
        assert await stored_cursor(session, "assignments_readings") is None
        assert await all_items(session) == []
        # Sources are independent: the failure in one did not stop the other.
        assert await stored_cursor(session, "exams_projects") == iso_utc(frozen_clock.now())

        failures = [
            event
            for event in await audit_events(session, "notion_sync")
            if event.result == "failed"
        ]
        assert len(failures) == 1
        assert failures[0].payload == {"source_db": "assignments_readings"}
        assert failures[0].error is not None
        assert "ValueError" in failures[0].error

    async def test_a_repeated_sync_is_completely_idempotent(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        """The second run genuinely re-reads the page — and still changes nothing.

        The page's timestamp is the cursor instant itself, which sits inside the 5-minute
        overlap, so it *is* returned a second time. Without that, the test would pass for
        the trivial reason that no page came back at all.
        """
        notion.add(make_assignment(page_id="p-a", last_edited_time=frozen_clock.now()))
        service = SyncService(app_container)

        await service.run_incremental()
        item = await item_for(session, "p-a")
        reminder_ids = [row.id for row in await reminders_for(session, item.id)]
        created_before = await audit_count(session, "reminder_created")
        assert created_before == 2

        await service.run_incremental()

        overlap = frozen_clock.now() - timedelta(minutes=5)
        assert notion.changed_since_args()[-2:] == [overlap, overlap]
        assert len(await all_items(session)) == 1
        assert [row.id for row in await reminders_for(session, item.id)] == reminder_ids
        assert await audit_count(session, "reminder_created") == created_before


# ── Course name resolution (§1.2) ───────────────────────────────────────────


class TestCourseResolution:
    async def test_a_cache_miss_fetches_the_course_page_and_caches_its_title(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        notion.add(make_course_page(page_id="course-1", title="Microeconomics"))
        notion.add(make_assignment(page_id="p-a", course_page_id="course-1"))

        await SyncService(app_container).run_incremental()

        item = await item_for(session, "p-a")
        assert item.course_page_id == "course-1"
        assert item.course == "Microeconomics"
        assert [call.page_id for call in notion.get_page_calls] == ["course-1"]
        assert (
            await courses_repo.get_cached(session, "course-1", ttl_hours=24, now=frozen_clock.now())
            == "Microeconomics"
        )

    async def test_a_fresh_cache_entry_avoids_the_notion_call(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        # Seeded through the repository rather than by a previous sync: the entry has to
        # exist *before* the sync for a cache hit to mean anything.
        await courses_repo.upsert(session, "course-1", "Microeconomics", frozen_clock.now())
        await session.commit()
        notion.add(make_assignment(page_id="p-a", course_page_id="course-1"))

        await SyncService(app_container).run_incremental()

        assert notion.get_page_calls == []
        assert (await item_for(session, "p-a")).course == "Microeconomics"

    async def test_an_entry_older_than_the_ttl_is_refetched(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
        frozen_clock: FrozenClock,
    ) -> None:
        # One hour past the 24-hour default TTL that the service reads from settings.
        stale = frozen_clock.now() - timedelta(hours=25)
        await courses_repo.upsert(session, "course-1", "Old Title", stale)
        await session.commit()
        notion.add(make_course_page(page_id="course-1", title="New Title"))
        notion.add(make_assignment(page_id="p-a", course_page_id="course-1"))

        await SyncService(app_container).run_incremental()

        assert [call.page_id for call in notion.get_page_calls] == ["course-1"]
        item = await item_for(session, "p-a")
        assert item.course == "New Title"
        assert (
            await courses_repo.get_cached(session, "course-1", ttl_hours=24, now=frozen_clock.now())
            == "New Title"
        )

    async def test_an_empty_relation_leaves_both_fields_none(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        notion.add(make_assignment(page_id="p-a", course_page_id=""))

        await SyncService(app_container).run_incremental()

        item = await item_for(session, "p-a")
        assert item.course_page_id is None
        assert item.course is None
        assert notion.get_page_calls == []
        assert await cached_course_count(session) == 0


# ── type_mismatch (§1.2) ────────────────────────────────────────────────────


class TestTypeMismatch:
    async def test_a_disagreeing_type_select_is_audited_and_the_database_wins(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        # An "Exam" sitting in the assignments database, built with `make_notion_page`
        # because `make_assignment` pins `type_name` to the agreeing value.
        notion.add(make_notion_page(page_id="p-a", type_name="Exam"))

        await SyncService(app_container).run_incremental()

        item = await item_for(session, "p-a")
        assert item.item_kind == "assignment_reading"  # the database, never the select
        assert item.notion_type == "Exam"  # kept as metadata

        events = await audit_events(session, "type_mismatch")
        assert len(events) == 1
        assert events[0].notion_page_id == "p-a"
        assert events[0].item_id == item.id
        assert events[0].payload == {
            "source_db": "assignments_readings",
            "item_kind": "assignment_reading",
            "notion_type": "Exam",
        }

    async def test_an_agreeing_type_select_is_silent(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        notion.add(make_assignment(page_id="p-a"))
        notion.add(make_exam(page_id="p-e", data_source_id="ds-2"))

        await SyncService(app_container).run_incremental()

        assert await audit_events(session, "type_mismatch") == []


# ── Full reconcile and FR-9 ─────────────────────────────────────────────────


class TestFullReconcile:
    async def _sync_one_page(
        self, app_container: AppContainer, notion: FakeNotionClient, page_id: str = "p-a"
    ) -> SyncService:
        """Put one assignment in the local mirror, ready to be deleted behind its back."""
        notion.add(make_assignment(page_id=page_id))
        service = SyncService(app_container)
        await service.run_incremental()
        return service

    async def test_a_page_absent_from_the_results_is_deactivated(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
    ) -> None:
        """The main path. A real query never returns a trashed page, so absence is the signal."""
        service = await self._sync_one_page(app_container, notion)
        item = await item_for(session, "p-a")
        assert {row.status for row in await reminders_for(session, item.id)} == {"pending"}

        # The page is deleted in Notion: it simply stops appearing in any query result.
        notion.pages = [page for page in notion.pages if page.page_id != "p-a"]

        await service.run_full_reconcile()

        stored = await item_for(session, "p-a")
        assert stored.is_active is False
        rows = await reminders_for(session, stored.id)
        assert len(rows) == 2  # FR-9: skipped, never deleted — sent history stays readable
        assert {row.status for row in rows} == {"skipped"}
        assert {row.skip_reason for row in rows} == {"item_inactive"}

        events = await audit_events(session, "item_deactivated")
        assert len(events) == 1
        assert events[0].notion_page_id == "p-a"
        assert events[0].item_id == stored.id
        assert events[0].payload == {"source_db": "assignments_readings", "reason": "absent"}

    async def test_a_page_returned_with_in_trash_is_deactivated(
        self,
        session: AsyncSession,
        app_container: AppContainer,
        notion: FakeNotionClient,
    ) -> None:
        """The secondary signal — the page does come back, carrying `in_trash: true`."""
        service = await self._sync_one_page(app_container, notion)

        notion.add(make_notion_page(page_id="p-a", in_trash=True))

        await service.run_full_reconcile()

        stored = await item_for(session, "p-a")
        assert stored.is_active is False
        rows = await reminders_for(session, stored.id)
        assert {row.status for row in rows} == {"skipped"}
        assert {row.skip_reason for row in rows} == {"item_inactive"}

        events = await audit_events(session, "item_deactivated")
        assert len(events) == 1
        assert events[0].payload == {"source_db": "assignments_readings", "reason": "in_trash"}

    async def test_both_databases_are_read_in_full_and_the_pass_is_audited(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        service = await self._sync_one_page(app_container, notion)

        await service.run_full_reconcile()

        assert notion.list_all_calls == ["ds-1", "ds-2"]
        events = await audit_events(session, "notion_full_reconcile")
        assert len(events) == 2
        by_source = {event.payload["source_db"]: event for event in events}
        assert by_source["assignments_readings"].payload["pages"] == 1
        assert by_source["assignments_readings"].payload["deactivated"] == 0
        assert by_source["exams_projects"].payload["pages"] == 0

    async def test_a_live_page_is_left_alone(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        service = await self._sync_one_page(app_container, notion)

        await service.run_full_reconcile()

        stored = await item_for(session, "p-a")
        assert stored.is_active is True
        assert {row.status for row in await reminders_for(session, stored.id)} == {"pending"}
        assert await audit_events(session, "item_deactivated") == []

    async def test_a_second_full_reconcile_changes_nothing(
        self, session: AsyncSession, app_container: AppContainer, notion: FakeNotionClient
    ) -> None:
        service = await self._sync_one_page(app_container, notion)
        notion.pages = [page for page in notion.pages if page.page_id != "p-a"]

        await service.run_full_reconcile()
        await service.run_full_reconcile()

        assert len(await audit_events(session, "item_deactivated")) == 1


# ── The stale-ORM trap ──────────────────────────────────────────────────────


class TestRepeatedUpsertInOneSession:
    """Upserting the same page twice in one session must not plan against a stale due date.

    `items.upsert` writes through a Core statement and then re-selects the row. When an
    object for that page is already in the session's identity map, SQLAlchemy refreshes
    only its *unloaded* attributes and the Core write does not expire it — so the second
    call can hand back the **previous** `due_at`. `reconcile_reminders` would then plan the
    schedule against a due date that is no longer stored, which is a silently wrong
    reminder rather than a visible error. `_upsert_page` refreshes for exactly this reason,
    and these tests are what fail if that refresh is ever removed.
    """

    async def test_a_changed_due_date_supersedes_the_old_schedule_and_creates_the_new_one(
        self, session: AsyncSession, app_container: AppContainer, frozen_clock: FrozenClock
    ) -> None:
        service = SyncService(app_container)
        first = make_assignment(page_id="p-a", due_start="2026-10-03")
        moved = make_assignment(page_id="p-a", due_start="2026-10-10")

        # Both upserts deliberately share one session: that is the shape which trips the
        # identity-map behaviour. Driving them through the service's own per-page path
        # (rather than the repository directly) is what proves the guard sits where it
        # needs to — inside the code that every real sync goes through.
        async with app_container.session_factory() as work:
            now = frozen_clock.now()
            await service._upsert_page(work, first, "assignments_readings", now)
            await service._upsert_page(work, moved, "assignments_readings", now)
            await work.commit()

        item = await item_for(session, "p-a")
        assert item.due_at == NEW_DUE
        assert item.due_date == date(2026, 10, 10)

        rows = await reminders_for(session, item.id)
        assert len(rows) == 4  # a new due date is a new key; the old rows stay as history
        old = [row for row in rows if row.due_at_snapshot == DUE]
        new = [row for row in rows if row.due_at_snapshot == NEW_DUE]
        assert {row.status for row in old} == {"superseded"}
        assert {row.status for row in new} == {"pending"}
        assert {row.target_at for row in new} == {
            NEW_DUE - timedelta(hours=48),
            NEW_DUE - timedelta(hours=24),
        }

    async def test_the_same_page_twice_with_no_change_is_still_silent(
        self, session: AsyncSession, app_container: AppContainer, frozen_clock: FrozenClock
    ) -> None:
        service = SyncService(app_container)
        page = make_assignment(page_id="p-a")

        async with app_container.session_factory() as work:
            now = frozen_clock.now()
            await service._upsert_page(work, page, "assignments_readings", now)
            await service._upsert_page(work, page, "assignments_readings", now)
            await work.commit()

        item = await item_for(session, "p-a")
        assert len(await all_items(session)) == 1
        assert len(await reminders_for(session, item.id)) == 2

    async def test_a_read_item_is_not_refreshed_by_a_later_upsert(
        self, session: AsyncSession, settings: Settings
    ) -> None:
        """The trap at its source — the reason `_upsert_page` refreshes.

        A characterisation test of the repository plus SQLAlchemy, **not** a rule the
        application relies on. `items.upsert` writes through a Core statement and re-selects
        the row; when an object for that page is already in the session's identity map and
        its attributes are loaded, that SELECT hands the object back untouched. So a second
        upsert in the same session returns the row as it was *before* the write, while the
        database holds the new value — exactly the disagreement that would let
        `reconcile_reminders` plan against a due date that is no longer stored.

        Reading `first.due_at` is what makes this deterministic: it loads the object, and a
        loaded, clean object is what SQLAlchemy declines to overwrite. Reproduced 3/3 at the
        time of writing.

        If a future SQLAlchemy synchronises the identity map here, this test fails — and the
        right response is to delete it and treat the refresh in `_upsert_page` as
        belt-and-braces. It is kept because "the object handed to the planner is the row in
        the database" is a guarantee the reminder schedule depends on, and the refresh is
        what makes it unconditional instead of dependent on identity-map behaviour.
        """

        def normalized(due_start: str) -> NormalizedItem:
            return normalize_page(
                make_assignment(page_id="p-a", due_start=due_start),
                source_db="assignments_readings",
                tz=settings.tz,
                default_due_time=settings.default_due_time,
            )

        first = await items_repo.upsert(session, normalized("2026-10-03"))
        assert first.due_at == DUE  # a read: from here the object is loaded and clean

        second = await items_repo.upsert(session, normalized("2026-10-10"))

        # Asserted before `item_for`, which would refresh this very object from the database.
        assert second is first
        assert second.due_at == DUE  # the previous row, not the one just written
        assert (await item_for(session, "p-a")).due_at == NEW_DUE  # the database is correct
