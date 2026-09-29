"""Unit tests for the pure email composer (§2.3.2.D, §2.3.2.E, §2.3.4). No I/O, no clock.

The zone is passed explicitly, exactly as the caller passes `Settings.tz` — this keeps the
tests honest about the fact that `compose` hardcodes no zone (constraint 3).

The reply-flow templates (clarification, confirmation, failure, notice) are checked for the
two things a caller cannot verify by reading its own code: that each message states what
actually happened to the item, and that a failure never reads like a success.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.types import ItemKind, NormalizedItem, ReminderType, SourceDb
from app.integrations.gmail.compose import (
    SUBJECT_MAX_LEN,
    format_due,
    reminder_subject,
    render_clarification,
    render_confirmation,
    render_failure,
    render_notice,
    render_reminder,
    render_system_alert,
)

ET = ZoneInfo("America/New_York")

# U+2192, the arrow §2.3.4 uses between the old and new value. Spelled out so no encoding
# mistake can hide inside an expected string.
ARROW = chr(0x2192)

LONG_NAME = "A very long assignment name " * 20  # 580 characters

# 2026-10-03 23:59 Eastern (EDT) — the date-only rule's 11:59 PM (FR-1).
DUE_AT_DATE_ONLY = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
# 2026-10-03 14:30 Eastern.
DUE_AT_WITH_TIME = datetime(2026, 10, 3, 18, 30, tzinfo=UTC)

TOKEN = "a1b2c3d4e5f6"


def make_item(
    *,
    name: str = "Essay 2",
    course: str | None = "CSE 101",
    item_kind: ItemKind = "assignment_reading",
    source_db: SourceDb = "assignments_readings",
    status: str = "In progress",
    due_at: datetime = DUE_AT_DATE_ONLY,
    due_has_time: bool = False,
    notion_url: str | None = "https://www.notion.so/abc123",
) -> NormalizedItem:
    return NormalizedItem(
        notion_page_id="page-1",
        notion_data_source_id="ds-1",
        source_db=source_db,
        item_kind=item_kind,
        notion_type="Assignment",
        name=name,
        course_page_id="course-1" if course else None,
        course=course,
        status=status,
        done=False,
        due_date=date(2026, 10, 3),
        due_at=due_at,
        due_has_time=due_has_time,
        timezone="America/New_York",
        notion_url=notion_url,
        notion_last_edited_time=None,
        in_trash=False,
    )


def subject_for(reminder_type: ReminderType = "assignment_48h", **kwargs: Any) -> str:
    return reminder_subject(make_item(**kwargs), reminder_type, ET)


# ── Subject ─────────────────────────────────────────────────────────────────


def test_subject_with_course() -> None:
    assert subject_for() == "[Reminder] CSE 101: Essay 2, due Sat Oct 3 (in 48 hours)"


def test_subject_omits_course_segment_entirely_when_course_is_none() -> None:
    assert subject_for(course=None) == "[Reminder] Essay 2, due Sat Oct 3 (in 48 hours)"


def test_exam_120h_uses_the_five_day_phrase() -> None:
    subject = subject_for(
        "exam_120h",
        item_kind="exam_project",
        source_db="exams_projects",
        name="Midterm 1",
    )
    assert subject == "[Reminder] CSE 101: Midterm 1, due Sat Oct 3 (in 5 days)"


def test_day_of_month_has_no_leading_zero() -> None:
    subject = subject_for()
    assert "Sat Oct 3 " in subject
    assert "Oct 03" not in subject


def test_due_has_time_renders_the_time() -> None:
    subject = subject_for(due_at=DUE_AT_WITH_TIME, due_has_time=True)
    assert subject == "[Reminder] CSE 101: Essay 2, due Sat Oct 3, 2:30 PM (in 48 hours)"


def test_time_renders_midnight_as_twelve_am() -> None:
    # 2026-10-03 00:05 Eastern -> "12:05 AM", not "0:05 AM".
    due_at = datetime(2026, 10, 3, 4, 5, tzinfo=UTC)
    assert format_due(due_at, ET, True) == "Sat Oct 3, 12:05 AM"


def test_time_renders_noon_as_twelve_pm() -> None:
    due_at = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)  # 12:00 Eastern
    assert format_due(due_at, ET, True) == "Sat Oct 3, 12:00 PM"


def test_date_only_ignores_the_clock_time() -> None:
    assert format_due(DUE_AT_WITH_TIME, ET, False) == "Sat Oct 3"


def test_long_name_is_truncated_in_subject_but_complete_in_body() -> None:
    long_name = "A very long assignment name " * 20  # 580 characters
    _, body = render_reminder(make_item(name=long_name), "assignment_48h", ET, TOKEN)
    subject = subject_for(name=long_name)

    assert len(subject) <= SUBJECT_MAX_LEN
    assert "..." in subject
    assert subject.startswith("[Reminder] CSE 101: A very long assignment name")
    assert subject.endswith("(in 48 hours)")
    assert long_name in body  # the body always carries the full name


# ── Body ────────────────────────────────────────────────────────────────────


def test_body_contains_every_required_field() -> None:
    subject, body = render_reminder(make_item(), "assignment_48h", ET, TOKEN)

    assert subject.startswith("[Reminder] ")
    assert "Essay 2" in body
    assert "Course: CSE 101" in body
    assert "Kind: Assignment / Reading" in body
    assert "Due: Sat Oct 3" in body
    assert "Status: In progress" in body
    assert "https://www.notion.so/abc123" in body
    assert "Reply to mark completed or change the due date" in body
    assert f"ref: {TOKEN}" in body


def test_body_is_plain_text_with_lf_only() -> None:
    subject, body = render_reminder(make_item(), "exam_48h", ET, TOKEN)

    assert "\r" not in subject
    assert "\r" not in body
    assert "<html" not in body.lower()


def test_body_records_a_missing_course_and_link_rather_than_omitting_them() -> None:
    _, body = render_reminder(make_item(course=None, notion_url=None), "assignment_24h", ET, TOKEN)

    assert "Course: (none)" in body
    assert body.count("(none)") == 2  # course and link


# ── Alerts ──────────────────────────────────────────────────────────────────


def test_system_alert_subject_is_tagged() -> None:
    subject, body = render_system_alert("send_failed", "3 reminders failed", "SMTP 550")

    assert subject == "[Alert] 3 reminders failed"
    assert "[Reminder]" not in subject
    assert "Alert: send_failed" in body
    assert "SMTP 550" in body
    assert "\r" not in body


# ── Clarification (§2.3.2.E, §2.3.4) ────────────────────────────────────────


def test_clarification_says_nothing_was_changed_and_carries_the_question() -> None:
    subject, body = render_clarification(
        item_name="Essay 2",
        question="Did you mean Fri Oct 3 or Fri Oct 10?",
    )

    assert subject.startswith("[Clarification] ")
    assert "Essay 2" in subject
    assert "I didn't change anything." in body
    assert "Did you mean Fri Oct 3 or Fri Oct 10?" in body
    assert "Essay 2" in body


def test_clarification_never_claims_a_change_was_made() -> None:
    _, body = render_clarification(item_name="Essay 2", question="Which date?")

    assert "Updated" not in body
    assert "Verified in Notion." not in body


# ── Confirmation (§2.3.4) ───────────────────────────────────────────────────


def test_confirmation_reports_a_status_change_only() -> None:
    subject, body = render_confirmation(
        item_name="Essay 2", status_change=("In progress", "Completed")
    )

    assert subject.startswith("[Confirmation] ")
    assert f"status In progress {ARROW} Completed" in body
    assert "due" not in body  # the due date did not change, so no clause claims it


def test_confirmation_reports_a_due_change_only() -> None:
    subject, body = render_confirmation(item_name="Essay 2", due_change=("Sat Oct 3", "Fri Oct 9"))

    assert subject.startswith("[Confirmation] ")
    assert f"due Sat Oct 3 {ARROW} Fri Oct 9" in body
    assert "status" not in body


def test_confirmation_reports_both_changes_in_one_sentence() -> None:
    _, body = render_confirmation(
        item_name="Essay 2",
        status_change=("In progress", "Completed"),
        due_change=("Sat Oct 3", "Fri Oct 9"),
    )

    assert body == (
        f"Updated. Essay 2: status In progress {ARROW} Completed; due Sat Oct 3 {ARROW} "
        "Fri Oct 9.\nVerified in Notion.\n"
    )


def test_confirmation_endorses_the_change_as_verified_in_notion() -> None:
    _, body = render_confirmation(item_name="Essay 2", status_change=("Not started", "Completed"))

    assert "Verified in Notion." in body
    assert "NOT" not in body


def test_confirmation_does_not_use_done_as_a_status_word() -> None:
    # §2.3.4 is explicit: "Done" is the name of a Notion property in both databases, so a
    # sentence containing it reads as a property write rather than as the result.
    subject, body = render_confirmation(
        item_name="Essay 2", status_change=("In progress", "Completed")
    )

    assert "Done" not in subject
    assert "Done" not in body


def test_confirmation_refuses_to_render_a_change_that_changed_nothing() -> None:
    with pytest.raises(ValueError, match="status_change"):
        render_confirmation(item_name="Essay 2")


# ── Failure (§2.3.2.E step 9, §2.3.4) ───────────────────────────────────────


def test_failure_is_honest_about_the_change_not_happening() -> None:
    subject, body = render_failure(item_name="Essay 2", reason="Notion returned 400: bad payload")

    assert subject.startswith("[Failure] ")
    assert "I could NOT make that change (Notion did not confirm it). Nothing was changed." in body
    assert "Notion returned 400: bad payload" in body
    assert "Essay 2" in body


def test_failure_never_implies_success() -> None:
    _, body = render_failure(item_name="Essay 2", reason="read-back mismatch")

    assert "Verified in Notion." not in body
    assert "Updated." not in body


# ── Notice (§2.3.2.E step 3, closed threads) ────────────────────────────────


def test_notice_is_distinguishable_from_an_alert() -> None:
    notice_subject, _ = render_notice(
        subject="Couldn't match your reply", body="Reply directly to a reminder email."
    )
    alert_subject, _ = render_system_alert("gmail_auth_failed", "Gmail rejected us", "detail")

    assert notice_subject == "[Notice] Couldn't match your reply"
    assert alert_subject.startswith("[Alert] ")
    assert "[Alert]" not in notice_subject
    assert "[Notice]" not in alert_subject
    assert "[Reminder]" not in notice_subject


def test_notice_carries_the_callers_body_verbatim() -> None:
    _, body = render_notice(
        subject="Reply to a closed thread",
        body="This thread is closed. Please edit the item in Notion directly.",
    )

    assert body == "This thread is closed. Please edit the item in Notion directly.\n"


# ── Shared plain-text contract for every template ───────────────────────────


def _every_message() -> list[tuple[str, str]]:
    """One rendered pair per template — the surface the plain-text contract covers."""
    item = make_item()
    return [
        render_reminder(item, "assignment_48h", ET, TOKEN),
        render_system_alert("notion_sync_failed", "Sync failed", "detail"),
        render_clarification(item_name="Essay 2", question="Which date?"),
        render_confirmation(item_name="Essay 2", status_change=("In progress", "Completed")),
        render_failure(item_name="Essay 2", reason="read-back mismatch"),
        render_notice(subject="A notice", body="Nothing to do."),
    ]


def test_every_body_ends_with_exactly_one_newline_and_uses_lf_only() -> None:
    for subject, body in _every_message():
        assert body.endswith("\n"), body
        assert not body.endswith("\n\n"), body
        assert "\r" not in subject
        assert "\r" not in body


def test_every_subject_stays_within_the_length_budget_for_a_long_item_name() -> None:
    name_carrying = [
        render_clarification(item_name=LONG_NAME, question="Which date?"),
        render_confirmation(item_name=LONG_NAME, status_change=("In progress", "Completed")),
        render_failure(item_name=LONG_NAME, reason="read-back mismatch"),
    ]
    for subject, body in name_carrying:
        assert len(subject) <= SUBJECT_MAX_LEN, subject
        assert "..." in subject
        assert LONG_NAME in body  # the body always carries the full name

    # A notice's subject is caller text rather than an item name, but it shares the budget.
    notice_subject, _ = render_notice(subject=LONG_NAME, body="Nothing to do.")
    assert len(notice_subject) <= SUBJECT_MAX_LEN
    assert "..." in notice_subject


def test_crlf_from_a_caller_supplied_block_is_normalised() -> None:
    _, body = render_notice(subject="A notice", body="Line one\r\nLine two\r\n")

    assert "\r" not in body
    assert "Line one\nLine two\n" in body
    assert body.endswith("Line two\n")
