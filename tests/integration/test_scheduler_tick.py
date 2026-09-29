"""`ReminderService` against a real Postgres — the tick, the send, and recovery.

`tests/unit/` proves the pure decisions; this module proves the part that can only be
proved against a database and a fake inbox: that a claimed reminder is sent **once**, that
the guards stop the ones that must not go out, that a failure retries on a schedule and
then fails loudly, that a crash between claim and send is resolved by asking Gmail, and that
the outbound cap pauses sending without losing anything.

Every test drives the real service over the real repositories with fake ports
(`FakeNotionClient`, `FakeMailClient`) and a `FrozenClock` (CLAUDE.md constraints 4 and 5).
No test calls a live service, and none reads the system clock.

Two harness details worth knowing before editing:

* **`TEST_DATABASE_URL` is what isolates a run.** `migrated_database` creates and migrates
  whatever that variable names; the module-level `engine` fixture below binds the engine to
  the same URL. Without the override the engine would use the literal database name in
  `tests/conftest.py`'s `settings` fixture, and a run with `TEST_DATABASE_URL` set would
  migrate one database and then talk to another.
* **A reminder is only sent if its page is still live in the fake Notion.** The pre-send
  check treats a missing page as "gone" (FR-9), so a test that expects an email registers
  the page with `add_live_page`. A test that wants "Notion says nothing" simply leaves the
  fake empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.container import AppContainer
from app.db.models import AuditLog, EmailThread, Item, OutboundMessage, Reminder
from app.db.repositories import items as items_repo
from app.db.repositories import reminders as repo
from app.db.repositories import state as state_repo
from app.db.session import create_session_factory
from app.domain.planner import PlannerPolicy
from app.domain.types import NormalizedItem
from app.integrations.gmail.compose import reminder_subject, render_reminder
from app.integrations.notion.normalize import normalize_page
from app.services.alert_service import ALERT_TYPES, AlertService, last_alert_key
from app.services.reminder_service import (
    ReminderService,
    backoff_delay,
    outbound_counter_key,
)
from fixtures import pages
from fixtures.fakes import FakeMailClient, FakeNotionClient, make_container

pytestmark = pytest.mark.integration

POLICY = PlannerPolicy(completed_status_value="Completed")

# Due 2026-10-03 23:59 Eastern (EDT) = 2026-10-04 03:59 UTC — the case CLAUDE.md
# constraint 3 calls out. The two assignment targets follow from it (FR-2).
DUE = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
T48 = DUE - timedelta(hours=48)  # 2026-10-02 03:59 UTC
T24 = DUE - timedelta(hours=24)  # 2026-10-03 03:59 UTC

STALE = timedelta(minutes=11)  # past `STALE_CLAIM_AFTER` (10 minutes)

REMINDER_SUBJECT_PREFIX = "[Reminder] "
ALERT_SUBJECT_PREFIX = "[Alert] "


@pytest.fixture
def service(app_container: AppContainer) -> ReminderService:
    """The service under test, wired to the fake ports and the real database."""
    return ReminderService(app_container)


# ── Arranging an item and its reminders ─────────────────────────────────────


def normalized_item(
    *,
    page_id: str,
    due_at: datetime | None = DUE,
    status: str = "Not started",
    done: bool = False,
    in_trash: bool = False,
    name: str = "Problem Set 4",
    course: str | None = "CS 101",
    course_page_id: str | None = "course-1",
) -> NormalizedItem:
    """The stored shape of one item, built directly rather than through a fake page.

    `tests/integration/test_reconcile_reminders.py` builds the same thing inline; the
    parameters here are the ones the scheduler's guards act on.
    """
    return NormalizedItem(
        notion_page_id=page_id,
        notion_data_source_id="ds-1",
        source_db="assignments_readings",
        item_kind="assignment_reading",
        notion_type="Assignment",
        name=name,
        course_page_id=course_page_id,
        course=course,
        status=status,
        done=done,
        due_date=due_at.date() if due_at is not None else None,
        due_at=due_at,
        due_has_time=False,
        timezone="America/New_York",
        notion_url=f"https://notion.so/{page_id}",
        notion_last_edited_time=None,
        in_trash=in_trash,
    )


async def arrange(
    session: AsyncSession,
    clock: Any,
    *,
    page_id: str = "page-tick-1",
    **overrides: Any,
) -> Item:
    """Store an item and reconcile its reminders, committed and ready for a tick.

    Reconciliation happens at `clock.now()` — the frozen instant both targets are still
    ahead of, so a fresh item starts with two `pending` rows. Every test then moves the
    clock to the target whose guard it is exercising.
    """
    item = await items_repo.upsert(session, normalized_item(page_id=page_id, **overrides))
    await repo.reconcile_reminders(session, item, clock.now(), POLICY)
    await session.commit()
    await session.refresh(item)
    return item


def add_live_page(
    notion: FakeNotionClient,
    *,
    page_id: str,
    status: str = "Not started",
    done: bool = False,
    in_trash: bool = False,
    due_start: str = "2026-10-03",
    name: str = "Problem Set 4",
) -> None:
    """Register the item's page as the pre-send check will see it."""
    notion.add(
        pages.make_assignment(
            page_id=page_id,
            name=name,
            due_start=due_start,
            status=status,
            done=done,
            in_trash=in_trash,
        )
    )


# ── Reading what happened ───────────────────────────────────────────────────


async def load_item(session: AsyncSession, item_id: Any) -> Item:
    """The item as stored, refreshed past any identity-map copy the test already holds."""
    result = await session.execute(
        select(Item).where(Item.id == item_id).execution_options(populate_existing=True)
    )
    item: Item = result.scalars().one()
    return item


async def load_reminders(session: AsyncSession, item_id: Any) -> list[Reminder]:
    """The item's reminders, earliest target first, refreshed from the database."""
    result = await session.execute(
        select(Reminder)
        .where(Reminder.item_id == item_id)
        .execution_options(populate_existing=True)
        .order_by(Reminder.target_at)
    )
    rows: list[Reminder] = list(result.scalars().all())
    return rows


async def audits(session: AsyncSession, event_type: str, *, item_id: Any = None) -> list[AuditLog]:
    """Audit rows of one type, oldest first, optionally for one item."""
    query = select(AuditLog).where(AuditLog.event_type == event_type).order_by(AuditLog.id)
    if item_id is not None:
        query = query.where(AuditLog.item_id == item_id)
    result = await session.execute(query.execution_options(populate_existing=True))
    return list(result.scalars().all())


def reminder_emails(mail: FakeMailClient) -> list[Any]:
    """Only the reminder emails — alerts also travel through the same mail port."""
    return [sent for sent in mail.sent if sent.subject.startswith(REMINDER_SUBJECT_PREFIX)]


def alert_emails(mail: FakeMailClient) -> list[Any]:
    return [sent for sent in mail.sent if sent.subject.startswith(ALERT_SUBJECT_PREFIX)]


async def counter(session: AsyncSession, key: str) -> Any:
    return await state_repo.get_json(session, key, None)


# ── The happy path, exactly once (FR-5, FR-7) ───────────────────────────────


class TestOneReminder:
    async def test_a_due_reminder_is_claimed_sent_and_recorded_exactly_once(
        self,
        session: AsyncSession,
        settings: Settings,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id)

        frozen_clock.set(T48)
        assert await service.tick() == 1

        # ── The email ──
        assert len(reminder_emails(mail)) == 1
        sent_email = reminder_emails(mail)[0]
        assert sent_email.to == settings.reminder_recipient
        # One item per email, and a new thread for each reminder (FR-6).
        assert sent_email.thread_id is None
        assert sent_email.in_reply_to is None
        assert sent_email.subject == "[Reminder] CS 101: Problem Set 4, due Sat Oct 3 (in 48 hours)"

        # ── The reminder row ──
        rows = await load_reminders(session, item.id)
        by_type = {row.reminder_type: row for row in rows}
        first = by_type["assignment_48h"]
        assert first.status == "sent"
        assert first.sent_at == T48
        assert first.attempt_count == 1
        assert first.provider_message_id == "fake-message-1"
        assert first.provider_thread_id == "fake-thread-1"
        # The 24h target is still in the future and untouched.
        assert by_type["assignment_24h"].status == "pending"

        # The footer is the stale-claim recovery key (§2.3.2.C), so it must be in the body.
        assert f"ref: {first.ref_token}" in sent_email.body

        # ── The thread, for the V1 reply mapping (§2.3.2.E step 3) ──
        thread = (await session.execute(select(EmailThread))).scalars().one()
        assert thread.item_id == item.id
        assert thread.reminder_id == first.id
        assert thread.provider_thread_id == "fake-thread-1"
        assert thread.root_rfc_message_id == "<fake-message-1@fake.invalid>"
        assert thread.subject == sent_email.subject

        outbound = (await session.execute(select(OutboundMessage))).scalars().one()
        assert outbound.thread_id == thread.id
        assert outbound.kind == "reminder"
        assert outbound.provider_message_id == "fake-message-1"
        assert outbound.rfc_message_id == "<fake-message-1@fake.invalid>"

        # ── The audit trail ──
        sent_audits = await audits(session, "reminder_sent", item_id=item.id)
        assert len(sent_audits) == 1
        assert sent_audits[0].provider_message_id == "fake-message-1"

        # ── The cap counter, keyed on the UTC hour of the send ──
        assert await counter(session, outbound_counter_key(T48)) == 1

    async def test_a_second_tick_sends_nothing(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """FR-7: a tick that finds nothing due is a no-op, not a second copy."""
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id)
        frozen_clock.set(T48)

        assert await service.tick() == 1
        assert await service.tick() == 0

        assert len(reminder_emails(mail)) == 1
        assert (
            await session.execute(select(func.count()).select_from(EmailThread))
        ).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(OutboundMessage))
        ).scalar_one() == 1
        assert len(await audits(session, "reminder_sent", item_id=item.id)) == 1
        rows = {row.reminder_type: row for row in await load_reminders(session, item.id)}
        assert rows["assignment_48h"].status == "sent"


# ── Guards: completed, inactive, past due (FR-3, FR-4, FR-9) ─────────────────


class TestGuards:
    async def test_a_completed_item_is_skipped_and_nothing_is_sent(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        item = await arrange(session, frozen_clock)
        # The item is completed between the sync and the tick. Deliberately NOT reconciled:
        # the scheduler's own guard is what this test is about.
        await items_repo.upsert(
            session, normalized_item(page_id=item.notion_page_id, status="Completed", done=True)
        )
        await session.commit()
        frozen_clock.set(T48)

        assert await service.tick() == 0

        assert reminder_emails(mail) == []
        rows = {row.reminder_type: row for row in await load_reminders(session, item.id)}
        assert rows["assignment_48h"].status == "skipped"
        assert rows["assignment_48h"].skip_reason == "item_completed"
        skipped = await audits(session, "reminder_skipped", item_id=item.id)
        assert [row.payload["skip_reason"] for row in skipped] == ["item_completed"]
        assert skipped[0].payload["source"] == "scheduler"
        assert await audits(session, "reminder_sent", item_id=item.id) == []

    async def test_an_inactive_item_is_skipped_and_nothing_is_sent(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """FR-9 — archived in Notion, and the local row already knows it."""
        item = await arrange(session, frozen_clock)
        await items_repo.upsert(
            session, normalized_item(page_id=item.notion_page_id, in_trash=True)
        )
        await session.commit()
        frozen_clock.set(T48)

        assert await service.tick() == 0

        assert reminder_emails(mail) == []
        rows = {row.reminder_type: row for row in await load_reminders(session, item.id)}
        assert rows["assignment_48h"].status == "skipped"
        assert rows["assignment_48h"].skip_reason == "item_inactive"

    async def test_a_reminder_whose_due_time_has_passed_is_skipped(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """FR-4: nothing is sent once `now >= due_at`, however claimable the row is."""
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id)
        frozen_clock.set(DUE)

        assert await service.tick() == 0

        assert reminder_emails(mail) == []
        rows = await load_reminders(session, item.id)
        assert {row.status for row in rows} == {"skipped"}
        assert {row.skip_reason for row in rows} == {"past_due"}
        # The live page was never consulted: the guard runs before any network call.
        assert notion.get_page_calls == []


# ── Guard 3: two targets, one email (§2.3.2.C) ──────────────────────────────


class TestSupersededByLater:
    async def test_only_the_closest_target_sends_after_downtime(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """Both targets fell during downtime; exactly one email goes out, the later one."""
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id)
        frozen_clock.set(T24)  # both T48 and T24 are now in the past

        assert await service.tick() == 1

        emails = reminder_emails(mail)
        assert len(emails) == 1
        assert "(in 24 hours)" in emails[0].subject

        rows = {row.reminder_type: row for row in await load_reminders(session, item.id)}
        assert rows["assignment_48h"].status == "skipped"
        assert rows["assignment_48h"].skip_reason == "superseded_by_later"
        assert rows["assignment_24h"].status == "sent"


# ── Guard 6: the pre-send check (§2.3.2.C) ──────────────────────────────────


class TestPreSendCheck:
    async def test_a_page_completed_since_the_sync_is_not_emailed(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id, status="Completed", done=True)
        frozen_clock.set(T48)

        assert await service.tick() == 0

        assert reminder_emails(mail) == []
        updated = await load_item(session, item.id)
        assert updated.status == "Completed"
        assert updated.done is True
        rows = await load_reminders(session, item.id)
        # `reconcile_reminders` ran, so every in-flight row is cancelled as completed.
        assert {row.status for row in rows} == {"skipped"}
        assert {row.skip_reason for row in rows} == {"item_completed"}
        assert len(await audits(session, "reminder_skipped", item_id=item.id)) == 2

    async def test_a_page_archived_since_the_sync_is_not_emailed(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id, in_trash=True)
        frozen_clock.set(T48)

        assert await service.tick() == 0

        assert reminder_emails(mail) == []
        updated = await load_item(session, item.id)
        assert updated.is_active is False
        rows = await load_reminders(session, item.id)
        assert {row.status for row in rows} == {"skipped"}
        assert {row.skip_reason for row in rows} == {"item_inactive"}

    async def test_a_page_that_no_longer_exists_deactivates_the_item_locally(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """`get_page` -> None is a 404: there is no page to re-upsert, only an item to stop."""
        item = await arrange(session, frozen_clock)
        frozen_clock.set(T48)

        assert await service.tick() == 0

        assert reminder_emails(mail) == []
        assert (await load_item(session, item.id)).is_active is False
        rows = await load_reminders(session, item.id)
        assert {row.skip_reason for row in rows} == {"item_inactive"}

    async def test_a_notion_outage_proceeds_with_local_data_and_audits_the_skip(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """The one thing the pre-send check must never do is swallow a reminder."""
        item = await arrange(session, frozen_clock)
        notion.raise_on_get_page = True
        frozen_clock.set(T48)

        assert await service.tick() == 1

        assert len(reminder_emails(mail)) == 1
        rows = {row.reminder_type: row for row in await load_reminders(session, item.id)}
        assert rows["assignment_48h"].status == "sent"
        skipped = await audits(session, "presend_check_skipped", item_id=item.id)
        assert len(skipped) == 1
        assert skipped[0].payload["reason"] == "FakeNotionUnreachable"
        assert skipped[0].error is not None


# ── Send failures: backoff, then the budget, then an alert (§2.3.2.C, FR-13) ─


class TestSendFailure:
    async def test_four_failures_back_off_and_the_fifth_claim_fails_with_one_alert(
        self,
        session: AsyncSession,
        settings: Settings,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        item = await arrange(session, frozen_clock)
        add_live_page(notion, page_id=item.notion_page_id)
        mail.fail_times = 4  # the first four attempts fail; a fifth would have succeeded
        frozen_clock.set(T48)

        observed: list[float] = []
        for attempt in range(1, 5):
            assert await service.tick() == 0
            row = (await load_reminders(session, item.id))[0]
            assert row.status == "pending"
            assert row.attempt_count == attempt
            assert row.claimed_at is None
            assert row.last_error is not None
            assert row.next_attempt_at is not None
            delay = (row.next_attempt_at - frozen_clock.now()).total_seconds()
            observed.append(delay)
            # 30s, then 60s, 120s, 240s — each within ±20%, so strictly increasing.
            base = 30.0 * 2 ** (attempt - 1)
            assert 0.8 * base <= delay <= 1.2 * base
            frozen_clock.set(row.next_attempt_at)  # the retry becomes due

        assert observed == sorted(observed)
        assert mail.send_attempts == 4

        # The fifth claim has no attempt left: terminal, audited, and alerted.
        assert await service.tick() == 0

        row = (await load_reminders(session, item.id))[0]
        assert row.status == "failed"
        assert row.attempt_count == 5
        assert row.last_error is not None
        # The reminder itself was attempted four times and never a fifth; the one further
        # transfer through the mail port is the alert (asserted below).
        assert mail.send_attempts == 5
        assert reminder_emails(mail) == []

        failed = await audits(session, "reminder_failed", item_id=item.id)
        assert len(failed) == 1
        assert failed[0].payload["attempt_count"] == 5

        alerts = await audits(session, "system_alert_sent")
        assert len(alerts) == 1
        assert alerts[0].payload["alert_type"] == "reminder_send_failed"
        alert_messages = alert_emails(mail)
        assert len(alert_messages) == 1
        assert alert_messages[0].to == settings.reminder_recipient
        assert alert_messages[0].thread_id is None

    def test_the_backoff_ladder_is_bounded_and_monotonic(self) -> None:
        """The four delays stay inside the window the "about 5 minutes" criterion needs."""
        for attempt in range(1, 5):
            delay = backoff_delay(attempt).total_seconds()
            base = 30.0 * 2 ** (attempt - 1)
            assert 0.8 * base <= delay <= 1.2 * base
        # The cap holds the ladder at ten minutes however far it is extended.
        assert backoff_delay(20).total_seconds() <= 600 * 1.2


# ── Stale-claim recovery (§2.3.2.C) ─────────────────────────────────────────


class TestStaleClaimRecovery:
    async def test_a_sent_email_is_confirmed_and_an_unsent_one_returns_to_pending(
        self,
        session: AsyncSession,
        settings: Settings,
        frozen_clock: Any,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        item_a = await arrange(session, frozen_clock, page_id="page-stale-a")
        item_b = await arrange(session, frozen_clock, page_id="page-stale-b")
        rows_a = {row.reminder_type: row for row in await load_reminders(session, item_a.id)}
        row_b = (await load_reminders(session, item_b.id))[0]

        # Item A's email really did go out — composed with the real template, so the
        # `ref: <token>` footer that recovery searches for is the one the app actually
        # writes — but the process died before any bookkeeping committed.
        view = normalize_page(
            pages.make_assignment(page_id="page-stale-a", name="Problem Set 4"),
            source_db="assignments_readings",
            tz=settings.tz,
            default_due_time=settings.default_due_time,
        )
        subject, body = render_reminder(
            view, "assignment_48h", settings.tz, rows_a["assignment_48h"].ref_token
        )
        really_sent = await mail.send(
            to=settings.reminder_recipient,
            subject=subject,
            body=body,
            thread_id=None,
            in_reply_to=None,
        )

        # Both rows are left in exactly the state a crash between claim and outcome leaves:
        # `claimed`, held past the staleness horizon, with no recorded result.
        stale_at = frozen_clock.now() - STALE
        await session.execute(
            update(Reminder)
            .where(Reminder.id.in_([rows_a["assignment_48h"].id, row_b.id]))
            .values(
                status="claimed",
                claimed_at=stale_at,
                attempt_count=1,
                next_attempt_at=None,
                sent_at=None,
                provider_message_id=None,
                provider_thread_id=None,
                last_error=None,
            )
        )
        await session.commit()

        assert await service.recover_stale_claims() == 1

        recovered = (await load_reminders(session, item_a.id))[0]
        assert recovered.status == "sent"
        assert recovered.provider_message_id == really_sent.provider_message_id
        assert recovered.provider_thread_id == really_sent.provider_thread_id

        returned = (await load_reminders(session, item_b.id))[0]
        assert returned.status == "pending"
        assert returned.claimed_at is None
        assert returned.next_attempt_at == frozen_clock.now()
        assert returned.last_error is not None
        # The interrupted claim already spent one attempt; returning the row adds none.
        assert returned.attempt_count == 1

        sent_audits = await audits(session, "reminder_sent", item_id=item_a.id)
        assert len(sent_audits) == 1
        assert sent_audits[0].payload["recovered_from_stale_claim"] is True
        assert await audits(session, "reminder_sent", item_id=item_b.id) == []

    async def test_a_recovered_send_writes_the_thread_mapping_and_survives_a_second_sweep(
        self,
        session: AsyncSession,
        settings: Settings,
        frozen_clock: Any,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """A recovered reminder must be answerable, and the backfill must be repeatable.

        The crashed send never reached `_record_sent`, so without the backfill no
        `email_threads` row would name the Gmail thread and V1 would audit a reply to this
        reminder `inbound_unmapped` (§2.3.2.E step 3). The ids written must be the ones
        Gmail reported, not recomposed ones — that is the whole point of asking Gmail.
        """
        page_id = "page-stale-d"
        item = await arrange(session, frozen_clock, page_id=page_id)
        row = {r.reminder_type: r for r in await load_reminders(session, item.id)}["assignment_48h"]

        view = normalize_page(
            pages.make_assignment(page_id=page_id, name="Problem Set 4"),
            source_db="assignments_readings",
            tz=settings.tz,
            default_due_time=settings.default_due_time,
        )
        subject, body = render_reminder(view, "assignment_48h", settings.tz, row.ref_token)
        really_sent = await mail.send(
            to=settings.reminder_recipient,
            subject=subject,
            body=body,
            thread_id=None,
            in_reply_to=None,
        )

        # The state a crash between claim and outcome leaves behind.
        await session.execute(
            update(Reminder)
            .where(Reminder.id == row.id)
            .values(
                status="claimed",
                claimed_at=frozen_clock.now() - STALE,
                attempt_count=1,
                next_attempt_at=None,
                sent_at=None,
                provider_message_id=None,
                provider_thread_id=None,
                last_error=None,
            )
        )
        await session.commit()

        assert await service.recover_stale_claims() == 1

        threads = list(
            (
                await session.execute(select(EmailThread).where(EmailThread.item_id == item.id))
            ).scalars()
        )
        assert len(threads) == 1
        thread = threads[0]
        assert thread.provider_thread_id == really_sent.provider_thread_id
        assert thread.reminder_id == row.id
        assert thread.root_rfc_message_id == really_sent.rfc_message_id
        # Recomposed from the stored item, exactly as `_record_sent` would have stored it.
        assert thread.subject == reminder_subject(
            normalized_item(page_id=page_id), "assignment_48h", settings.tz
        )

        messages = list(
            (
                await session.execute(
                    select(OutboundMessage).where(OutboundMessage.thread_id == thread.id)
                )
            ).scalars()
        )
        assert len(messages) == 1
        assert messages[0].provider_message_id == really_sent.provider_message_id
        assert messages[0].rfc_message_id == really_sent.rfc_message_id
        assert messages[0].kind == "reminder"

        # A second sweep sees nothing (`sent` is not `claimed`), so it neither raises nor
        # duplicates.
        assert await service.recover_stale_claims() == 0

        # And the backfill's own guard: force the row back to `claimed`, as a recovery whose
        # commit was lost would leave it, and sweep again. The rows already exist, so the
        # lookup must short-circuit rather than let the UNIQUE constraints throw.
        await session.execute(
            update(Reminder)
            .where(Reminder.id == row.id)
            .values(status="claimed", claimed_at=frozen_clock.now() - STALE)
        )
        await session.commit()
        assert await service.recover_stale_claims() == 1
        thread_count = await session.scalar(
            select(func.count()).select_from(EmailThread).where(EmailThread.item_id == item.id)
        )
        message_count = await session.scalar(
            select(func.count())
            .select_from(OutboundMessage)
            .where(OutboundMessage.thread_id == thread.id)
        )
        assert thread_count == 1
        assert message_count == 1

    async def test_a_fresh_claim_is_not_touched(
        self,
        session: AsyncSession,
        frozen_clock: Any,
        mail: FakeMailClient,
        service: ReminderService,
    ) -> None:
        """Only rows past the 10-minute horizon are swept; a live claim is left alone."""
        item = await arrange(session, frozen_clock, page_id="page-stale-c")
        await session.execute(
            update(Reminder)
            .where(Reminder.item_id == item.id)
            .values(status="claimed", claimed_at=frozen_clock.now(), attempt_count=1)
        )
        await session.commit()

        assert await service.recover_stale_claims() == 0
        assert (await load_reminders(session, item.id))[0].status == "claimed"
        assert mail.sent == []


# ── The outbound cap (§2.3.2.D, FR-13) ──────────────────────────────────────


class TestOutboundCap:
    async def test_sends_stop_at_the_cap_the_reminder_survives_and_the_alert_fires_once(
        self,
        session: AsyncSession,
        settings: Settings,
        engine: AsyncEngine,
        frozen_clock: Any,
        notion: FakeNotionClient,
        mail: FakeMailClient,
    ) -> None:
        """The cap is a pause, not a failure: the held reminder is still `pending`.

        `max_outbound_emails_per_hour` is lowered by injecting settings — never by editing
        the configuration default.
        """
        capped = settings.model_copy(update={"max_outbound_emails_per_hour": 1})
        container = make_container(
            settings=capped,
            clock=frozen_clock,
            session_factory=create_session_factory(engine),
            notion=notion,
            mail=mail,
        )
        service = ReminderService(container)

        items = [
            await arrange(session, frozen_clock, page_id=f"page-cap-{index}") for index in range(3)
        ]
        for item in items:
            add_live_page(notion, page_id=item.notion_page_id)
        frozen_clock.set(T48)

        # One send, then the next claim is refused by the cap and the tick stops.
        assert await service.tick() == 1
        assert len(reminder_emails(mail)) == 1

        first_hour = outbound_counter_key(T48)
        assert await counter(session, first_hour) == 1

        held = [
            row
            for item in items
            for row in await load_reminders(session, item.id)
            if row.next_attempt_at is not None and row.status == "pending"
        ]
        assert len(held) == 1
        assert held[0].last_error is not None and "cap" in held[0].last_error
        assert held[0].claimed_at is None
        # Not lost, and not marked failed: it is waiting for the next tick.
        assert held[0].attempt_count == 1

        alerts = await audits(session, "system_alert_sent")
        assert len(alerts) == 1
        assert alerts[0].payload["alert_type"] == "outbound_cap_exceeded"
        assert len(alert_emails(mail)) == 1

        # Still inside the same UTC hour, so the cap still holds — and the alert is
        # suppressed by the cooldown rather than sent a second time (FR-13).
        frozen_clock.set(T48 + timedelta(seconds=30))
        assert await service.tick() == 0
        assert len(reminder_emails(mail)) == 1
        assert len(await audits(session, "system_alert_sent")) == 1
        assert len(alert_emails(mail)) == 1

        # A new UTC hour is a new counter, so the held reminder goes out: the cap delayed
        # it, it did not lose it.
        frozen_clock.set(T48 + timedelta(hours=1))
        assert await service.tick() == 1
        assert len(reminder_emails(mail)) == 2
        refreshed = [
            row
            for item in items
            for row in await load_reminders(session, item.id)
            if row.status == "sent"
        ]
        assert len(refreshed) == 2


# ── AlertService's own contract (FR-13, spec §2.3.4) ────────────────────────


class TestAlertService:
    async def test_an_unknown_alert_type_is_rejected_before_anything_happens(
        self,
        session: AsyncSession,
        mail: FakeMailClient,
        app_container: AppContainer,
    ) -> None:
        """An alert type is not an audit event type: a typo must not invent one."""
        alerts = AlertService(app_container)

        with pytest.raises(ValueError, match="unknown alert type"):
            await alerts.alert("gmail_broke", "subject", "body")

        assert mail.sent == []
        assert await audits(session, "system_alert_sent") == []

    async def test_the_cooldown_is_per_type_and_uses_the_clock(
        self,
        session: AsyncSession,
        settings: Settings,
        frozen_clock: Any,
        mail: FakeMailClient,
        app_container: AppContainer,
    ) -> None:
        """FR-13: one alert per failure type per cooldown — and a different type still gets
        through."""
        alerts = AlertService(app_container)

        assert await alerts.alert(ALERT_TYPES[0], "first", "body") is True
        # Same type, still inside the cooldown: suppressed, and not a failure.
        assert await alerts.alert(ALERT_TYPES[0], "first again", "body") is False
        # A different failure type is a different conversation and its own cooldown.
        assert await alerts.alert(ALERT_TYPES[1], "second", "body") is True

        assert len(alert_emails(mail)) == 2
        sent = await audits(session, "system_alert_sent")
        assert [row.payload["alert_type"] for row in sent] == [ALERT_TYPES[0], ALERT_TYPES[1]]

        # Past the cooldown the same type may send again.
        frozen_clock.advance(timedelta(hours=settings.alert_cooldown_hours + 1))
        assert await alerts.alert(ALERT_TYPES[0], "later", "body") is True
        assert len(alert_emails(mail)) == 3
        assert (
            await counter(session, last_alert_key(ALERT_TYPES[0])) == frozen_clock.now().isoformat()
        )
