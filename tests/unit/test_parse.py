"""Unit tests for inbound Gmail parsing and the history-poll helpers (§2.3.2.E).

Pure: everything is a literal string or a literal Gmail response dict. No test here makes a
live Gmail call (CLAUDE.md constraint 5); the responses are the shapes the API returns,
copied from the documentation.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

from app.domain.types import InboundMessage
from app.integrations.gmail.parse import (
    extract_reply_text,
    header_value,
    is_auto_reply,
    parse_from_address,
    parse_message_payload,
    parse_rfc_message_ids,
)
from app.integrations.gmail.poller import (
    history_message_ids,
    new_history_id,
    search_query_fallback,
)

RECEIVED_AT = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


def make_message(
    *, auto_submitted: str | None = None, precedence: str | None = None
) -> InboundMessage:
    """An `InboundMessage` carrying only the two headers `is_auto_reply` reads."""
    return InboundMessage(
        provider_message_id="m1",
        provider_thread_id="t1",
        from_address="owner@example.com",
        subject="Re: [Reminder] CSE 101: Essay 2, due Sat Oct 3 (in 48 hours)",
        in_reply_to="<abc@mail.example.com>",
        references=("<abc@mail.example.com>",),
        body_text="done",
        received_at=RECEIVED_AT,
        auto_submitted=auto_submitted,
        precedence=precedence,
    )


def b64(text: str) -> str:
    """URL-safe base64 as Gmail stores it: padding stripped."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def full_message(
    *,
    headers: list[tuple[str, str]] | None = None,
    body: str = "Yes, done.",
    message_id: str = "m1",
    thread_id: str | None = "t1",
    internal_date: str = "1700000000000",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A minimal `users.messages.get(format="full")` response."""
    if payload is None:
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": name, "value": value} for name, value in headers or []],
            "body": {"data": b64(body)},
        }
    message: dict[str, Any] = {
        "id": message_id,
        "internalDate": internal_date,
        "payload": payload,
    }
    if thread_id is not None:
        message["threadId"] = thread_id
    return message


# ── extract_reply_text ──────────────────────────────────────────────────────


def test_extract_reply_text_drops_quoted_lines() -> None:
    body = "Yes, done.\n\n> [Reminder] CSE 101: Essay 2\n> due Sat Oct 3\n"
    assert extract_reply_text(body) == "Yes, done."


def test_extract_reply_text_cuts_at_attribution_line() -> None:
    body = (
        "Set it to Friday.\n"
        "Thanks\n"
        "\n"
        "On Mon, Sep 28, 2026 at 3:00 PM Alice <a@b.com> wrote:\n"
        "> [Reminder] CSE 101: Essay 2\n"
    )
    assert extract_reply_text(body) == "Set it to Friday.\nThanks"


def test_extract_reply_text_cuts_at_signature_delimiter() -> None:
    body = "Done, thanks.\n\n-- \nBen Kronfeld\nbk@lehigh.edu\n"
    assert extract_reply_text(body) == "Done, thanks."


def test_extract_reply_text_cuts_at_original_message_marker() -> None:
    body = "See below.\n\n-----Original Message-----\nFrom: reminders@example.com\n"
    assert extract_reply_text(body) == "See below."


def test_extract_reply_text_keeps_inline_reply_between_quotes() -> None:
    body = "> Did you finish?\nYes, actually.\n> Great.\n"
    assert extract_reply_text(body) == "Yes, actually."


def test_extract_reply_text_handles_crlf() -> None:
    body = "Yes, done.\r\n\r\n> quoted\r\n> more\r\n"
    assert extract_reply_text(body) == "Yes, done."


def test_extract_reply_text_trims_surrounding_blank_lines() -> None:
    assert extract_reply_text("\n\nHi there   \n\n") == "Hi there"


def test_extract_reply_text_returns_original_when_stripping_would_empty_it() -> None:
    # A reply that was *only* a quote must not reach the interpreter as an empty string.
    body = "> [Reminder] CSE 101: Essay 2\n> due Sat Oct 3\n"
    assert extract_reply_text(body) == body.strip()


def test_extract_reply_text_of_empty_body_is_empty() -> None:
    assert extract_reply_text("") == ""


# ── parse_from_address ──────────────────────────────────────────────────────


def test_parse_from_address_angle_form_lowercased() -> None:
    assert parse_from_address("Ben Kronfeld <Ben@Example.COM>") == "ben@example.com"


def test_parse_from_address_bare_form() -> None:
    assert parse_from_address("A@B.COM") == "a@b.com"


def test_parse_from_address_strips_surrounding_whitespace() -> None:
    assert parse_from_address("  Owner@Lehigh.EDU  ") == "owner@lehigh.edu"


def test_parse_from_address_with_punctuated_display_name() -> None:
    assert parse_from_address('"Kronfeld, Ben" <bk@lehigh.edu>') == "bk@lehigh.edu"


# ── is_auto_reply ───────────────────────────────────────────────────────────


def test_is_auto_reply_true_for_auto_submitted() -> None:
    assert is_auto_reply(make_message(auto_submitted="auto-generated")) is True


def test_is_auto_reply_false_for_auto_submitted_no() -> None:
    assert is_auto_reply(make_message(auto_submitted="no")) is False


def test_is_auto_reply_true_for_precedence_bulk() -> None:
    assert is_auto_reply(make_message(precedence="bulk")) is True


def test_is_auto_reply_true_for_precedence_auto_reply_case_insensitive() -> None:
    assert is_auto_reply(make_message(precedence="AUTO_REPLY")) is True


def test_is_auto_reply_true_for_precedence_junk() -> None:
    assert is_auto_reply(make_message(precedence="junk")) is True


def test_is_auto_reply_false_for_precedence_list() -> None:
    assert is_auto_reply(make_message(precedence="list")) is False


def test_is_auto_reply_false_when_headers_absent() -> None:
    assert is_auto_reply(make_message()) is False


# ── parse_rfc_message_ids ───────────────────────────────────────────────────


def test_parse_rfc_message_ids_none_and_empty() -> None:
    assert parse_rfc_message_ids(None) == ()
    assert parse_rfc_message_ids("") == ()


def test_parse_rfc_message_ids_single() -> None:
    assert parse_rfc_message_ids("<abc@mail.example.com>") == ("<abc@mail.example.com>",)


def test_parse_rfc_message_ids_multiple_preserve_order() -> None:
    raw = "<second@x.com> <first@x.com>"
    assert parse_rfc_message_ids(raw) == ("<second@x.com>", "<first@x.com>")


def test_parse_rfc_message_ids_strip_folded_whitespace() -> None:
    raw = "\n <one@x.com>\n\t<two@x.com> \n"
    assert parse_rfc_message_ids(raw) == ("<one@x.com>", "<two@x.com>")


# ── header_value ────────────────────────────────────────────────────────────


def test_header_value_is_case_insensitive() -> None:
    assert header_value({"From": "a@b.com"}, "from") == "a@b.com"
    assert header_value({"from": "a@b.com"}, "FROM") == "a@b.com"


def test_header_value_missing_returns_none() -> None:
    assert header_value({"From": "a@b.com"}, "Subject") is None


# ── parse_message_payload ───────────────────────────────────────────────────


def test_parse_message_payload_reads_every_field() -> None:
    message = full_message(
        headers=[
            ("From", "Ben Kronfeld <BK@Lehigh.EDU>"),
            ("Subject", "Re: [Reminder] CSE 101: Essay 2"),
            ("In-Reply-To", "<abc@mail.example.com>"),
            ("References", "<abc@mail.example.com> <def@mail.example.com>"),
            ("Auto-Submitted", "auto-replied"),
            ("Precedence", "bulk"),
        ],
        body="Yes, done.\n\n> quoted\n",
    )

    parsed = parse_message_payload(message)

    assert parsed.provider_message_id == "m1"
    assert parsed.provider_thread_id == "t1"
    assert parsed.from_address == "bk@lehigh.edu"
    assert parsed.subject == "Re: [Reminder] CSE 101: Essay 2"
    assert parsed.in_reply_to == "<abc@mail.example.com>"
    assert parsed.references == (
        "<abc@mail.example.com>",
        "<def@mail.example.com>",
    )
    assert parsed.body_text == "Yes, done.\n\n> quoted\n"
    assert parsed.auto_submitted == "auto-replied"
    assert parsed.precedence == "bulk"
    assert is_auto_reply(parsed) is True


def test_parse_message_payload_optional_headers_are_none_when_absent() -> None:
    parsed = parse_message_payload(full_message())
    assert parsed.in_reply_to is None
    assert parsed.references == ()
    assert parsed.auto_submitted is None
    assert parsed.precedence is None


def test_parse_message_payload_decodes_epoch_milliseconds_to_aware_utc() -> None:
    parsed = parse_message_payload(full_message(internal_date="1700000000000"))
    assert parsed.received_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert parsed.received_at.tzinfo is not None
    assert parsed.received_at.utcoffset() == datetime(2023, 11, 14, tzinfo=UTC).utcoffset()


def test_parse_message_payload_prefers_text_plain_part_of_alternative() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "From", "value": "owner@example.com"}],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("the plain reply")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>the html reply</p>")}},
        ],
    }
    parsed = parse_message_payload(full_message(payload=payload))
    assert parsed.body_text == "the plain reply"


def test_parse_message_payload_thread_id_may_be_absent() -> None:
    parsed = parse_message_payload(full_message(thread_id=None))
    assert parsed.provider_thread_id is None


# ── history_message_ids / new_history_id ────────────────────────────────────


def test_history_message_ids_deduplicates_across_records() -> None:
    response = {
        "historyId": "999",
        "history": [
            {"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]},
            {"messages": [{"id": "m1", "threadId": "t1"}]},
        ],
    }
    assert history_message_ids(response) == ["m1", "m2"]


def test_history_message_ids_reads_messages_added_shape() -> None:
    response = {
        "history": [
            {"messagesAdded": [{"message": {"id": "m3", "threadId": "t3"}}]},
            {"messages": [{"id": "m4", "threadId": "t4"}]},
        ]
    }
    assert history_message_ids(response) == ["m3", "m4"]


def test_history_message_ids_excludes_already_known() -> None:
    response = {"history": [{"messages": [{"id": "m1"}, {"id": "m2"}]}]}
    assert history_message_ids(response, already_known={"m1"}) == ["m2"]


def test_history_message_ids_empty_response() -> None:
    assert history_message_ids({}) == []
    assert history_message_ids({"history": []}) == []


def test_new_history_id_present_and_absent() -> None:
    assert new_history_id({"historyId": "12345"}) == "12345"
    assert new_history_id({"history": []}) is None


def test_search_query_fallback_is_the_bounded_inbox_search() -> None:
    assert search_query_fallback() == "in:inbox newer_than:2d"
