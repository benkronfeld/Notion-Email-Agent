"""`system_state` repository — cursors, the data-source cache, and alert cooldowns.

All writes are `INSERT ... ON CONFLICT (key) DO UPDATE`, so a caller never has to know
whether a key already exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SystemState


async def get_json(session: AsyncSession, key: str, default: Any = None) -> Any:
    """The JSON value stored under `key`, or `default` if the key is absent."""
    result = await session.execute(select(SystemState.value).where(SystemState.key == key))
    value: Any = result.scalar_one_or_none()
    if value is None:
        return default
    return value


async def set_json(session: AsyncSession, key: str, value: Any) -> None:
    """Upsert a JSON value under `key`, refreshing `updated_at`."""
    stmt = pg_insert(SystemState).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SystemState.key],
        set_={"value": stmt.excluded.value, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def get_str(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """`get_json` for callers that expect a plain string (a cursor, a history id).

    A stored value that is not a string returns `default`, so a malformed row degrades to
    "no cursor" rather than to a confusing type error deeper in a job.
    """
    value = await get_json(session, key)
    if isinstance(value, str):
        return value
    return default


async def set_str(session: AsyncSession, key: str, value: str) -> None:
    """Upsert a plain string under `key` (stored as a bare JSON string)."""
    await set_json(session, key, value)


async def delete_key(session: AsyncSession, key: str) -> None:
    """Remove `key` if present. A no-op when it is already absent."""
    await session.execute(delete(SystemState).where(SystemState.key == key))
