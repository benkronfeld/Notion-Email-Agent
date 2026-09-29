"""Email composition (spec §2.3.2.D, §2.3.2.E, §2.3.4 templates, FR-5/FR-6).

Pure: no I/O, no clock, no config lookup. The timezone always arrives as a `ZoneInfo`
argument, so nothing here hardcodes a zone or a fixed offset (CLAUDE.md constraint 3).
The reply-flow templates take already-formatted dates for the same reason — the caller
owns the timezone, this module owns the wording.

Invariants this module owns:

- **FR-6 — one item per email.** Every subject names exactly one item.
- **Stale-claim recovery.** The `ref: <ref_token>` footer is what
  `MailClient.find_sent_by_token` greps Gmail Sent for, so it is a contract, not decoration.
- **Honesty.** A failure template never implies the change was made; a confirmation is
  only rendered from values read back out of Notion. The owner acts on these on the
  strength of the wording alone, so the wording is load-bearing.
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

# Subject tags. Each names the *kind* of mail before the client renders the thread, which
# matters in the Sent folder and in the outbound row. `[Notice]` and `[Alert]` are
# deliberately different words: a notice reports something that already happened and needs
# no action, while `render_system_alert`'s `[Alert]` means the system is broken. A notice
# must never be mistakable for an alert, or for a `[Reminder]`, which the owner's reply
# handling treats as actionable.
_REMINDER_TAG = "[Reminder] "
_CLARIFICATION_TAG = "[Clarification] "
_CONFIRMATION_TAG = "[Confirmation] "
_FAILURE_TAG = "[Failure] "
_NOTICE_TAG = "[Notice] "

_REPLY_HINT = "Reply to mark completed or change the due date"

_KIND_LABELS: Mapping[ItemKind, str] = {
    "assignment_reading": "Assignment / Reading",
    "exam_project": "Exam / Project",
}

_NO_DUE = "no due date"
_NO_COURSE = "(none)"
_NO_LINK = "(none)"

# §2.3.4's clarification lead. Fixed, and the important half of the message: the owner
# must be told in as many words that the reply did NOT move anything, or a silent no-op
# reads as a silent change.
_NOTHING_CHANGED = "I didn't change anything."

# §2.3.4's confirmation and failure wording, verbatim. The status word is never "Done":
# `Done` is a property name in both databases, so "status Not started -> Done" reads as a
# property write rather than a description of the result.
_UPDATED_LEAD = "Updated."
_VERIFIED_IN_NOTION = "Verified in Notion."
_FAILURE_LEAD = "I could NOT make that change (Notion did not confirm it). Nothing was changed."

# U+2192, the arrow §2.3.4's confirmation wording uses, and the one typographic character
# this module emits: the message is plain text and the arrow renders in every client this
# targets. Spelled out as a code point so a future edit never has to guess what it is.
_ARROW = chr(0x2192)


def _fit(name: str, budget: int) -> str:
    """Trim `name` to `budget` characters, marking the cut with `_ELLIPSIS` when it fits.

    When there is not even room for the ellipsis the name is cut silently — a subject that
    overran `SUBJECT_MAX_LEN` to display "..." would be worse than a truncated one.
    """
    if budget < len(_ELLIPSIS) + 1:
        return name[: max(budget, 0)]
    if len(name) > budget:
        return name[: budget - len(_ELLIPSIS)] + _ELLIPSIS
    return name


def _tagged_subject(tag: str, text: str) -> str:
    """`{tag}{text}`, with `text` trimmed so the whole subject fits `SUBJECT_MAX_LEN`.

    The name is the only part that grows without bound, so it absorbs the whole budget —
    the same trade `reminder_subject` makes.
    """
    return f"{tag}{_fit(text, SUBJECT_MAX_LEN - len(tag))}"


def _block(text: str) -> str:
    """Normalise caller-supplied text to this module's plain-text contract.

    LF only, and no trailing newline, so a template can place the text on its own lines and
    own the message's single final newline. The text can come from a Gmail body or a Notion
    value, either of which may carry CRLF. Internal blank lines are preserved.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


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
    course = f"{item.course}: " if item.course else ""
    due = format_due(item.due_at, tz, item.due_has_time) if item.due_at else _NO_DUE
    suffix = f", due {due} ({relative_phrase(reminder_type)})"

    budget = SUBJECT_MAX_LEN - len(_REMINDER_TAG) - len(course) - len(suffix)
    name = _fit(item.name, budget)

    return f"{_REMINDER_TAG}{course}{name}{suffix}"


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


def render_clarification(*, item_name: str, question: str) -> tuple[str, str]:
    """The `(subject, body)` pair for a clarification question (§2.3.2.E, §2.3.4).

    A clarification is what the system sends when it is *not* sure — an ambiguous date, an
    intent it could not read (§2.3.2.E steps 6-7). The fixed lead sentence is the half that
    matters: the reply changed nothing, and it says so in as many words. A no-op that is not
    announced reads as a silent change, and the owner acts on deadlines from these emails.

    `question` is the interpreter's or resolver's specific ask — the one concrete thing the
    owner has to answer, such as which of two dates was meant. It is passed through verbatim
    (LF-normalised); this module never invents a question of its own.
    """
    lines = [_block(item_name), "", _NOTHING_CHANGED, "", _block(question)]
    return _tagged_subject(_CLARIFICATION_TAG, item_name), "\n".join(lines) + "\n"


def render_confirmation(
    *,
    item_name: str,
    status_change: tuple[str, str] | None = None,
    due_change: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """The `(subject, body)` pair for a verified change (§2.3.4).

    Each argument is an `(old, new)` pair of already-formatted values, and a clause is
    emitted **only** when its pair was passed: a status-only reply must never claim the due
    date moved, and a due-only reply must never mention status. Callers reach this function
    only after the read-back matched (§2.3.2.E step 9), so the values stated here are the
    ones Notion actually holds — which is what "Verified in Notion." asserts. That sentence
    belongs to this template and is not a caller-supplied string for exactly that reason.

    The status word is never "Done": `Done` is a property name in both databases, so
    "status Not started → Done" would read as a property write rather than a result.
    """
    clauses: list[str] = []
    if status_change is not None:
        old, new = status_change
        clauses.append(f"status {old} {_ARROW} {new}")
    if due_change is not None:
        old, new = due_change
        clauses.append(f"due {old} {_ARROW} {new}")
    if not clauses:
        raise ValueError(
            "render_confirmation needs status_change and/or due_change: a verified write "
            "always changed at least one value, and an empty confirmation would be a lie."
        )

    lines = [f"{_UPDATED_LEAD} {_block(item_name)}: {'; '.join(clauses)}.", _VERIFIED_IN_NOTION]
    return _tagged_subject(_CONFIRMATION_TAG, item_name), "\n".join(lines) + "\n"


def render_failure(*, item_name: str, reason: str) -> tuple[str, str]:
    """The `(subject, body)` pair for a change that was NOT made (§2.3.2.E step 9, §2.3.4).

    This is the message the spec insists be honest: a PATCH that was rejected, or a read-back
    that disagreed with what was requested, means the deadline in Notion is still whatever it
    was. So the body says "NOT" and "Nothing was changed" and carries the reason verbatim —
    never a softened "there was a problem", which invites the owner to assume the change
    landed anyway, and never a success claim.
    """
    lines = [_block(item_name), "", _FAILURE_LEAD, "", f"Reason: {_block(reason)}"]
    return _tagged_subject(_FAILURE_TAG, item_name), "\n".join(lines) + "\n"


def render_notice(*, subject: str, body: str) -> tuple[str, str]:
    """The `(subject, body)` pair for a plain informational notice (§2.3.2.E).

    Two cases use it, and both mean "there is nothing to do here": a reply that could not be
    mapped to an item (step 3 — the system does not guess which item was meant) and a reply
    to a thread whose clarification rounds are exhausted, which points the owner back to
    Notion instead of asking again.

    A notice is **not** an alert. `[Notice]` reports something that already happened and
    needs no action; `render_system_alert`'s `[Alert]` means the system is broken and the
    owner is being paged. Different words, so neither is mistakable for the other — or for a
    `[Reminder]`, which the reply handling treats as actionable.
    """
    lines = [_block(body)]
    return _tagged_subject(_NOTICE_TAG, subject), "\n".join(lines) + "\n"
