"""The fixtures themselves, proven to work — no database, no network.

If this file fails, every other test's failures are suspect, so it stays narrow: page
builders produce the shape the normalizer reads, the fakes record what they were asked to
do, and the MVP's no-LLM / no-write guards actually raise.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.container import AppContainer
from app.domain.due_time import compute_due_at
from app.domain.intents import ALLOWED_STATUSES, Intent, IntentAction, StatusValue
from app.domain.types import InboundMessage, InterpretationContext
from app.integrations.gmail.client import MailClient
from app.integrations.notion.client import NotionClient

# Imported as a module, not `from fixtures.db import test_database_url`: pytest collects any
# module-level name starting with `test` in a test module, so a direct import would be
# collected as a test (and its return value flagged as a leak).
from fixtures import db as db_helpers
from fixtures.clock import FrozenClock, at_due_minus, at_due_plus, frozen_at
from fixtures.fakes import (
    FakeIntentInterpreter,
    FakeMailClient,
    FakeNotionClient,
    FakeNotionNotFound,
    FakeNotionTransient,
    FakeNotionUnreachable,
    make_container,
)
from fixtures.pages import make_assignment, make_course_page, make_exam, make_notion_page

# The due instant for the builders' default date-only due date: 2026-10-03 23:59 Eastern
# (EDT) = 2026-10-04T03:59Z — not 2026-10-03T23:59Z (CLAUDE.md constraint 3, FR-1).
EXPECTED_DUE_AT = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)


# ── Page builders ───────────────────────────────────────────────────────────


def test_assignment_has_the_property_shape_the_normalizer_reads() -> None:
    page = make_assignment()

    assert set(page.properties) == {"Name", "Course", "Type", "Due Date", "Status", "Done"}

    title = page.properties["Name"]
    assert title["type"] == "title"
    assert title["title"][0]["plain_text"] == "Problem Set 4"

    # Course is a Relation: a page ID, never a name (§2.3.2.A).
    assert page.properties["Course"] == {
        "id": "abc",
        "type": "relation",
        "relation": [{"id": "course-1"}],
        "has_more": False,
    }
    assert page.properties["Type"]["select"] == {"id": "x", "name": "Assignment"}
    assert page.properties["Due Date"]["date"] == {
        "start": "2026-10-03",
        "end": None,
        "time_zone": None,
    }
    assert page.properties["Status"]["type"] == "status"
    assert page.properties["Status"]["status"]["name"] == "Not started"
    assert page.properties["Done"] == {"id": "mno", "type": "checkbox", "checkbox": False}

    assert page.data_source_id == "ds-1"
    assert page.in_trash is False
    assert page.last_edited_time.tzinfo is not None


def test_title_key_follows_title_prop_so_discovery_is_by_type_not_name() -> None:
    page = make_notion_page(name="Read Ch. 7", title_prop="Task")

    assert "Name" not in page.properties
    assert page.properties["Task"]["type"] == "title"
    assert page.properties["Task"]["title"][0]["plain_text"] == "Read Ch. 7"


def test_an_unlinked_course_yields_an_empty_relation() -> None:
    page = make_assignment(course_page_id="")
    assert page.properties["Course"]["relation"] == []
    assert page.properties["Course"]["has_more"] is False


def test_include_done_false_models_a_database_without_the_checkbox() -> None:
    assert "Done" not in make_notion_page(include_done=False).properties
    assert make_notion_page(done=True).properties["Done"]["checkbox"] is True


def test_make_exam_sets_the_exam_type_and_honours_overrides() -> None:
    page = make_exam(name="Midterm 1", due_start="2026-11-06")
    assert page.properties["Type"]["select"]["name"] == "Exam"
    assert page.properties["Name"]["title"][0]["plain_text"] == "Midterm 1"
    assert page.properties["Due Date"]["date"]["start"] == "2026-11-06"


def test_course_page_carries_a_title_property() -> None:
    page = make_course_page(page_id="course-1", title="CSE 271")
    assert page.properties["Name"]["type"] == "title"
    assert page.properties["Name"]["title"][0]["plain_text"] == "CSE 271"
    assert page.page_id == "course-1"


def test_assignment_normalizes_into_an_item(settings: Settings) -> None:
    """The builder feeds the real normalizer, when it exists.

    `normalize_page` is written by another workstream, so the call adapts to whichever of
    the plausible configuration parameters it declares rather than assuming one signature.
    Absent module -> skip (the property-shape assertions above still cover the builder).
    """
    module = pytest.importorskip(
        "app.integrations.notion.normalize", reason="normalize.py not written yet"
    )
    normalize_page = getattr(module, "normalize_page", None)
    if normalize_page is None:
        pytest.skip("app.integrations.notion.normalize declares no normalize_page()")

    params = inspect.signature(normalize_page).parameters
    optional: dict[str, Any] = {
        "settings": settings,
        "source_db": "assignments_readings",
        "tz": settings.tz,
        "timezone": settings.timezone,
        "default_time_of_day": settings.default_due_time,
        "default_due_time": settings.default_due_time,
    }
    kwargs = {name: value for name, value in optional.items() if name in params}
    item = normalize_page(make_assignment(), **kwargs)

    assert item.name == "Problem Set 4"
    assert item.item_kind == "assignment_reading"
    assert item.due_date == date(2026, 10, 3)
    assert (
        item.due_at
        == EXPECTED_DUE_AT
        == compute_due_at(date(2026, 10, 3), ZoneInfo(settings.timezone))
    )
    assert item.due_has_time is False
    assert item.is_complete(settings.notion_status_completed) is False


# ── FakeNotionClient ────────────────────────────────────────────────────────


async def test_list_changed_pages_filters_on_last_edited_time_and_records_the_cursor() -> None:
    old = make_assignment(page_id="old", last_edited_time=datetime(2026, 9, 28, 10, 0, tzinfo=UTC))
    new = make_assignment(page_id="new", last_edited_time=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    client = FakeNotionClient([old, new])

    assert [page.page_id for page in await client.list_changed_pages("ds-1", None)] == [
        "old",
        "new",
    ]

    since = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    assert [page.page_id for page in await client.list_changed_pages("ds-1", since)] == ["new"]
    assert [page.page_id for page in await client.list_changed_pages("ds-2", since)] == []

    # The 5-minute overlap is applied by the caller, so the argument is recorded verbatim
    # (a sync test asserts since == last_success_cursor - 5min).
    assert client.changed_since_args() == [None, since, since]
    assert client.list_changed_calls[0].data_source_id == "ds-1"


async def test_trashed_pages_are_hidden_from_the_query_but_visible_to_the_full_reconcile() -> None:
    live = make_assignment(page_id="live")
    trashed = make_assignment(page_id="gone", in_trash=True)
    client = FakeNotionClient([live, trashed])

    assert [page.page_id for page in await client.list_changed_pages("ds-1", None)] == ["live"]
    assert [page.page_id for page in await client.list_all_pages("ds-1")] == ["live", "gone"]


async def test_get_page_returns_none_for_an_unknown_id_and_raises_when_notion_is_down() -> None:
    page = make_assignment(page_id="known")
    client = FakeNotionClient([page])

    assert await client.get_page("known") is page
    assert await client.get_page("missing") is None
    assert [call.page_id for call in client.get_page_calls] == ["known", "missing"]

    client.raise_on_get_page = True
    with pytest.raises(FakeNotionUnreachable):
        await client.get_page("known")


async def test_resolve_data_source_id_returns_the_recorded_mapping() -> None:
    client = FakeNotionClient()
    assert await client.resolve_data_source_id("db-assignments") == "ds-1"
    assert client.resolve_calls == ["db-assignments"]
    with pytest.raises(KeyError):
        await client.resolve_data_source_id("db-unknown")


async def test_fake_notion_update_page_applies_the_write_so_read_back_can_verify() -> None:
    """V1: the fake really mutates the page, which is what makes read-back meaningful."""
    client = FakeNotionClient([make_assignment(page_id="page-1")])

    await client.update_page(
        page_id="page-1",
        status_property="Status",
        status_name="Completed",
        done_property="Done",
        done_value=True,
        due_property="Due Date",
        due_date=date(2026, 10, 10),
    )

    assert len(client.update_page_calls) == 1
    page = await client.get_page("page-1")
    assert page is not None
    assert page.properties["Status"]["status"]["name"] == "Completed"
    assert page.properties["Done"]["checkbox"] is True
    assert page.properties["Due Date"]["date"]["start"] == "2026-10-10"


async def test_fake_notion_update_page_can_accept_a_write_that_does_not_land() -> None:
    """`drop_update` is the "PATCH succeeded but read-back disagrees" case (§2.3.4)."""
    client = FakeNotionClient([make_assignment(page_id="page-1")], drop_update=True)

    await client.update_page(
        page_id="page-1",
        status_property="Status",
        status_name="Completed",
        done_property="Done",
        done_value=True,
        due_property="Due Date",
        due_date=None,
    )

    assert len(client.update_page_calls) == 1  # the write was attempted...
    page = await client.get_page("page-1")
    assert page is not None
    assert page.properties["Status"]["status"]["name"] != "Completed"  # ...and did not land


async def test_fake_notion_update_page_raises_not_found_for_an_unknown_page() -> None:
    client = FakeNotionClient()
    with pytest.raises(FakeNotionNotFound):
        await client.update_page(
            page_id="page-1",
            status_property="Status",
            status_name="Completed",
            done_property="Done",
            done_value=True,
            due_property="Due Date",
            due_date=None,
        )


async def test_fake_notion_update_page_fails_transiently_then_succeeds() -> None:
    """`fail_update_times=1` is the writer's one-retry-then-re-verify path."""
    client = FakeNotionClient([make_assignment(page_id="page-1")], fail_update_times=1)

    with pytest.raises(FakeNotionTransient):
        await client.update_page(
            page_id="page-1",
            status_property="Status",
            status_name="Completed",
            done_property="Done",
            done_value=True,
            due_property="Due Date",
            due_date=None,
        )

    # The retry succeeds, and the write is applied.
    await client.update_page(
        page_id="page-1",
        status_property="Status",
        status_name="Completed",
        done_property="Done",
        done_value=True,
        due_property="Due Date",
        due_date=None,
    )
    page = await client.get_page("page-1")
    assert page is not None
    assert page.properties["Status"]["status"]["name"] == "Completed"


# ── FakeMailClient ──────────────────────────────────────────────────────────


async def test_send_is_recorded_and_find_sent_by_token_finds_it() -> None:
    mail = FakeMailClient()

    message = await mail.send(
        to="owner@example.test",
        subject="[Reminder] CSE 271: Problem Set 4, due Sat Oct 3 (in 48 hours)",
        body="Problem Set 4\n\nReply to mark completed or change the due date.\n\nref: abc123",
        thread_id=None,
        in_reply_to=None,
    )

    assert message.provider_thread_id == "fake-thread-1"  # thread_id=None starts a new thread
    assert len(mail.sent) == 1
    call = mail.sent[0]
    assert call.to == "owner@example.test"
    assert call.subject.startswith("[Reminder]")
    assert call.thread_id is None
    assert call.in_reply_to is None

    assert await mail.find_sent_by_token("abc123") is message
    # On a token boundary: a longer token is a different token.
    assert await mail.find_sent_by_token("abc") is None
    assert await mail.find_sent_by_token("nope") is None


async def test_send_failures_are_injectable_for_the_backoff_and_five_strike_paths() -> None:
    flaky = FakeMailClient(fail_times=2)
    with pytest.raises(RuntimeError):
        await flaky.send("owner@example.test", "s", "b", None, None)
    with pytest.raises(RuntimeError):
        await flaky.send("owner@example.test", "s", "b", None, None)
    await flaky.send("owner@example.test", "s", "b", None, None)
    assert flaky.send_attempts == 3
    assert len(flaky.sent) == 1  # only the successful attempt is recorded as sent

    broken = FakeMailClient(fail_always=True)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await broken.send("owner@example.test", "s", "b", None, None)
    assert broken.send_attempts == 5
    assert broken.sent == []
    assert await broken.find_sent_by_token("anything") is None


async def test_poll_new_drains_the_inbox_and_advances_the_cursor() -> None:
    """V1: the fake poll hands back what was queued, exactly once.

    Draining is the point — a second poll returns nothing, so a service-level test can tell
    "the poller saw it twice" apart from "the dedupe caught it the second time".
    """
    mail = FakeMailClient()
    message = InboundMessage(
        provider_message_id="m-1",
        provider_thread_id="t-1",
        from_address="owner@example.test",
        subject="Re: [Reminder] ...",
        in_reply_to="<out-1@example.test>",
        references=("<out-1@example.test>",),
        body_text="done",
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    mail.receive(message)

    first = await mail.poll_new(None)
    assert first.messages == (message,)
    assert first.history_id is not None

    second = await mail.poll_new(first.history_id)
    assert second.messages == ()
    assert mail.poll_calls == [None, first.history_id]


async def test_poll_new_raises_when_the_poll_is_configured_to_fail() -> None:
    mail = FakeMailClient(poll_error=RuntimeError("gmail exploded"))
    with pytest.raises(RuntimeError):
        await mail.poll_new(None)


# ── FakeIntentInterpreter ───────────────────────────────────────────────────


def _context() -> InterpretationContext:
    return InterpretationContext(
        reply_text="done",
        item_name="Problem Set 4",
        item_course="CSE 271",
        status="Not started",
        due_date=date(2026, 10, 3),
        allowed_statuses=ALLOWED_STATUSES,
    )


async def test_unscripted_interpret_raises_so_the_reminder_flow_never_reaches_an_llm() -> None:
    """The MVP guard survives into V1: an unscripted interpreter still refuses to guess."""
    interpreter = FakeIntentInterpreter()
    context = _context()
    with pytest.raises(NotImplementedError):
        await interpreter.interpret(context)
    assert interpreter.calls == [context]


async def test_scripted_interpret_returns_the_next_intent_in_order() -> None:
    first = Intent(action=IntentAction.NO_ACTION)
    second = Intent(action=IntentAction.CHANGE_STATUS, status=StatusValue.COMPLETED)
    interpreter = FakeIntentInterpreter([first, second])

    assert await interpreter.interpret(_context()) is first
    assert await interpreter.interpret(_context()) is second
    assert len(interpreter.calls) == 2


# ── Container, clock, settings ──────────────────────────────────────────────


def test_make_container_returns_a_fully_wired_container_of_fakes(
    settings: Settings, frozen_clock: FrozenClock
) -> None:
    notion = FakeNotionClient([make_assignment()])
    mail = FakeMailClient()
    container = make_container(settings, clock=frozen_clock, notion=notion, mail=mail)

    # These two annotations are the check: mypy verifies each fake satisfies the frozen port.
    wired_notion: NotionClient = container.notion
    wired_mail: MailClient = container.mail

    assert isinstance(container, AppContainer)
    assert wired_notion is notion
    assert wired_mail is mail
    assert isinstance(container.interpreter, FakeIntentInterpreter)
    assert container.clock is frozen_clock
    assert container.settings is settings
    assert container.session_factory is not None


def test_frozen_clock_helpers_land_on_the_target_instants() -> None:
    due_at = compute_due_at(date(2026, 10, 3), ZoneInfo("America/New_York"))
    assert due_at == EXPECTED_DUE_AT

    assert frozen_at("2026-10-01T00:00:00Z").now() == datetime(2026, 10, 1, tzinfo=UTC)
    # A naive string is read in the requested zone (EDT here: UTC-4).
    assert frozen_at("2026-10-01 20:00").now() == datetime(2026, 10, 2, tzinfo=UTC)

    assert at_due_minus(48).now() == due_at - timedelta(hours=48)
    assert at_due_plus(1).now() == due_at + timedelta(hours=1)
    assert at_due_minus(48).now_local().date() == date(2026, 10, 1)


def test_settings_fixture_outranks_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    # The safety property that keeps a test off production (CLAUDE.md constraint 1):
    # values passed to Settings explicitly win over os.environ, and `.env` is never read.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://someone/elsewhere")
    monkeypatch.setenv("REMINDER_RECIPIENT", "a-real-person@lehigh.edu")
    monkeypatch.setenv("NOTION_TOKEN", "secret_real_token")

    built: Settings = request.getfixturevalue("settings")

    assert built.database_url == db_helpers.DEFAULT_TEST_DATABASE_URL
    assert built.reminder_recipient == "owner@example.test"
    assert built.notion_token == "test-notion-token"
    assert built.timezone == "America/New_York"


def test_the_test_database_url_never_falls_back_to_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://someone/elsewhere")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert db_helpers.test_database_url() == db_helpers.DEFAULT_TEST_DATABASE_URL

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://localhost:5432/other_test")
    assert db_helpers.test_database_url() == "postgresql+psycopg://localhost:5432/other_test"
