"""Shared value types and the closed vocabularies from spec §2.3.5.

Pure: standard library plus dataclasses only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

# ── Closed vocabularies ─────────────────────────────────────────────────────

ItemKind = Literal["assignment_reading", "exam_project"]
SourceDb = Literal["assignments_readings", "exams_projects"]
ReminderType = Literal["assignment_48h", "assignment_24h", "exam_120h", "exam_48h"]
ReminderStatus = Literal["pending", "claimed", "sent", "skipped", "failed", "superseded"]
SkipReason = Literal[
    "missed_window",
    "item_completed",
    "item_inactive",
    "past_due",
    "superseded_by_later",
]

# The closed `audit_log.event_type` vocabulary (spec §2.3.5). Events marked (V1) are not
# emitted by the MVP — they are listed so the vocabulary stays visibly closed, not so
# they can be used.
AuditEvent = Literal[
    # MVP (build phases 1-3)
    "notion_sync",
    "notion_full_reconcile",
    "item_deactivated",
    "type_mismatch",
    "reminder_created",
    "reminder_sent",
    "reminder_skipped",
    "reminder_superseded",
    "reminder_failed",
    "presend_check_skipped",
    "system_alert_sent",
    # V1 (build phases 4-6)
    "inbound_email_received",
    "inbound_ignored",
    "inbound_unmapped",
    "reply_interpreted",
    "clarification_requested",
    "notion_update_attempted",
    "notion_update_succeeded",
    "notion_update_failed",
    # V1 addition (§2.3.2.E "flagged in the audit log"): a `processed_inbound_messages` row
    # left `processing` past its deadline. The spec required the row be flagged but did not
    # name the event, so this one is recorded in §2.3.5 as of the same change.
    "inbound_stuck",
]

# The database an item lives in is the primary determinant of its kind (spec §1.2).
SOURCE_DB_TO_KIND: Mapping[SourceDb, ItemKind] = {
    "assignments_readings": "assignment_reading",
    "exams_projects": "exam_project",
}

# The `Type` select is a *backup* classifier only, used when the database cannot classify.
NOTION_TYPE_TO_KIND: Mapping[str, ItemKind] = {
    "Assignment": "assignment_reading",
    "Exam": "exam_project",
}

REMINDER_TYPES: frozenset[str] = frozenset(
    {"assignment_48h", "assignment_24h", "exam_120h", "exam_48h"}
)

# ── Adapter/domain boundary types ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NotionPage:
    """One page as the Notion adapter hands it back. Fakes build these directly."""

    page_id: str
    data_source_id: str
    url: str | None
    properties: Mapping[str, Any]
    last_edited_time: datetime
    in_trash: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedItem:
    """A Notion page reduced to the fields the app stores (spec §2.3.5 `items`)."""

    notion_page_id: str
    notion_data_source_id: str
    source_db: SourceDb
    item_kind: ItemKind
    notion_type: str | None
    name: str
    course_page_id: str | None
    course: str | None
    status: str
    done: bool
    due_date: date | None
    due_at: datetime | None
    due_has_time: bool
    timezone: str
    notion_url: str | None
    notion_last_edited_time: datetime | None
    in_trash: bool

    def is_complete(self, completed_status_value: str) -> bool:
        """`Done` OR `Status == Completed`, checked independently (FR-3).

        Rows predating this system may have `Done = true` with `Status` stuck at
        `Not started`, so neither field alone is trusted.
        """
        return self.done or self.status == completed_status_value


@dataclass(frozen=True, slots=True)
class SentMessage:
    """A message the mail adapter accepted."""

    provider_message_id: str
    provider_thread_id: str
    rfc_message_id: str


# ── Planner inputs ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Target:
    """A reminder that should exist for an item, and when it should fire."""

    reminder_type: ReminderType
    target_at: datetime


@dataclass(frozen=True, slots=True)
class PlannerItem:
    """The item fields `plan_reminders` needs — and nothing else."""

    item_id: UUID
    notion_page_id: str
    item_kind: ItemKind
    status: str
    done: bool
    due_at: datetime | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class PlannerReminder:
    """An existing reminder row as the planner sees it."""

    reminder_id: UUID
    reminder_type: ReminderType
    due_at_snapshot: datetime
    target_at: datetime
    status: ReminderStatus
    skip_reason: SkipReason | None


# ── Reply flow (V1, build phases 4-6) ───────────────────────────────────────
#
# The structured intent itself lives in `app.domain.intents` — it is Pydantic, because
# §2.3.4 makes validating the model's output mandatory, and these are plain dataclasses.
# What is here is the *adapter boundary*: what the mail adapter hands back, and what the
# app hands to the interpreter. Neither crosses into business rules.


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One inbound email as the mail adapter hands it back (§2.3.2.E).

    Everything the pipeline needs to decide what to do with it, and nothing it does not:
    the two provider ids for thread→item mapping, the sender for the allowlist, the reply
    headers for the `References` fallback, the new reply text, and the two headers that
    identify an auto-reply.

    `auto_submitted` and `precedence` are carried as explicit values rather than as a raw
    header mapping so that auto-reply detection stays a pure, unit-testable predicate over
    a `InboundMessage` — and so a fake can construct one without synthesizing a MIME
    payload. Both are `None` when the header is absent.

    `body_text` is the *whole* body as received; stripping quoted history and signatures is
    `parse.extract_reply_text`'s job, deliberately kept out of the adapter so the stripping
    rules are testable without a Gmail response.
    """

    provider_message_id: str
    provider_thread_id: str | None
    from_address: str
    subject: str
    in_reply_to: str | None
    references: tuple[str, ...]
    body_text: str
    received_at: datetime
    auto_submitted: str | None = None
    precedence: str | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    """The result of one inbound poll (§2.3.6).

    `history_id` is the cursor to store for next time. It is returned even when `messages`
    is empty — a poll that found nothing still advanced Gmail's history, and not storing
    the new cursor would mean re-scanning the same window forever.
    """

    messages: tuple[InboundMessage, ...] = ()
    history_id: str | None = None


@dataclass(frozen=True, slots=True)
class InterpretationContext:
    """What the interpreter is allowed to see about one reply and one item (§2.3.4).

    Deliberately narrow. The item's name and course are context only; nothing else about
    the owner is sent. `pending_clarification` is present so a follow-up reply can be read
    against the question that prompted it.
    """

    reply_text: str
    item_name: str
    item_course: str | None
    status: str
    due_date: date | None
    allowed_statuses: tuple[str, ...]
    pending_clarification: str | None = None
