"""`courses` repository — the title cache behind the `Course` relation (spec §1.2)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course


async def get_cached(
    session: AsyncSession, notion_page_id: str, ttl_hours: int, now: datetime
) -> str | None:
    """The cached course name, or None when missing **or** older than `ttl_hours`.

    `now` must be timezone-aware (CLAUDE.md constraint 4); it is passed in rather than
    read from the system clock so the TTL is testable.
    """
    result = await session.execute(select(Course).where(Course.notion_page_id == notion_page_id))
    course: Course | None = result.scalar_one_or_none()
    if course is None:
        return None
    if now - course.last_synced_at > timedelta(hours=ttl_hours):
        return None
    return course.name


async def upsert(session: AsyncSession, notion_page_id: str, name: str, now: datetime) -> None:
    """Insert or refresh the cached title, stamping `last_synced_at` with `now`."""
    stmt = pg_insert(Course).values(notion_page_id=notion_page_id, name=name, last_synced_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Course.notion_page_id],
        set_={"name": stmt.excluded.name, "last_synced_at": stmt.excluded.last_synced_at},
    )
    await session.execute(stmt)
