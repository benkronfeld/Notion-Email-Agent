"""`processed_inbound_messages` repository — dedupe and per-message lifecycle (§2.3.2.E).

This table is the *first* thing the reply pipeline touches and the *last* thing it updates,
and that order is the whole point: §2.3.2.E step 1 is the dedupe, so a message that has
already been handled is stopped before a sender check, before an item lookup, and long
before any LLM call or write.

`claim` is where exactly-once inbound processing actually lives. It is an
`INSERT ... ON CONFLICT DO NOTHING` against `UNIQUE(provider_message_id)`, so the database —
not a SELECT-then-INSERT race in application code — decides who wins. Two pollers, an
overlapping deploy, and a Gmail history hiccup that re-delivers the same message all collapse
to one row and one `None`.

The `processing` row is written *before* the work and moved to a terminal state after it.
That ordering is what makes a crash visible: a row left `processing` means a reply was
picked up and never finished, and §2.3.2.E is explicit that such a row is **flagged and
alerted, never silently retried** — a retry could double a confirmation email or, worse,
double a write.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedInboundMessage

# The `processed_inbound_messages.status` CHECK values (§2.3.5).
InboundStatus = Literal["processing", "done", "failed", "ignored"]


async def claim(
    session: AsyncSession,
    *,
    provider_message_id: str,
    provider_thread_id: str | None,
) -> ProcessedInboundMessage | None:
    """Claim one inbound message for processing, or `None` if it was already claimed.

    `None` means duplicate and the caller must stop immediately (§2.3.2.E step 1) — it is
    not an error and not a reason to continue with partial handling.

    The row is created with the column default `status = 'processing'`, so the claim and
    the "I am now working on this" record are the same write. There is no window in which a
    message is claimed but unrecorded.
    """
    result = await session.execute(
        pg_insert(ProcessedInboundMessage)
        .values(
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
        )
        # `index_elements` rather than `constraint=`: the conflict target is the unique
        # constraint on this column, and naming the column keeps this working even if the
        # constraint is ever renamed.
        .on_conflict_do_nothing(index_elements=["provider_message_id"])
        .returning(ProcessedInboundMessage)
        .execution_options(populate_existing=True)
    )
    return result.scalars().one_or_none()


async def finish(
    session: AsyncSession,
    message_id: UUID,
    *,
    status: InboundStatus,
    item_id: UUID | None,
    result: str | None,
    now: datetime,
) -> None:
    """Move a claimed message to a terminal state.

    `item_id` is recorded once the reply has been mapped to an item, so the audit trail can
    answer "which inbound message touched which item" without parsing `result`.

    No row is ever updated twice: `claim` creates it exactly once, and this is the single
    terminal transition. A message that is never finished stays `processing`, which
    `list_stuck` turns into an alert rather than a retry.
    """
    await session.execute(
        update(ProcessedInboundMessage)
        .where(ProcessedInboundMessage.id == message_id)
        .values(
            status=status,
            item_id=item_id,
            result=result,
            completed_at=now,
        )
    )


async def list_stuck(
    session: AsyncSession,
    *,
    now: datetime,
    stuck_after: timedelta,
) -> list[ProcessedInboundMessage]:
    """Rows left `processing` past `stuck_after` — a crash between claim and finish.

    Read-only on purpose. §2.3.2.E: a stuck row is flagged and alerted, **never silently
    retried**. Returning these to a claimable state would risk a second confirmation email
    or a second Notion write for a reply that may already have been half-processed, and the
    design prefers a loud, human-visible stall over a silent double effect.

    Ordered by `created_at` so the oldest stall is reported first when several arrive at once.
    """
    cutoff = now - stuck_after
    result = await session.execute(
        select(ProcessedInboundMessage)
        .where(
            ProcessedInboundMessage.status == "processing",
            ProcessedInboundMessage.created_at <= cutoff,
        )
        .order_by(ProcessedInboundMessage.created_at)
    )
    return list(result.scalars().all())


async def get_by_message_id(
    session: AsyncSession, provider_message_id: str
) -> ProcessedInboundMessage | None:
    """One row by its provider id, or `None`. Read-only; used by tests and the admin surface."""
    result = await session.execute(
        select(ProcessedInboundMessage).where(
            ProcessedInboundMessage.provider_message_id == provider_message_id
        )
    )
    return result.scalar_one_or_none()


__all__ = ["InboundStatus", "claim", "finish", "get_by_message_id", "list_stuck"]
