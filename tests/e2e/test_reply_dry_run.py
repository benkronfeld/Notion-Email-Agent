"""The frozen-clock reply dry run — V1's acceptance test (§1.2 criteria 6-10).

This is the mirror of `test_frozen_clock_dry_run.py`. Where that one proves the reminder
flow end to end, this one proves the *reply* flow: the real application over a real Postgres,
the real reminder send, and then a real reply travelling through the inbound pipeline and
back out as a confirmation.

Nothing is stubbed except the three external systems. A reminder is genuinely sent (to a
fake inbox), which genuinely creates the `email_threads` and `outbound_messages` rows the
reply mapper reads; the reply is genuinely polled through the real admin route; the Notion
write goes through the real `NotionWriter` live-fetch/read-back path. The only fake in the
write is Notion itself.

What it demonstrates, against V1's success criteria:

6. the reply maps to exactly one item via the stored Gmail thread id — never by name;
7. a clear status change is applied and read-back verified;
8. an ambiguous reply produces a question and **zero** writes;
9. a duplicate inbound message is processed once;
10. the owner receives an accurate confirmation.

No test makes a live Notion, Gmail, or DeepSeek call (CLAUDE.md constraint 5), and the
interpreter is scripted rather than contacted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import FrozenClock
from app.container import AppContainer
from app.db.models import AuditLog, EmailThread, Item, ProcessedInboundMessage, Reminder
from app.domain.intents import Intent, IntentAction, StatusValue
from app.domain.types import InboundMessage
from app.jobs import JOB_NOTION_SYNC, JOB_REMINDER_SCHEDULER, run_job_once
from fixtures import pages
from fixtures.fakes import FakeIntentInterpreter, FakeMailClient, FakeNotionClient

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

PAGE_ID = "page-assignment-1"
OWNER = "owner@example.test"

# The 48h target for a 2026-10-03 due date is 2026-10-02T03:59Z (EDT); a minute later the
# reminder is due and a tick sends it.
T_AFTER_48H = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)


def scripted(container: AppContainer) -> FakeIntentInterpreter:
    """The container's interpreter, asserted to be the scriptable fake.

    `AppContainer` is frozen, so the fake is scripted in place — and the assertion is what
    keeps this honest: an unscripted fake raises, so a test that forgot to script one would
    fail loudly rather than silently exercise the no-LLM guard.
    """
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    return interpreter


def fake_notion(container: AppContainer) -> FakeNotionClient:
    notion = container.notion
    assert isinstance(notion, FakeNotionClient)
    return notion


def fake_mail(container: AppContainer) -> FakeMailClient:
    mail = container.mail
    assert isinstance(mail, FakeMailClient)
    return mail


async def send_the_reminder(container: AppContainer) -> str:
    """Run the real reminder flow and return the Gmail thread id it created.

    This is the setup every test needs, and it deliberately goes through the real jobs
    rather than inserting rows: the mapping a reply depends on must be created by the same
    code that creates it in production, or the test proves nothing about mapping.
    """
    clock = container.clock
    assert isinstance(clock, FrozenClock)
    notion, mail = fake_notion(container), fake_mail(container)

    notion.add(pages.make_assignment(page_id=PAGE_ID, due_start="2026-10-03"))
    await run_job_once(container, JOB_NOTION_SYNC)

    clock.advance(T_AFTER_48H - clock.now())
    await run_job_once(container, JOB_REMINDER_SCHEDULER)

    reminders = [call for call in mail.sent if call.subject.startswith("[Reminder]")]
    assert len(reminders) == 1, "the 48h reminder should have gone out exactly once"

    async with container.session_factory() as session:
        thread = (await session.execute(select(EmailThread))).scalar_one()
        return thread.provider_thread_id


def inbound(thread_id: str, body: str, message_id: str) -> InboundMessage:
    return InboundMessage(
        provider_message_id=message_id,
        provider_thread_id=thread_id,
        from_address=OWNER,
        subject="Re: [Reminder] CS 101: Problem Set 4",
        in_reply_to=None,
        references=(),
        body_text=body,
        received_at=T_AFTER_48H,
    )


@pytest.fixture(autouse=True)
def _empty_database(session: AsyncSession) -> None:
    """Truncate before each test, via the harness fixture (see the integration module)."""


@pytest.fixture
def sessions(app_container: AppContainer) -> async_sessionmaker[AsyncSession]:
    return app_container.session_factory


# ── Criterion 7 and 10: a clear change is applied, verified, and confirmed ──


async def test_a_reply_marks_the_item_complete_end_to_end(
    app_container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    thread_id = await send_the_reminder(app_container)
    notion, mail = fake_notion(app_container), fake_mail(app_container)
    scripted(app_container).intents = [
        Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)
    ]

    # The reply quotes the reminder, as a real one would; only the new text may be read.
    mail.receive(
        inbound(
            thread_id,
            "done with this one\n\n> On Fri, Oct 2, 2026 at 12:00 AM wrote:\n> Problem Set 4\n",
            "reply-1",
        )
    )

    # Through the real admin route, not by calling the service directly.
    response = await client.post("/admin/gmail/poll", headers=admin_headers)
    assert response.status_code == 200

    # Criterion 7: one PATCH, Status and Done together, verified by read-back.
    assert len(notion.update_page_calls) == 1
    assert notion.update_page_calls[0].status_name == "Completed"
    assert notion.update_page_calls[0].done_value is True

    async with sessions() as session:
        item = (
            await session.execute(select(Item).where(Item.notion_page_id == PAGE_ID))
        ).scalar_one()
    assert item.status == "Completed"
    assert item.done is True

    # Criterion 10: an accurate confirmation, quoting the read-back.
    assert mail.sent[-1].subject == "[Confirmation] Problem Set 4"
    assert "Verified in Notion" in mail.sent[-1].body
    assert "Not started" in mail.sent[-1].body and "Completed" in mail.sent[-1].body

    async with sessions() as session:
        events = [
            row.event_type
            for row in (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars()
        ]
    assert "reply_interpreted" in events
    assert "notion_update_succeeded" in events


async def test_a_completed_item_sends_no_further_reminders(
    app_container: AppContainer,
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """The consequence the owner actually cares about: replying "done" stops the mail."""
    thread_id = await send_the_reminder(app_container)
    mail = fake_mail(app_container)
    scripted(app_container).intents = [
        Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)
    ]
    mail.receive(inbound(thread_id, "done", "reply-1"))
    await client.post("/admin/gmail/poll", headers=admin_headers)

    clock = app_container.clock
    assert isinstance(clock, FrozenClock)
    # Move past the 24h target and the due instant itself.
    clock.advance(timedelta(hours=30))
    await run_job_once(app_container, JOB_REMINDER_SCHEDULER)

    reminders_sent = [call for call in mail.sent if call.subject.startswith("[Reminder]")]
    assert len(reminders_sent) == 1, "no second reminder for a completed item (FR-3)"


# ── Criterion 8: ambiguity means a question and zero writes ─────────────────


async def test_an_ambiguous_reply_asks_and_writes_nothing(
    app_container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    thread_id = await send_the_reminder(app_container)
    notion, mail = fake_notion(app_container), fake_mail(app_container)
    scripted(app_container).intents = [
        Intent(action=IntentAction.CHANGE_DUE_DATE, due_date_text="push it back a bit")
    ]
    # Counted, not asserted empty: the *sync* legitimately reads the page and its course
    # relation, so what matters is that the ambiguous reply adds no read of its own.
    reads_before = len(notion.get_page_calls)

    mail.receive(inbound(thread_id, "can you push it back a bit?", "reply-ambiguous"))
    await client.post("/admin/gmail/poll", headers=admin_headers)

    # Not one write, and not one further read of Notion.
    assert notion.update_page_calls == []
    assert len(notion.get_page_calls) == reads_before

    assert mail.sent[-1].subject == "[Clarification] Problem Set 4"
    assert "didn't change anything" in mail.sent[-1].body.lower()

    async with sessions() as session:
        item = (
            await session.execute(select(Item).where(Item.notion_page_id == PAGE_ID))
        ).scalar_one()
        assert item.due_date == date(2026, 10, 3)  # untouched
        pending = [
            row
            for row in (await session.execute(select(Reminder))).scalars()
            if row.status == "pending"
        ]
    assert pending, "the still-future reminder must not be disturbed by a failed request"


# ── Criterion 9: a duplicate is one unit of work ────────────────────────────


async def test_a_duplicate_reply_is_processed_once(
    app_container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    thread_id = await send_the_reminder(app_container)
    notion, mail = fake_notion(app_container), fake_mail(app_container)
    scripted(app_container).intents = [
        Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)
    ]

    message = inbound(thread_id, "done", "reply-dup")
    mail.receive(message)
    await client.post("/admin/gmail/poll", headers=admin_headers)

    # The very same message arrives again — a re-delivered history record.
    mail.receive(message)
    await client.post("/admin/gmail/poll", headers=admin_headers)

    assert len(notion.update_page_calls) == 1, "the write must happen exactly once"
    confirmations = [call for call in mail.sent if call.subject.startswith("[Confirmation]")]
    assert len(confirmations) == 1

    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    select(ProcessedInboundMessage).where(
                        ProcessedInboundMessage.provider_message_id == "reply-dup"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "exactly one dedupe row"


# ── Criterion 6: mapping is by stored id, never by name ─────────────────────


async def test_a_reply_on_an_unknown_thread_maps_to_nothing(
    app_container: AppContainer,
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    await send_the_reminder(app_container)
    notion, mail = fake_notion(app_container), fake_mail(app_container)

    mail.receive(inbound("some-other-thread", "done", "reply-unknown"))
    await client.post("/admin/gmail/poll", headers=admin_headers)

    assert notion.update_page_calls == []
    assert mail.sent[-1].subject.startswith("[Notice]")
    assert "couldn't tell which item" in mail.sent[-1].subject.lower()

    async with app_container.session_factory() as session:
        events = [row.event_type for row in (await session.execute(select(AuditLog))).scalars()]
    assert "inbound_unmapped" in events
