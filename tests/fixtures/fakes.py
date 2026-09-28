"""Fake ports: `NotionClient`, `MailClient`, `IntentInterpreter`, wired into `AppContainer`.

CLAUDE.md constraint 5: no test, fixture, or CI job makes a live Notion, Gmail, or
DeepSeek call. These fakes are how that is achieved — `src/app/main.py` constructs the real
adapters, tests construct these, and the V1-only methods (`update_page`, `poll_new`,
`interpret`) raise rather than silently no-op, so an MVP code path that reaches an LLM or a
Notion write fails loudly instead of quietly appearing to work.

Every call is recorded (`calls`, `sent`, `send_attempts`, ...) so a test can assert what
the service actually did, not merely what it returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.config import Settings
from app.container import AppContainer
from app.db.session import create_engine, create_session_factory
from app.domain.types import Intent, InterpretationContext, NotionPage, PollResult, SentMessage
from app.integrations.gmail.client import MailClient
from app.integrations.llm.interpreter import IntentInterpreter
from app.integrations.notion.client import NotionClient

# Default `database_id -> data_source_id` map, matching `tests/fixtures/pages.py`'s default
# `DEFAULT_DATA_SOURCE_ID` ("ds-1"). Real resolution is a `GET /v1/databases/{id}` call; the
# fake returns this so tests never need a network round trip (§2.3.2.A).
DEFAULT_DATA_SOURCE_IDS: Mapping[str, str] = {
    "db-assignments": "ds-1",
    "db-exams": "ds-2",
}


class FakeNotionUnreachable(RuntimeError):
    """Notion could not be reached. Raised by `FakeNotionClient(raise_on_get_page=True)`.

    Models the pre-send check when Notion is down: the scheduler must proceed on local
    data and audit `presend_check_skipped`, not skip the reminder (§2.3.2.C).
    """


class FakeMailError(RuntimeError):
    """A simulated Gmail send failure, for the backoff / 5-strike path (§2.3.2.C)."""


@dataclass(frozen=True, slots=True)
class ChangedPagesCall:
    """One `list_changed_pages` call, recorded verbatim.

    `since` is the *unmodified* argument the caller passed, so a test can assert the
    5-minute overlap: it must be `last_success_cursor - 5min`, not the cursor itself.
    """

    data_source_id: str
    since: datetime | None


@dataclass(frozen=True, slots=True)
class GetPageCall:
    page_id: str


@dataclass(frozen=True, slots=True)
class UpdatePageCall:
    """An attempted write. The MVP must never produce one — `update_page` always raises."""

    page_id: str
    status_property: str
    status_name: str | None
    done_property: str | None
    done_value: bool | None
    due_property: str
    due_date: date | None


class FakeNotionClient:
    """In-memory `NotionClient`. No network, ever."""

    def __init__(
        self,
        pages: Iterable[NotionPage] = (),
        *,
        data_source_ids: Mapping[str, str] | None = None,
        raise_on_get_page: bool = False,
    ) -> None:
        self.pages: list[NotionPage] = list(pages)
        self.data_source_ids: dict[str, str] = dict(data_source_ids or DEFAULT_DATA_SOURCE_IDS)
        # Mutable on purpose: a test flips this mid-test to simulate Notion going down
        # between the claim and the pre-send check.
        self.raise_on_get_page = raise_on_get_page
        self.resolve_calls: list[str] = []
        self.list_changed_calls: list[ChangedPagesCall] = []
        self.list_all_calls: list[str] = []
        self.get_page_calls: list[GetPageCall] = []
        self.update_page_calls: list[UpdatePageCall] = []

    # ── Helpers for arranging a test ────────────────────────────────────────

    def add(self, page: NotionPage) -> None:
        """Add or replace a page (a page re-queried after an edit keeps its id)."""
        self.pages = [existing for existing in self.pages if existing.page_id != page.page_id]
        self.pages.append(page)

    def changed_since_args(self) -> list[datetime | None]:
        """Just the `since` arguments, in call order — the 5-minute-overlap assertion."""
        return [call.since for call in self.list_changed_calls]

    # ── NotionClient ────────────────────────────────────────────────────────

    async def resolve_data_source_id(self, database_id: str) -> str:
        self.resolve_calls.append(database_id)
        try:
            return self.data_source_ids[database_id]
        except KeyError:
            known = ", ".join(sorted(self.data_source_ids)) or "(none)"
            raise KeyError(
                f"FakeNotionClient has no data_source_id for {database_id!r}; known: {known}"
            ) from None

    async def list_changed_pages(
        self, data_source_id: str, since: datetime | None
    ) -> list[NotionPage]:
        """Pages edited after `since`, newest Notion semantics included.

        Trashed pages are **excluded**, exactly as the real data-source query excludes them.
        That asymmetry with `list_all_pages` is the whole reason the daily full reconcile
        can detect archiving (FR-9) — a fake that returned trashed pages here would make
        FR-9 untestable.
        """
        self.list_changed_calls.append(ChangedPagesCall(data_source_id=data_source_id, since=since))
        return [
            page
            for page in self.pages
            if page.data_source_id == data_source_id
            and not page.in_trash
            and (since is None or page.last_edited_time > since)
        ]

    async def list_all_pages(self, data_source_id: str) -> list[NotionPage]:
        """Everything in the data source, trashed pages included (FR-9 detection)."""
        self.list_all_calls.append(data_source_id)
        return [page for page in self.pages if page.data_source_id == data_source_id]

    async def get_page(self, page_id: str) -> NotionPage | None:
        self.get_page_calls.append(GetPageCall(page_id=page_id))
        if self.raise_on_get_page:
            raise FakeNotionUnreachable(f"simulated: GET /v1/pages/{page_id} failed")
        for page in self.pages:
            if page.page_id == page_id:
                return page
        return None

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
        """V1 (build phase 6). The MVP must never write to Notion, so this always raises."""
        self.update_page_calls.append(
            UpdatePageCall(
                page_id=page_id,
                status_property=status_property,
                status_name=status_name,
                done_property=done_property,
                done_value=done_value,
                due_property=due_property,
                due_date=due_date,
            )
        )
        raise NotImplementedError(
            "FakeNotionClient.update_page is V1 (build phase 6); the MVP never writes to Notion"
        )


@dataclass(frozen=True, slots=True)
class SendCall:
    """One accepted `send`, recorded exactly as the service called it."""

    to: str
    subject: str
    body: str
    thread_id: str | None
    in_reply_to: str | None


class FakeMailClient:
    """In-memory `MailClient`. Nothing is ever sent over a network."""

    def __init__(self, *, fail_times: int = 0, fail_always: bool = False) -> None:
        # `fail_times=2` fails the first two attempts then succeeds: the retry/backoff path.
        # `fail_always=True` drives the 5-strike -> failed + alert path (§2.3.2.C).
        self.fail_times = fail_times
        self.fail_always = fail_always
        self.send_attempts = 0
        self.sent: list[SendCall] = []
        self.messages: list[SentMessage] = []

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None,
        in_reply_to: str | None,
    ) -> SentMessage:
        self.send_attempts += 1
        if self.fail_always or self.send_attempts <= self.fail_times:
            raise FakeMailError(f"simulated Gmail send failure on attempt {self.send_attempts}")

        number = len(self.sent) + 1
        message = SentMessage(
            provider_message_id=f"fake-message-{number}",
            # `thread_id=None` starts a new thread — one item per email (FR-6).
            provider_thread_id=thread_id or f"fake-thread-{number}",
            rfc_message_id=f"<fake-message-{number}@fake.invalid>",
        )
        self.sent.append(
            SendCall(
                to=to, subject=subject, body=body, thread_id=thread_id, in_reply_to=in_reply_to
            )
        )
        self.messages.append(message)
        return message

    async def poll_new(self, history_id: str | None) -> PollResult:
        """V1 (build phase 4). The MVP has no inbound poll."""
        raise NotImplementedError(
            "FakeMailClient.poll_new is V1 (build phase 4); the MVP never polls Gmail"
        )

    async def find_sent_by_token(self, token: str) -> SentMessage | None:
        """The stale-claim recovery lookup (§2.3.2.C): "did the email actually go out?".

        Matches the `ref: <token>` footer in a previously sent body, on a token boundary so
        `abc` does not match `abcd`.
        """
        pattern = re.compile(rf"ref:\s*{re.escape(token)}\b")
        for call, message in zip(self.sent, self.messages, strict=True):
            if pattern.search(call.body):
                return message
        return None


class FakeIntentInterpreter:
    """The MVP has no LLM anywhere in the flow. Reaching this is a bug, so it raises.

    A silent no-op here would let an LLM call slip into the reminder path unnoticed; the
    `raise` is the guard (§1.2 non-goals, CLAUDE.md "no LLM call anywhere in the MVP").
    """

    def __init__(self) -> None:
        self.calls: list[InterpretationContext] = []

    async def interpret(self, ctx: InterpretationContext) -> Intent:
        self.calls.append(ctx)
        raise NotImplementedError(
            "FakeIntentInterpreter.interpret: the MVP makes no LLM call (V1, build phase 5)"
        )


def make_container(
    settings: Settings,
    *,
    clock: Clock,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    notion: NotionClient | None = None,
    mail: MailClient | None = None,
    interpreter: IntentInterpreter | None = None,
) -> AppContainer:
    """A fully wired `AppContainer` of fakes.

    `session_factory` may be omitted for a test that never touches the database: the
    default builds one from `settings.database_url` (the test URL), and SQLAlchemy engines
    connect lazily, so constructing it opens no connection.
    """
    return AppContainer(
        settings=settings,
        clock=clock,
        session_factory=(
            session_factory
            if session_factory is not None
            else create_session_factory(create_engine(settings))
        ),
        notion=notion if notion is not None else FakeNotionClient(),
        mail=mail if mail is not None else FakeMailClient(),
        interpreter=interpreter if interpreter is not None else FakeIntentInterpreter(),
    )
