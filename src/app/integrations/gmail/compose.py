"""Email composition (spec §2.3.2.D, §2.3.4 templates, FR-5/FR-6).

Pure: no I/O, no clock, no config lookup. The timezone always arrives as a `ZoneInfo`
argument, so nothing here hardcodes a zone or a fixed offset (CLAUDE.md constraint 3).

Two invariants this module owns:

- **FR-6 — one item per email.** Every subject names exactly one item.
- **Stale-claim recovery.** The `ref: <ref_token>` footer is what
  `MailClient.find_sent_by_token` greps Gmail Sent for, so it is a contract, not decoration.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain.reminder_rules import relative_phrase
from app.domain.types import ItemKind, NormalizedItem, ReminderType

# Gmail truncates long subjects and the spec's subject shape is fixed, so the item name
# absorbs the whole budget. The full name is always in the body.
SUBJECT_MAX_LEN = 200

# ASCII, not "…": the subject is plain text read in many mail clients, and the truncation
# must be predictable when the message is quoted back in a reply.
_ELLIPSIS = "..."

_REPLY_HINT = "Reply to mark completed or change the due date"

_KIND_LABELS: Mapping[ItemKind, str] = {
    "assignment_reading": "Assignment / Reading",
    "exam_project": "Exam / Project",
}

_NO_DUE = "no due date"
_NO_COURSE = "(none)"
_NO_LINK = "(none)"


def format_due(due_at: datetime, tz: ZoneInfo, due_has_time: bool) -> str:
    """Render a due instant for a human, in `tz`.

    Date-only → `"Sat Oct 3"`. With a time → `"Sat Oct 3, 2:30 PM"`.

    The day is formatted as `f"{d:%a %b} {d.day}"` and **never** with `%-d`: `strftime`
    does not support the no-padding flag on Windows — this project's dev platform — and
    renders `%-d` literally as `"Oct %-d"`.
    """
    local = due_at.astimezone(tz)
    day = f"{local:%a %b} {local.day}"
    if not due_has_time:
        return day
    hour = (local.hour % 12) or 12  # 0 -> 12; 12 stays 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{day}, {hour}:{local.minute:02d} {meridiem}"


def reminder_subject(item: NormalizedItem, reminder_type: ReminderType, tz: ZoneInfo) -> str:
    """`[Reminder] {Course}: {Name}, due {Sat Oct 3} (in 48 hours)`.

    The `{Course}: ` segment is omitted entirely when the item has no course, and the
    relative phrase comes from `reminder_rules.relative_phrase` — never hardcoded here.
    """
    prefix = "[Reminder] "
    course = f"{item.course}: " if item.course else ""
    due = format_due(item.due_at, tz, item.due_has_time) if item.due_at else _NO_DUE
    suffix = f", due {due} ({relative_phrase(reminder_type)})"

    name = item.name
    budget = SUBJECT_MAX_LEN - len(prefix) - len(course) - len(suffix)
    if budget < len(_ELLIPSIS) + 1:
        name = name[: max(budget, 0)]
    elif len(name) > budget:
        name = name[: budget - len(_ELLIPSIS)] + _ELLIPSIS

    return f"{prefix}{course}{name}{suffix}"


def render_reminder(
    item: NormalizedItem,
    reminder_type: ReminderType,
    tz: ZoneInfo,
    ref_token: str,
) -> tuple[str, str]:
    """The `(subject, body)` pair for one reminder.

    Plain text, LF line endings only. The body carries the full (untruncated) item name,
    the course, the kind, the due date/time, the current status, the Notion link, the
    one-line reply hint, and the `ref: <ref_token>` footer.
    """
    subject = reminder_subject(item, reminder_type, tz)
    due = format_due(item.due_at, tz, item.due_has_time) if item.due_at else _NO_DUE
    lines = [
        item.name,
        "",
        f"Course: {item.course or _NO_COURSE}",
        f"Kind: {_KIND_LABELS[item.item_kind]}",
        f"Due: {due}",
        f"Status: {item.status}",
        "",
        item.notion_url or _NO_LINK,
        "",
        _REPLY_HINT,
        "",
        f"ref: {ref_token}",
    ]
    return subject, "\n".join(lines) + "\n"


def render_system_alert(alert_type: str, subject: str, body: str) -> tuple[str, str]:
    """The `(subject, body)` pair for an operational alert (§2.3.4, AlertService).

    `[Alert]` is not cosmetic: an alert must never be mistakable for a reminder, because
    the owner's own reply handling treats a reminder thread as actionable.
    """
    lines = [f"Alert: {alert_type}", "", body]
    return f"[Alert] {subject}", "\n".join(lines) + "\n"
