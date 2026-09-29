"""`InboundService` against a real Postgres — the reply pipeline end to end (§2.3.2.E).

`tests/unit/` proves the pure decisions (date resolution, validation, parsing). This module
proves the part that only a database and a fake inbox can: that a reply is **deduplicated
before anything else**, that it maps to exactly one item by stored Gmail ids and never by
name, that an ambiguous reply produces a question and **zero writes**, that a verified write
is read back and confirmed with the values Notion actually holds, and that a write which
does not land is reported honestly rather than as a success.

Every test drives the real service over the real repositories with fake ports and a
`FrozenClock` (CLAUDE.md constraints 4 and 5). No test calls a live Notion, Gmail, or
DeepSeek endpoint.

Two arrangements are deliberate and worth knowing before editing:

* **The thread is seeded directly, not by running a reminder tick.** The frozen clock sits
  before every reminder target (the 48h target is 2026-10-02T03:59Z), so a tick would send
  nothing. Seeding through `threads_repo` produces exactly the rows a real send produces,
  which is what step 3 reads.
* **The interpreter is scripted.** `FakeIntentInterpreter` raises when unscripted, so a test
  that forgets to script one fails loudly instead of silently exercising the no-LLM guard.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.clock import FrozenClock
from app.config import Settings
from app.container import AppContainer
from app.db.models import AuditLog, EmailThread, Item, ProcessedInboundMessage
from app.db.repositories import items as items_repo
from app.db.repositories import threads as threads_repo
from app.db.session import create_session_factory
from app.domain.intents import Intent, IntentAction, StatusValue
from app.domain.types import InboundMessage, NormalizedItem
from app.services.inbound_service import InboundService
from fixtures import pages
from fixtures.fakes import (
    FakeIntentInterpreter,
    FakeMailClient,
    FakeNotionClient,
    make_container,
)

pytestmark = pytest.mark.integration

OWNER = "owner@example.test"
PAGE_ID = "page-1"
GMAIL_THREAD = "gmail-thread-1"
OUT_RFC_ID = "<out-1@example.test>"
DUE_DATE = date(2026, 10, 3)
# 2026-10-03 23:59 Eastern (EDT) = 2026-10-04T03:59Z (CLAUDE.md constraint 3).
DUE_AT = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


# ── Wiring ──────────────────────────────────────────────────────────────────


@pytest.fixture
def interpreter() -> FakeIntentInterpreter:
    """Scripted per test; unscripted it raises, which keeps the no-LLM guard honest."""
    return FakeIntentInterpreter(default=Intent(action=IntentAction.NO_ACTION))


@pytest.fixture
def container(
    settings: Settings,
    frozen_clock: FrozenClock,
    engine: AsyncEngine,
    notion: FakeNotionClient,
    mail: FakeMailClient,
    interpreter: FakeIntentInterpreter,
) -> AppContainer:
    return make_container(
        settings=settings,
        clock=frozen_clock,
        session_factory=create_session_factory(engine),
        notion=notion,
        mail=mail,
        interpreter=interpreter,
    )


@pytest.fixture
def service(container: AppContainer) -> InboundService:
    return InboundService(container)


@pytest.fixture(autouse=True)
def _empty_database(session: AsyncSession) -> None:
    """Start every test from an empty schema.

    The harness truncates inside its `session` fixture, and its docstring is explicit that a
    test which never requests it inherits the previous test's rows. That bit this module
    immediately: without it the second test hits `uq_email_threads_provider_thread_id`,
    because every test here seeds the same Gmail thread id. Requesting `session` here makes
    the truncation unconditional rather than a thing each test must remember.
    """


@pytest.fixture
def sessions(container: AppContainer) -> async_sessionmaker[AsyncSession]:
    return container.session_factory


# ── Arranging an item, a thread, and a reply ────────────────────────────────


def _normalized(
    *,
    status: str = "Not started",
    done: bool = False,
    due_date: date | None = DUE_DATE,
    due_at: datetime | None = DUE_AT,
) -> NormalizedItem:
    return NormalizedItem(
        notion_page_id=PAGE_ID,
        notion_data_source_id="ds-1",
        source_db="assignments_readings",
        item_kind="assignment_reading",
        notion_type="Assignment",
        name="Problem Set 4",
        course_page_id="course-1",
        course="CS 101",
        status=status,
        done=done,
        due_date=due_date,
        due_at=due_at,
        due_has_time=False,
        timezone="America/New_York",
        notion_url="https://notion.so/page-1",
        notion_last_edited_time=None,
        in_trash=False,
    )


async def seed(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    *,
    status: str = "Not started",
    done: bool = False,
) -> tuple[Item, UUID]:
    """One live Notion page, one local item, and the reminder thread that points at it.

    Registers the page with the fake Notion too, because `NotionWriter` live-fetches before
    writing and `_apply_locally` re-fetches afterwards — an item whose page is absent would
    fail for a reason the test did not intend.
    """
    notion = container.notion
    assert isinstance(notion, FakeNotionClient)
    notion.add(
        pages.make_assignment(
            page_id=PAGE_ID,
            due_start="2026-10-03",
            status=status,
            done=done,
        )
    )

    async with sessions() as session:
        item = await items_repo.upsert(session, _normalized(status=status, done=done))
        await session.refresh(item)
        thread = await threads_repo.create_thread(
            session,
            item_id=item.id,
            reminder_id=None,
            provider_thread_id=GMAIL_THREAD,
            subject="[Reminder] CS 101: Problem Set 4, due Sat Oct 3 (in 48 hours)",
            root_rfc_message_id=OUT_RFC_ID,
        )
        await threads_repo.add_outbound(
            session,
            thread_id=thread.id,
            kind="reminder",
            provider_message_id="out-1",
            rfc_message_id=OUT_RFC_ID,
        )
        await session.commit()
        return item, thread.id


def reply(
    body: str = "done",
    *,
    message_id: str = "in-1",
    sender: str = OWNER,
    thread_id: str | None = GMAIL_THREAD,
    in_reply_to: str | None = OUT_RFC_ID,
    references: tuple[str, ...] = (OUT_RFC_ID,),
    **extra: Any,
) -> InboundMessage:
    return InboundMessage(
        provider_message_id=message_id,
        provider_thread_id=thread_id,
        from_address=sender,
        subject="Re: [Reminder] CS 101: Problem Set 4",
        in_reply_to=in_reply_to,
        references=references,
        body_text=body,
        received_at=RECEIVED_AT,
        **extra,
    )


# ── Reading back what happened ──────────────────────────────────────────────


async def audit_types(sessions: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessions() as session:
        rows = (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        return [row.event_type for row in rows]


async def item_now(sessions: async_sessionmaker[AsyncSession]) -> Item:
    async with sessions() as session:
        row = (
            await session.execute(select(Item).where(Item.notion_page_id == PAGE_ID))
        ).scalar_one()
        return row


async def thread_now(sessions: async_sessionmaker[AsyncSession], thread_id: UUID) -> EmailThread:
    async with sessions() as session:
        return (
            await session.execute(select(EmailThread).where(EmailThread.id == thread_id))
        ).scalar_one()


async def inbound_row(
    sessions: async_sessionmaker[AsyncSession], message_id: str
) -> ProcessedInboundMessage | None:
    async with sessions() as session:
        return (
            await session.execute(
                select(ProcessedInboundMessage).where(
                    ProcessedInboundMessage.provider_message_id == message_id
                )
            )
        ).scalar_one_or_none()


def kinds_sent(mail: FakeMailClient) -> list[str]:
    """The `kind` of each outbound message, via the subject the composer produced.

    Read from what was actually sent rather than from the database, so a reply that was
    emailed but never recorded (or the reverse) is visible.
    """
    return [call.subject for call in mail.sent]


# ── The happy paths (FR-10, criteria 7 and 10) ──────────────────────────────


async def test_status_change_is_written_verified_and_confirmed(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    _, _thread_id = await seed(container, sessions)
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    interpreter.intents = [Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)]

    service = InboundService(container)
    mail.receive(reply("done with this"))
    assert await service.poll_once() == 1

    # One PATCH, carrying Status AND Done together (§1.2, FR-10).
    assert len(notion.update_page_calls) == 1
    call = notion.update_page_calls[0]
    assert call.status_name == "Completed"
    assert call.done_value is True
    assert call.due_date is None  # the due date was not part of this change

    item = await item_now(sessions)
    assert item.status == "Completed"
    assert item.done is True

    assert [c.subject for c in mail.sent] == ["[Confirmation] Problem Set 4"]
    assert "Verified in Notion" in mail.sent[0].body

    events = await audit_types(sessions)
    assert "reply_interpreted" in events
    assert "notion_update_attempted" in events
    assert "notion_update_succeeded" in events


async def test_due_date_change_updates_the_item_and_regenerates_reminders(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    """The step that makes a date change real: local row updated, new schedule planned."""
    from app.db.models import Reminder

    await seed(container, sessions)
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    interpreter.intents = [
        Intent(action=IntentAction.CHANGE_DUE_DATE, due_date_text="October 10th")
    ]

    service = InboundService(container)
    mail.receive(reply("push the due date to October 10th"))
    assert await service.poll_once() == 1

    item = await item_now(sessions)
    assert item.due_date == date(2026, 10, 10)
    # 2026-10-10 23:59 Eastern (EDT) = 2026-10-11T03:59Z.
    assert item.due_at == datetime(2026, 10, 11, 3, 59, tzinfo=UTC)

    async with sessions() as session:
        snapshots = {
            row.due_at_snapshot for row in (await session.execute(select(Reminder))).scalars().all()
        }
    # Option A: a brand-new schedule keyed on the new date (FR-8).
    assert datetime(2026, 10, 11, 3, 59, tzinfo=UTC) in snapshots
    assert "[Confirmation] Problem Set 4" in kinds_sent(mail)


async def test_combined_change_is_a_single_patch(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    notion: FakeNotionClient,
) -> None:
    await seed(container, sessions)
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    interpreter.intents = [
        Intent(
            action=IntentAction.CHANGE_STATUS_AND_DUE_DATE,
            status=StatusValue.IN_PROGRESS,
            due_date_text="October 10th",
        )
    ]

    mail = container.mail
    assert isinstance(mail, FakeMailClient)
    mail.receive(reply("in progress and move it to October 10th"))
    assert await InboundService(container).poll_once() == 1

    assert len(notion.update_page_calls) == 1
    call = notion.update_page_calls[0]
    assert call.status_name == "In progress"
    assert call.done_value is False  # Not started / In progress always write Done = false
    assert call.due_date == date(2026, 10, 10)


# ── Failure to understand: a question and ZERO writes (criterion 8) ─────────


async def test_ambiguous_reply_asks_and_writes_nothing(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    thread_id = (await seed(container, sessions))[1]
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    interpreter.intents = [Intent(action=IntentAction.CHANGE_DUE_DATE, due_date_text="next Friday")]

    service = InboundService(container)
    mail.receive(reply("can you push it to next Friday"))
    assert await service.poll_once() == 1

    # The whole point: a question, and not one write.
    assert notion.update_page_calls == []
    assert notion.get_page_calls == []
    assert kinds_sent(mail) == ["[Clarification] Problem Set 4"]
    assert "didn't change anything" in mail.sent[0].body.lower()

    thread = await thread_now(sessions, thread_id)
    assert thread.state == "awaiting_clarification"
    assert thread.clarification_rounds == 1
    assert thread.pending_clarification is not None

    item = await item_now(sessions)
    assert item.due_date == DUE_DATE  # untouched

    assert "clarification_requested" in await audit_types(sessions)


async def test_clarification_rounds_close_the_thread_and_only_notice_after(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    """MAX_CLARIFICATION_ROUNDS (3) is a budget, and a closed thread is a dead end."""
    thread_id = (await seed(container, sessions))[1]
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    service = InboundService(container)

    for round_number in range(3):
        interpreter.intents = [
            Intent(action=IntentAction.CHANGE_DUE_DATE, due_date_text="sometime soon")
        ]
        mail.receive(reply("sometime soon", message_id=f"in-{round_number}"))
        await service.poll_once()

    thread = await thread_now(sessions, thread_id)
    assert thread.clarification_rounds == 3
    assert thread.state == "closed"

    # A reply to a closed thread gets the notice, never another question.
    replies_before = len(mail.sent)
    interpreter.intents = [
        Intent(action=IntentAction.CHANGE_DUE_DATE, due_date_text="sometime soon")
    ]
    mail.receive(reply("still soon", message_id="in-closed"))
    await service.poll_once()

    assert len(mail.sent) == replies_before + 1
    assert mail.sent[-1].subject == "[Notice] [Closed] Problem Set 4"
    assert "did not change anything" in mail.sent[-1].body
    assert notion.update_page_calls == []


# ── Steps 1-3: dedupe, sender, mapping (criterion 6 and 9) ──────────────────


async def test_duplicate_message_is_processed_exactly_once(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    await seed(container, sessions)
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    interpreter.intents = [Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)]

    service = InboundService(container)
    message = reply("done")
    mail.receive(message)
    assert await service.poll_once() == 1
    assert len(notion.update_page_calls) == 1

    # The same provider_message_id arrives again — a re-delivered history record.
    mail.receive(message)
    assert await service.poll_once() == 0

    assert len(notion.update_page_calls) == 1  # still one write
    assert len(mail.sent) == 1  # and still one confirmation

    row = await inbound_row(sessions, "in-1")
    assert row is not None
    assert row.status == "done"


async def test_non_allowlisted_sender_is_ignored(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    await seed(container, sessions)
    service = InboundService(container)

    mail.receive(reply("mark it done", sender="stranger@example.test"))
    # 1, not 0: the message *was* handled — to the terminal `ignored` state. The count
    # excludes only duplicates, which stop before anything happens.
    assert await service.poll_once() == 1

    assert notion.update_page_calls == []
    assert mail.sent == []
    assert "inbound_ignored" in await audit_types(sessions)


async def test_auto_reply_is_ignored(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    await seed(container, sessions)
    service = InboundService(container)

    mail.receive(reply("out of office", auto_submitted="auto-replied"))
    assert await service.poll_once() == 1
    assert notion.update_page_calls == []
    assert "inbound_ignored" in await audit_types(sessions)


async def test_unmappable_reply_gets_a_notice_and_no_guess(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    """No thread match -> fail safe. Never resolved by name, never guessed."""
    await seed(container, sessions)
    service = InboundService(container)

    # Both resolution paths must miss: an unknown Gmail thread AND no reply header that
    # points at a message we sent. (`in_reply_to` defaults to the reminder's own Message-ID,
    # so it has to be cleared too — leaving it set is how the `References` fallback
    # legitimately maps a reply whose thread id was lost.)
    mail.receive(
        reply(
            "done",
            thread_id="gmail-thread-unknown",
            in_reply_to=None,
            references=("<nope@example.test>",),
        )
    )
    assert await service.poll_once() == 1

    assert notion.update_page_calls == []
    assert len(mail.sent) == 1
    assert "couldn't tell which item" in mail.sent[0].subject.lower()

    events = await audit_types(sessions)
    assert "inbound_unmapped" in events
    assert "inbound_ignored" not in events  # the two must never be conflated


# ── Steps 8-9: honest reporting (criterion 10, Appendix B case 13) ──────────


async def test_a_write_that_does_not_land_is_reported_as_failure(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    notion: FakeNotionClient,
) -> None:
    """`drop_update` accepts the PATCH and does nothing — read-back must catch it."""
    await seed(container, sessions)
    notion.drop_update = True
    interpreter = container.interpreter
    assert isinstance(interpreter, FakeIntentInterpreter)
    interpreter.intents = [Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)]

    service = InboundService(container)
    mail.receive(reply("done"))
    assert await service.poll_once() == 1

    assert mail.sent[0].subject == "[Failure] Problem Set 4"
    assert "NOT" in mail.sent[0].body
    assert "Verified" not in mail.sent[0].body

    item = await item_now(sessions)
    assert item.status == "Not started"  # the local row did NOT follow a failed write

    assert "notion_update_failed" in await audit_types(sessions)


async def test_interpreter_outage_alerts_and_does_not_send_a_question(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
    mail: FakeMailClient,
    interpreter: FakeIntentInterpreter,
    notion: FakeNotionClient,
) -> None:
    """A DeepSeek outage is not a clarification — the owner must learn the system is down."""
    await seed(container, sessions)
    interpreter.error = RuntimeError("deepseek is unreachable")
    sent_before = len(mail.sent)

    service = InboundService(container)
    mail.receive(reply("done"))
    await service.poll_once()

    # Exactly one email, and it is the alert. The alert is *supposed* to be mail — what must
    # not happen is a clarification, which would answer an outage with a question the owner
    # cannot resolve, or a confirmation for a change that was never made.
    assert len(mail.sent) == sent_before + 1
    assert mail.sent[-1].subject.startswith("[Alert]")
    assert "didn't change" not in mail.sent[-1].body.lower()

    assert notion.update_page_calls == []
    row = await inbound_row(sessions, "in-1")
    assert row is not None
    assert row.status == "failed"


async def test_stuck_rows_are_flagged_and_never_retried(
    container: AppContainer,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(container, sessions)
    # Simulate a crash between claim and finish: a `processing` row left behind.
    from app.db.models import ProcessedInboundMessage as Row

    async with sessions() as session:
        session.add(
            Row(
                provider_message_id="in-stuck",
                provider_thread_id=GMAIL_THREAD,
                status="processing",
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
        )
        await session.commit()

    flagged = await InboundService(container).flag_stuck()
    assert flagged == 1
    assert "inbound_stuck" in await audit_types(sessions)

    # Never retried: the row is still exactly as it was left.
    row = await inbound_row(sessions, "in-stuck")
    assert row is not None
    assert row.status == "processing"
    assert row.completed_at is None
