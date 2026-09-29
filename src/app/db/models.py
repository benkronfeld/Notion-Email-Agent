"""SQLAlchemy 2.0 models — the eight tables of spec §2.3.5, mapped field for field.

This module is the ORM half of the database contract. The handwritten initial migration
(`migrations/versions/0001_initial_schema.py`) is the migration half; the two are kept in
step by hand and must agree on every column, constraint, and index *name*.

Notes carried from the spec:

- Every `timestamptz` column is timezone-aware (`DateTime(timezone=True)`). The app never
  stores a naive datetime (CLAUDE.md constraint 4); `due_at` is UTC.
- `reminders.skip_reason` and `audit_log.event_type` deliberately have **no** CHECK
  constraint (spec §2.3.5 is explicit). Their closed vocabularies live in
  `app.domain.types` (`SkipReason`, `AuditEvent`).
- `audit_log` is append-only. The app never issues UPDATE or DELETE against it; the
  repository only exposes an INSERT.
- The two partial indexes (`items_active_due_idx`, `reminders_due_idx`) use
  `postgresql_where`; they cannot be expressed by autogenerate, which is why the initial
  migration is handwritten.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base. `Base.metadata` is Alembic's `target_metadata`."""


# ── items ───────────────────────────────────────────────────────────────────


class Item(Base):
    """A Notion page reduced to the fields the app stores (spec §2.3.5 `items`)."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("notion_page_id", name="uq_items_notion_page_id"),
        CheckConstraint(
            "source_db IN ('assignments_readings','exams_projects')",
            name="ck_items_source_db",
        ),
        CheckConstraint(
            "item_kind IN ('assignment_reading','exam_project')",
            name="ck_items_item_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    notion_page_id: Mapped[str] = mapped_column(Text, nullable=False)
    # parent.data_source_id (API version 2025-09-03+); resolved from source_db's
    # database_id at startup and cached in system_state.
    notion_data_source_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_db: Mapped[str] = mapped_column(Text, nullable=False)
    item_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # 'Assignment' | 'Exam' (backup classifier / metadata) — nullable.
    notion_type: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Raw Course relation target. `Course` is a Relation, so this is a page id.
    course_page_id: Mapped[str | None] = mapped_column(Text)
    # Resolved display name, cached from courses.name at upsert time.
    course: Mapped[str | None] = mapped_column(Text)
    # Notion value; unknown values are treated as active by the domain layer.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Notion 'Done' checkbox; independent of status (FR-3, §1.2).
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # As entered in Notion.
    due_date: Mapped[date | None] = mapped_column(Date)
    # UTC; a date-only due date is 23:59 in TIMEZONE (constraint 3).
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_has_time: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'America/New_York'")
    )
    notion_url: Mapped[str | None] = mapped_column(Text)
    # False when archived/deleted (FR-9). The upsert sets this from the page's in_trash.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notion_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notion_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Partial index: only active rows, by due time.
Index("items_active_due_idx", Item.due_at, postgresql_where=text("is_active"))


# ── reminders ───────────────────────────────────────────────────────────────


class Reminder(Base):
    """One scheduled reminder for one item (spec §2.3.5 `reminders`)."""

    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(
            "reminder_type IN ('assignment_48h','assignment_24h','exam_120h','exam_48h')",
            name="ck_reminders_reminder_type",
        ),
        CheckConstraint(
            "status IN ('pending','claimed','sent','skipped','failed','superseded')",
            name="ck_reminders_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_reminders_idempotency_key"),
        UniqueConstraint("ref_token", name="uq_reminders_ref_token"),
        UniqueConstraint(
            "item_id",
            "reminder_type",
            "due_at_snapshot",
            name="uq_reminders_item_id_reminder_type_due_at_snapshot",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE", name="fk_reminders_item_id"),
        nullable=False,
    )
    reminder_type: Mapped[str] = mapped_column(Text, nullable=False)
    # The due_at this target was computed from. A new due date is a new key (Option A).
    due_at_snapshot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    # missed_window | item_completed | item_inactive | past_due | superseded_by_later.
    # No CHECK constraint by design (spec §2.3.5).
    skip_reason: Mapped[str | None] = mapped_column(Text)
    # {notion_page_id}:{reminder_type}:{due_at_snapshot ISO UTC}
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Short alphanumeric token printed in the email footer (stale-claim recovery).
    ref_token: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Partial index: the scheduler only ever scans pending rows.
Index("reminders_due_idx", Reminder.target_at, postgresql_where=text("status = 'pending'"))


# ── email_threads ───────────────────────────────────────────────────────────


class EmailThread(Base):
    """One outbound conversation, keyed by the Gmail thread id (spec §2.3.5 `email_threads`)."""

    __tablename__ = "email_threads"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open','awaiting_clarification','closed')",
            name="ck_email_threads_state",
        ),
        # One reminder starts one Gmail thread (`thread_id=None` on send), so a second
        # thread row for the same Gmail thread is a duplicate mapping and must fail loudly
        # rather than shadow the first. This also makes the reply pipeline's thread→item
        # lookup a single-row lookup, which is where "exactly one item" comes from.
        # The migration has enforced this since 0001; the constraint is declared here too
        # so the model and the schema agree and an autogenerate renders an empty diff.
        UniqueConstraint("provider_thread_id", name="uq_email_threads_provider_thread_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE", name="fk_email_threads_item_id"),
        nullable=False,
    )
    reminder_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("reminders.id", name="fk_email_threads_reminder_id")
    )
    provider_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    root_rfc_message_id: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # 'closed' is set when MAX_CLARIFICATION_ROUNDS is exhausted; a reply in a closed
    # thread is still recorded (never reprocessed) but produces only a notice.
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    # {question, original_request}
    pending_clarification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    clarification_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── outbound_messages ───────────────────────────────────────────────────────


class OutboundMessage(Base):
    """A message the app sent (spec §2.3.5 `outbound_messages`)."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('reminder','clarification','confirmation','failure','notice','system_alert')",
            name="ck_outbound_messages_kind",
        ),
        UniqueConstraint("provider_message_id", name="uq_outbound_messages_provider_message_id"),
        UniqueConstraint("rfc_message_id", name="uq_outbound_messages_rfc_message_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("email_threads.id", ondelete="CASCADE", name="fk_outbound_messages_thread_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Used for In-Reply-To/References fallback mapping.
    rfc_message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── processed_inbound_messages ──────────────────────────────────────────────


class ProcessedInboundMessage(Base):
    """Dedupe + status for one inbound message (spec §2.3.5 `processed_inbound_messages`)."""

    __tablename__ = "processed_inbound_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing','done','failed','ignored')",
            name="ck_processed_inbound_messages_status",
        ),
        UniqueConstraint(
            "provider_message_id", name="uq_processed_inbound_messages_provider_message_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    provider_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    item_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("items.id", name="fk_processed_inbound_messages_item_id"),
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'processing'"))
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── audit_log ───────────────────────────────────────────────────────────────


class AuditLog(Base):
    """Append-only audit trail (spec §2.3.5 `audit_log`). Never UPDATEd or DELETEd."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # The closed vocabulary is `app.domain.types.AuditEvent`; deliberately no CHECK here.
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    notion_page_id: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index("audit_item_idx", AuditLog.item_id, AuditLog.created_at.desc())
Index("audit_type_idx", AuditLog.event_type, AuditLog.created_at.desc())


# ── system_state ────────────────────────────────────────────────────────────


class SystemState(Base):
    """Key/value store: cursors, the data-source cache, alert cooldowns (spec §2.3.5)."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── courses ─────────────────────────────────────────────────────────────────


class Course(Base):
    """Cache of the 'Course / Class' page titles the `Course` relation points at."""

    __tablename__ = "courses"

    notion_page_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
