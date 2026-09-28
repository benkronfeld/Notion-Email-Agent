"""Unit tests for the pure email composer (§2.3.2.D, §2.3.4). No I/O, no clock.

The zone is passed explicitly, exactly as the caller passes `Settings.tz` — this keeps the
tests honest about the fact that `compose` hardcodes no zone (constraint 3).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.types import ItemKind, NormalizedItem, ReminderType, SourceDb
from app.integrations.gmail.compose import (
    SUBJECT_MAX_LEN,
    format_due,
    reminder_subject,
    render_reminder,
    render_system_alert,
)

ET = ZoneInfo("America/New_York")

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
