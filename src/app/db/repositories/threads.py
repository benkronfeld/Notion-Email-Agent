"""`email_threads` + `outbound_messages` repository — what the app sent, and to whom.

Two tables, one job: make every outbound message answerable to "which Notion item does a
reply in this conversation refer to?" (V1, §2.3.2.E step 3). The MVP writes both rows; the
V1 reply pipeline is the only reader that matters, and it matches on **stored provider ids**
— a Gmail `threadId`, or an `In-Reply-To`/`References` header against
`outbound_messages.rfc_message_id` — and never on a name (§2.3.2.E, FR-6).

That is why the two identifiers are captured unconditionally rather than looked up later:

* `email_threads.provider_thread_id` — the Gmail thread. One reminder starts one thread
  (`thread_id=None` on send), and the UNIQUE constraint means a second insert for the same
  Gmail thread is a loud `IntegrityError` rather than a duplicate mapping.
* `outbound_messages.rfc_message_id` — the RFC 5322 `Message-ID` the sender generated, the
  fallback key when a reply arrives with headers but no usable thread.

`id`, `state`, `clarification_rounds`, `sent_at`, and the timestamps are `server_default`
columns, so both functions use `INSERT ... RETURNING` and hand back a fully populated
entity: nothing here needs a second SELECT, and nothing reads the wall clock to invent a
value the database already has (CLAUDE.md constraint 4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailThread, OutboundMessage

# The `email_threads.state` CHECK values (§2.3.5). 'closed' is set once
# MAX_CLARIFICATION_ROUNDS is exhausted; a reply in a closed thread is still deduplicated
# but only ever produces a repeat notice, never another clarification attempt.
ThreadState = Literal["open", "awaiting_clarification", "closed"]

# The `outbound_messages.kind` CHECK values (§2.3.5). The MVP only ever writes `reminder`;
# the rest exist so V1 cannot quietly write a kind the constraint rejects, and so a typo
# fails at type-check time instead of at the database.
OutboundKind = Literal[
    "reminder",
    "clarification",
    "confirmation",
    "failure",
    "notice",
    "system_alert",
]


async def create_thread(
    session: AsyncSession,
    *,
    item_id: UUID,
    reminder_id: UUID | None,
    provider_thread_id: str,
    subject: str,
    root_rfc_message_id: str | None,
) -> EmailThread:
    """Record one outbound conversation, keyed by the Gmail thread id.

    Called after a message was actually accepted by Gmail — never before — so a row here
    always describes a conversation that exists. `reminder_id` is nullable because V1's
    confirmation and clarification threads are not tied to a reminder row; a reminder
    thread always sets it, which is what lets the reply pipeline load "that one item's
    context" without guessing (§2.3.2.E step 5).

    `state` defaults to `open` and `clarification_rounds` to 0 — the initial state of a
    thread nobody has replied to yet. The caller owns the transaction: this does not
    commit, so a failure later in the send bookkeeping rolls the thread back with it
    rather than leaving a thread with no message in it.
    """
    result = await session.execute(
        insert(EmailThread)
        .values(
            item_id=item_id,
            reminder_id=reminder_id,
            provider_thread_id=provider_thread_id,
            root_rfc_message_id=root_rfc_message_id,
            subject=subject,
        )
        .returning(EmailThread)
        .execution_options(populate_existing=True)
    )
    thread: EmailThread = result.scalars().one()
    return thread


async def add_outbound(
    session: AsyncSession,
    *,
    thread_id: UUID,
    kind: OutboundKind,
    provider_message_id: str,
    rfc_message_id: str | None,
) -> OutboundMessage:
    """Record one message the app sent, inside the thread it belongs to.

    `provider_message_id` is UNIQUE: the same Gmail message cannot be filed twice, which is
    the second line of defence (after the reminder's own exactly-once controls) against a
    retried send being recorded as two messages.

    `rfc_message_id` is nullable only because the contract inherits it from
    `MailClient.send`; every message composed by this app carries one, and the V1
    `References` fallback depends on it being there.
    """
    result = await session.execute(
        insert(OutboundMessage)
        .values(
            thread_id=thread_id,
            kind=kind,
            provider_message_id=provider_message_id,
            rfc_message_id=rfc_message_id,
        )
        .returning(OutboundMessage)
        .execution_options(populate_existing=True)
    )
    message: OutboundMessage = result.scalars().one()
    return message


# ── Readers and mutators for the reply pipeline (V1, §2.3.2.E step 3) ───────
#
# Step 3 resolves a reply to exactly one item by *stored provider ids* and never by name.
# These two readers are the whole of that resolution: a Gmail threadId first, and an
# `In-Reply-To`/`References` match against the RFC Message-ID we generated at send time as
# the fallback. Both are `UNIQUE` columns, so a hit is always exactly one row — the
# "exactly one item" guarantee is enforced by the schema, not by the query.


async def get_by_provider_thread_id(
    session: AsyncSession, provider_thread_id: str
) -> EmailThread | None:
    """The conversation for a Gmail thread, or `None` when we never started one.

    `None` is a real answer, not an error: it is what §2.3.2.E step 3 calls "no match",
    which fails safe (`inbound_unmapped`, one notice, no guessing).
    """
    result = await session.execute(
        select(EmailThread).where(EmailThread.provider_thread_id == provider_thread_id)
    )
    return result.scalar_one_or_none()


async def get_outbound_by_rfc_message_id(
    session: AsyncSession, rfc_message_id: str
) -> OutboundMessage | None:
    """The message we sent that a reply's `In-Reply-To`/`References` points at.

    This is the fallback when a reply arrives with headers but no usable thread. It works
    because we generate the RFC `Message-ID` ourselves and store it at send time; without
    that, a reply whose Gmail thread had been broken would be unmappable.
    """
    result = await session.execute(
        select(OutboundMessage).where(OutboundMessage.rfc_message_id == rfc_message_id)
    )
    return result.scalar_one_or_none()


async def get_thread_by_id(session: AsyncSession, thread_id: UUID) -> EmailThread | None:
    """One thread by its primary key."""
    result = await session.execute(select(EmailThread).where(EmailThread.id == thread_id))
    return result.scalar_one_or_none()


async def set_state(
    session: AsyncSession, thread_id: UUID, *, state: ThreadState, now: datetime
) -> None:
    """Set a thread's state (`open` / `awaiting_clarification` / `closed`)."""
    await session.execute(
        update(EmailThread).where(EmailThread.id == thread_id).values(state=state, updated_at=now)
    )


async def set_pending_clarification(
    session: AsyncSession, thread_id: UUID, *, pending: dict[str, Any] | None, now: datetime
) -> None:
    """Store (or clear) the question this thread is waiting on.

    Shape is `{question, original_request}` (§2.3.5). It is cleared when the owner's reply
    is finally understood, so a later reply in the same thread is not read against a
    question that has already been answered.
    """
    await session.execute(
        update(EmailThread)
        .where(EmailThread.id == thread_id)
        .values(pending_clarification=pending, updated_at=now)
    )


async def bump_clarification_rounds(
    session: AsyncSession, thread_id: UUID, *, now: datetime
) -> int:
    """Increment and return the thread's clarification round count.

    Counted in the database rather than in memory so an overlapping deploy cannot reset it,
    and returned so the caller can compare it against `MAX_CLARIFICATION_ROUNDS` and close
    the thread on the round that exhausts the budget (§2.3.2.E).
    """
    result = await session.execute(
        update(EmailThread)
        .where(EmailThread.id == thread_id)
        .values(
            clarification_rounds=EmailThread.clarification_rounds + 1,
            updated_at=now,
        )
        .returning(EmailThread.clarification_rounds)
    )
    return int(result.scalar_one())
