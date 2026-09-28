"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-28

Hand-transliterated from spec §2.3.5. Autogenerate cannot express the two partial
indexes (`items_active_due_idx`, `reminders_due_idx`) and drops CHECK-constraint names,
so this file is written by hand. Constraint and index names line up with
`app.db.models` so a future `alembic revision --autogenerate` renders an empty diff.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Must be first: gen_random_uuid() backs every uuid primary key created below.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "items",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("notion_page_id", sa.Text(), nullable=False),
        sa.Column("notion_data_source_id", sa.Text(), nullable=False),
        sa.Column("source_db", sa.Text(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("notion_type", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("course_page_id", sa.Text(), nullable=True),
        sa.Column("course", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("done", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("due_at", _TSTZ, nullable=True),
        sa.Column("due_has_time", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "timezone",
            sa.Text(),
            server_default=sa.text("'America/New_York'"),
            nullable=False,
        ),
        sa.Column("notion_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notion_last_edited_at", _TSTZ, nullable=True),
        sa.Column("last_notion_sync_at", _TSTZ, nullable=True),
        sa.Column("created_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_items"),
        sa.UniqueConstraint("notion_page_id", name="uq_items_notion_page_id"),
        sa.CheckConstraint(
            "source_db IN ('assignments_readings','exams_projects')",
            name="ck_items_source_db",
        ),
        sa.CheckConstraint(
            "item_kind IN ('assignment_reading','exam_project')",
            name="ck_items_item_kind",
        ),
    )
    op.create_index(
        "items_active_due_idx",
        "items",
        ["due_at"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "reminders",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("item_id", _UUID, nullable=False),
        sa.Column("reminder_type", sa.Text(), nullable=False),
        sa.Column("due_at_snapshot", _TSTZ, nullable=False),
        sa.Column("target_at", _TSTZ, nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("ref_token", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", _TSTZ, nullable=True),
        sa.Column("claimed_at", _TSTZ, nullable=True),
        sa.Column("sent_at", _TSTZ, nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("provider_thread_id", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reminders"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_reminders_item_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "reminder_type IN ('assignment_48h','assignment_24h','exam_120h','exam_48h')",
            name="ck_reminders_reminder_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','claimed','sent','skipped','failed','superseded')",
            name="ck_reminders_status",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_reminders_idempotency_key"),
        sa.UniqueConstraint("ref_token", name="uq_reminders_ref_token"),
        sa.UniqueConstraint(
            "item_id",
            "reminder_type",
            "due_at_snapshot",
            name="uq_reminders_item_id_reminder_type_due_at_snapshot",
        ),
    )
    op.create_index(
        "reminders_due_idx",
        "reminders",
        ["target_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "email_threads",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("item_id", _UUID, nullable=False),
        sa.Column("reminder_id", _UUID, nullable=True),
        sa.Column("provider_thread_id", sa.Text(), nullable=False),
        sa.Column("root_rfc_message_id", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("pending_clarification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "clarification_rounds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("created_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_email_threads"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_email_threads_item_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reminder_id"],
            ["reminders.id"],
            name="fk_email_threads_reminder_id",
        ),
        sa.CheckConstraint(
            "state IN ('open','awaiting_clarification','closed')",
            name="ck_email_threads_state",
        ),
        sa.UniqueConstraint("provider_thread_id", name="uq_email_threads_provider_thread_id"),
    )

    op.create_table(
        "outbound_messages",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("thread_id", _UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("rfc_message_id", sa.Text(), nullable=True),
        sa.Column("sent_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_outbound_messages"),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["email_threads.id"],
            name="fk_outbound_messages_thread_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('reminder','clarification','confirmation','failure','notice','system_alert')",
            name="ck_outbound_messages_kind",
        ),
        sa.UniqueConstraint("provider_message_id", name="uq_outbound_messages_provider_message_id"),
        sa.UniqueConstraint("rfc_message_id", name="uq_outbound_messages_rfc_message_id"),
    )

    op.create_table(
        "processed_inbound_messages",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("provider_thread_id", sa.Text(), nullable=True),
        sa.Column("item_id", _UUID, nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'processing'"), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", _TSTZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_processed_inbound_messages"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_processed_inbound_messages_item_id",
        ),
        sa.CheckConstraint(
            "status IN ('processing','done','failed','ignored')",
            name="ck_processed_inbound_messages_status",
        ),
        sa.UniqueConstraint(
            "provider_message_id",
            name="uq_processed_inbound_messages_provider_message_id",
        ),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("item_id", _UUID, nullable=True),
        sa.Column("notion_page_id", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("provider_thread_id", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("audit_item_idx", "audit_log", ["item_id", sa.text("created_at DESC")])
    op.create_index("audit_type_idx", "audit_log", ["event_type", sa.text("created_at DESC")])

    op.create_table(
        "system_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_system_state"),
    )

    op.create_table(
        "courses",
        sa.Column("notion_page_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("last_synced_at", _TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("notion_page_id", name="pk_courses"),
    )


def downgrade() -> None:
    # Reverse dependency order. The pgcrypto extension is intentionally left in place:
    # `CREATE EXTENSION IF NOT EXISTS` cannot tell whether this migration created it, and
    # dropping a shared extension is not a safe reversal.
    op.drop_index("audit_type_idx", table_name="audit_log")
    op.drop_index("audit_item_idx", table_name="audit_log")
    op.drop_index("reminders_due_idx", table_name="reminders")
    op.drop_index("items_active_due_idx", table_name="items")

    op.drop_table("courses")
    op.drop_table("system_state")
    op.drop_table("audit_log")
    op.drop_table("processed_inbound_messages")
    op.drop_table("outbound_messages")
    op.drop_table("email_threads")
    op.drop_table("reminders")
    op.drop_table("items")
