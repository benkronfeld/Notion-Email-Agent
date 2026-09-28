"""`audit_log` repository — append only (spec §2.3.5, CLAUDE.md).

There is deliberately no update or delete function here. The audit log is the record of
what the system did; a mutable audit row would make that record worthless.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.domain.types import AuditEvent


async def append(
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
    """Insert one audit row in the caller's transaction.

    `event_type` is the closed vocabulary from `app.domain.types.AuditEvent`; the column
    has no CHECK constraint (spec §2.3.5), so the type is the enforcement point.
    `payload` defaults to `{}` to match the column default when omitted.
    """
    await session.execute(
        insert(AuditLog).values(
            event_type=event_type,
            item_id=item_id,
            notion_page_id=notion_page_id,
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            payload=payload if payload is not None else {},
            result=result,
            error=error,
        )
    )
