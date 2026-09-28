"""Reminder rules (FR-2, spec §1.2).

The database an item lives in determines its kind, and the kind determines its schedule:

| Kind                 | Reminders                        |
|----------------------|----------------------------------|
| `assignment_reading` | 48h and 24h before due           |
| `exam_project`       | 120h (5 days) and 48h before due |

Offsets are absolute durations. Across a DST change the local wall-clock time shifts by
an hour; that is accepted (FR-2), not a defect to fix.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from app.domain.types import ItemKind, ReminderType, Target

RULES: Mapping[ItemKind, tuple[tuple[ReminderType, timedelta], ...]] = {
    "assignment_reading": (
        ("assignment_48h", timedelta(hours=48)),
        ("assignment_24h", timedelta(hours=24)),
    ),
    "exam_project": (
        ("exam_120h", timedelta(hours=120)),
        ("exam_48h", timedelta(hours=48)),
    ),
}

# Human phrasing for the "in ..." part of a reminder subject and body.
RELATIVE_PHRASE: Mapping[ReminderType, str] = {
    "assignment_48h": "in 48 hours",
    "assignment_24h": "in 24 hours",
    "exam_120h": "in 5 days",
    "exam_48h": "in 48 hours",
}


def offsets_for(kind: ItemKind) -> tuple[tuple[ReminderType, timedelta], ...]:
    """The (reminder_type, offset) pairs for an item kind, earliest offset first."""
    return RULES[kind]


def targets(kind: ItemKind, due_at: datetime) -> list[Target]:
    """Every reminder target for an item, derived from its due instant.

    `due_at` must be timezone-aware. Targets are absolute UTC instants.
    """
    if due_at.tzinfo is None:
        raise ValueError("due_at must be timezone-aware")
    return [
        Target(reminder_type=reminder_type, target_at=due_at - offset)
        for reminder_type, offset in RULES[kind]
    ]


def relative_phrase(reminder_type: ReminderType) -> str:
    """The human phrase for a reminder's distance from its due date."""
    return RELATIVE_PHRASE[reminder_type]
