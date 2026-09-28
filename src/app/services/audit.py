"""Audit logging: one call that writes the row *and* mirrors it to structlog.

A thin convenience wrapper over `app.db.repositories.audit.append`, and nothing more. The
repository stays the only thing that writes `audit_log`, this module adds no schema and no
event type (`audit_log.event_type` is a closed vocabulary, spec §2.3.5 — new events are a
spec change, not something to invent here). Its single job is that a service never has to
remember two calls: the durable row and the operator-visible log line carry the same facts.

**Ordering, and what it costs.** The row is inserted first, then logged. The insert lives
in the caller's transaction, so a later rollback can leave a log line describing an event
that did not persist. That direction is deliberate: structlog is a convenience for
whoever is watching the process, while `audit_log` is the record of truth. Erring the
other way — logging first — would risk the failure mode that matters, an event that
happened with nothing in the log about it.

The mirror is a single `audit_event` line with the same identifying fields. It is not a
second audit system: nothing reads it back, and no decision is ever made from it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import audit as audit_repo
from app.domain.types import AuditEvent
from app.logging import get_logger

log = get_logger(__name__)


async def record(
    session: AsyncSession,
    event_type: AuditEvent,
    *,
    item_id: UUID | None = None,
    notion_page_id: str | None = None,
    provider_message_id: str | None = None,
    provider_thread_id: str | None = None,
    payload: dict[str, Any] | None = None,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """Append one audit row in the caller's transaction, then mirror it to the log.

    The signature is `audit_repo.append`'s, deliberately and exactly: this is a
    pass-through, so a caller that outgrows the convenience can drop to the repository
    without rewriting anything. `payload` defaults to `{}` and `error` is expected to be a
    short, non-secret description — an exception type and message, never a response body
    that might carry a token (CLAUDE.md constraint 2).
    """
    await audit_repo.append(
        session,
        event_type,
        item_id=item_id,
        notion_page_id=notion_page_id,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        payload=payload,
        result=result,
        error=error,
    )
    log.info(
        "audit_event",
        event_type=event_type,
        item_id=str(item_id) if item_id is not None else None,
        notion_page_id=notion_page_id,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        payload=payload if payload is not None else {},
        result=result,
        error=error,
    )
