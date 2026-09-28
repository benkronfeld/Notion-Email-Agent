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

from typing import Literal
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailThread, OutboundMessage

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
