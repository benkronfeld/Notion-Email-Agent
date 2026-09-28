"""`items` repository — the local mirror of the two Notion databases."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item
from app.domain.types import NormalizedItem, SourceDb


async def upsert(session: AsyncSession, item: NormalizedItem) -> Item:
    """Insert or update the item keyed on `notion_page_id`, then return the stored row.

    `is_active` is set from the page itself — `not item.in_trash` — deliberately rather
    than being left alone on conflict. A full reconcile feeds every page (trashed pages
    carry `in_trash=True`, so they deactivate), while an incremental sync only returns
    live pages, so a page reappearing in a query is by definition not trashed and is
    reactivated. This is the symmetric skip→pending resurrection path (spec §2.3.2.B).
    """
    stmt = pg_insert(Item).values(
        notion_page_id=item.notion_page_id,
        notion_data_source_id=item.notion_data_source_id,
        source_db=item.source_db,
        item_kind=item.item_kind,
        notion_type=item.notion_type,
        name=item.name,
        course_page_id=item.course_page_id,
        course=item.course,
        status=item.status,
        done=item.done,
        due_date=item.due_date,
        due_at=item.due_at,
        due_has_time=item.due_has_time,
        timezone=item.timezone,
        notion_url=item.notion_url,
        is_active=not item.in_trash,
        notion_last_edited_at=item.notion_last_edited_time,
        last_notion_sync_at=func.now(),
    )
    update_columns: dict[str, Any] = {
        "notion_data_source_id": stmt.excluded.notion_data_source_id,
        "source_db": stmt.excluded.source_db,
        "item_kind": stmt.excluded.item_kind,
        "notion_type": stmt.excluded.notion_type,
        "name": stmt.excluded.name,
        "course_page_id": stmt.excluded.course_page_id,
        "course": stmt.excluded.course,
        "status": stmt.excluded.status,
        "done": stmt.excluded.done,
        "due_date": stmt.excluded.due_date,
        "due_at": stmt.excluded.due_at,
        "due_has_time": stmt.excluded.due_has_time,
        "timezone": stmt.excluded.timezone,
        "notion_url": stmt.excluded.notion_url,
        # See the docstring: a page seen in a query is not trashed.
        "is_active": stmt.excluded.is_active,
        "notion_last_edited_at": stmt.excluded.notion_last_edited_at,
        "last_notion_sync_at": func.now(),
        "updated_at": func.now(),
    }
    stmt = stmt.on_conflict_do_update(index_elements=[Item.notion_page_id], set_=update_columns)
    await session.execute(stmt)
    await session.flush()

    stored = await get_by_page_id(session, item.notion_page_id)
    if stored is None:
        raise RuntimeError(
            f"items.upsert lost its row for {item.notion_page_id!r} immediately after insert"
        )
    return stored


async def get_by_page_id(session: AsyncSession, notion_page_id: str) -> Item | None:
    """The item with this Notion page id, or None."""
    result = await session.execute(select(Item).where(Item.notion_page_id == notion_page_id))
    found: Item | None = result.scalar_one_or_none()
    return found


async def get_by_id(session: AsyncSession, item_id: UUID) -> Item | None:
    """The item with this local id, or None."""
    result = await session.execute(select(Item).where(Item.id == item_id))
    found: Item | None = result.scalar_one_or_none()
    return found


async def list_active(session: AsyncSession) -> list[Item]:
    """Every active (non-archived) item, soonest due first, undated last."""
    result = await session.execute(
        select(Item)
        .where(Item.is_active.is_(True))
        .order_by(Item.due_at.asc().nulls_last(), Item.name.asc())
    )
    items: list[Item] = list(result.scalars().all())
    return items


async def list_active_page_ids(session: AsyncSession, source_db: SourceDb) -> list[str]:
    """Active notion_page_ids in one database — the full-reconcile diff baseline (FR-9)."""
    result = await session.execute(
        select(Item.notion_page_id).where(Item.is_active.is_(True), Item.source_db == source_db)
    )
    page_ids: list[str] = list(result.scalars().all())
    return page_ids


async def deactivate(session: AsyncSession, item: Item) -> bool:
    """Mark an item inactive. Returns True only if this call changed its state.

    The guard is in the WHERE clause, so two concurrent reconciles cannot both report a
    transition (and the audit event is written once).
    """
    result = await session.execute(
        update(Item)
        .where(Item.id == item.id, Item.is_active.is_(True))
        .values(is_active=False, updated_at=func.now())
        .returning(Item.id)
    )
    # RETURNING yields a row only when the UPDATE actually matched, so this is the
    # "changed" signal without depending on an untyped cursor rowcount.
    changed = result.first() is not None
    if changed:
        item.is_active = False
    return changed
