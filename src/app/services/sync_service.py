"""Notion → local mirror sync (spec §2.3.2.A, build phase 1).

Two entry points, matching `jobs.py`: an hourly **incremental** sync and a daily **full
reconcile** at 04:00 local time. Both are orchestration only — every decision lives
elsewhere on purpose:

* `normalize_page` reduces a page to a `NormalizedItem` (pure, unit-tested).
* `items.upsert` writes it and owns `is_active` (`not in_trash`).
* `reminders.reconcile_reminders` decides the reminder schedule (spec §2.3.2.B). This
  service never computes a target time, never inspects a reminder row, and never decides
  whether a reminder should fire.
* `services.audit.record` writes the audit row and the log line.

The service reaches the world only through `AppContainer`: the `NotionClient` port (never
a live HTTP call in a test) and the injected `Clock` (never `datetime.now()`, CLAUDE.md
constraint 4).

## The two detection paths (FR-9)

Deletion is *not* one signal, it is two, and the primary one is the absence of a page:

* A real data-source query **never returns a trashed page**, so an item that was deleted
  simply stops appearing. Absence is therefore the main signal, and a reconcile that only
  looked at `in_trash` would never fire.
* `list_all_pages` is the one call that can return a page carrying `in_trash=true`
  (spec §2.3.2.A), so that flag is the secondary signal.

Both funnel into the same diff — `active_items_before - live_page_ids` — which is why one
code path covers them. The distinction is preserved in the `item_deactivated` payload
(`reason: "absent" | "in_trash"`) so the audit trail says which one was seen.

## Concurrency and failure

One transaction per source database. The cursor is written *inside* that transaction, so a
failure anywhere in the source's pass rolls the cursor back with the rest and the next run
re-queries the same window: a partially-failed sync can never skip pages. The failure is
then audited in its own small transaction (the rollback would otherwise take that record
with it) and re-raised, so the caller — `jobs.py` — decides whether to alert. This service
deliberately does not alert, and does not swallow.

Sources are processed independently: a malformed page in one database still lets the other
sync, and the first failure is re-raised once all sources have had their turn.

## Notion writes

None. This is the read half of the app; `NotionClient.update_page` is V1 and is never
called here (CLAUDE.md constraint 6 — the write surface is exactly three properties, and
it belongs to the V1 write path).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.container import AppContainer
from app.db.repositories import courses as courses_repo
from app.db.repositories import items as items_repo
from app.db.repositories import reminders as reminders_repo
from app.db.repositories import state as state_repo
from app.domain.planner import PlannerPolicy, iso_utc
from app.domain.types import AuditEvent, NormalizedItem, NotionPage, SourceDb
from app.integrations.notion.normalize import normalize_page, title_of, type_mismatch
from app.logging import get_logger
from app.services import audit as audit_service

log = get_logger(__name__)

# `system_state` key prefixes (§2.3.2.A). The cursor is per *source database*, not per data
# source: the id mapped to `notion_cursor:{source_db}` is the same small closed vocabulary
# the rest of the app uses, so a renamed data source id cannot orphan a cursor.
CURSOR_KEY_PREFIX = "notion_cursor:"
DATA_SOURCE_KEY_PREFIX = "notion_datasource:"

# The deliberate overlap (spec §2.3.2.A): the query asks for pages edited after
# `cursor - 5 min`, not after the cursor itself. Notion's `last_edited_time` filter is not
# guaranteed to be ordered against our clock, so a page edited during the previous run
# could otherwise fall exactly on the boundary and be missed forever. Re-reading five
# minutes is free — `upsert` is idempotent and so is `reconcile_reminders`.
SYNC_OVERLAP = timedelta(minutes=5)

# The two source databases, in the order they are synced. A `Literal` tuple, so the same
# closed vocabulary as `items.source_db` (spec §2.3.5).
SOURCE_DBS: tuple[SourceDb, ...] = ("assignments_readings", "exams_projects")


def _database_ids(settings: Settings) -> dict[SourceDb, str]:
    """`source_db -> database_id` from config. Both are *database* ids, not data sources."""
    return {
        "assignments_readings": settings.notion_db_assignments_readings,
        "exams_projects": settings.notion_db_exams_projects,
    }


def _parse_cursor(raw: str | None) -> datetime | None:
    """A stored cursor as an aware instant, or None when there is not a usable one.

    A missing *or malformed* cursor both degrade to "no cursor", which makes the next
    query unfiltered. That is the safe direction: an unreadable cursor costs a wider
    query, where the alternative (treating it as "now") would silently skip every page
    edited while the cursor was broken. The value is never read from the system clock.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("notion_cursor_unreadable", raw=raw)
        return None
    if parsed.tzinfo is None:
        log.warning("notion_cursor_naive", raw=raw)
        return None
    return parsed


@dataclass(slots=True)
class _ApplyCounts:
    """What a pass over one source's pages actually did. Feeds the audit payload."""

    pages: int = 0
    type_mismatches: int = 0


class SyncService:
    """Mirrors the two Notion databases into local Postgres (spec §2.3.2.A)."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container

    # ── Public entry points ─────────────────────────────────────────────────

    async def run_incremental(self) -> None:
        """Sync pages edited since the last successful run, once per source database.

        Raises the first failure after every source has been attempted. Raising rather
        than swallowing is what keeps a broken sync from looking like a quiet one the
        owner has nothing to act on.
        """
        now = self._container.clock.now()
        failures: list[Exception] = []
        for source_db, database_id in _database_ids(self._container.settings).items():
            try:
                await self._sync_source(source_db, database_id, now)
            except Exception as exc:  # one bad database must not block the other
                failures.append(exc)
        if failures:
            raise failures[0]

    async def run_full_reconcile(self) -> None:
        """Re-read every page in both databases and deactivate what is gone (FR-9).

        Run daily at 04:00 local (§2.3.2.A). It is the only path that can notice a page
        that was deleted or archived between two incremental syncs, because a trashed page
        simply stops appearing in query results.
        """
        now = self._container.clock.now()
        failures: list[Exception] = []
        for source_db, database_id in _database_ids(self._container.settings).items():
            try:
                await self._reconcile_source(source_db, database_id, now)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    # ── Startup / data-source resolution ────────────────────────────────────

    async def _data_source_id(self, session: AsyncSession, database_id: str) -> str:
        """Resolve a `database_id` to its `data_source_id`, caching it in `system_state`.

        API version 2025-09-03 split databases from data sources: queries go to the data
        source, so every sync needs this mapping. Cached under
        `notion_datasource:{database_id}` and reused, so the resolution is one call at
        startup and nothing per run (§2.3.2.A).

        The cache has no TTL on purpose. A second data source appearing on one of these
        databases is explicitly out of scope for V1 and flagged as a risk in the spec; a
        silent re-resolution would just as happily pick the *wrong* one of the two, so the
        stale mapping is the honest failure — it fails loudly rather than quietly.
        """
        key = f"{DATA_SOURCE_KEY_PREFIX}{database_id}"
        cached = await state_repo.get_str(session, key)
        if cached is not None:
            return cached
        resolved = await self._container.notion.resolve_data_source_id(database_id)
        await state_repo.set_str(session, key, resolved)
        log.info("notion_data_source_resolved", database_id=database_id, data_source_id=resolved)
        return resolved

    # ── Incremental ─────────────────────────────────────────────────────────

    async def _sync_source(self, source_db: SourceDb, database_id: str, now: datetime) -> None:
        """One source database's incremental pass, in a single transaction.

        Everything — the item upserts, the reminder reconciliation, the data-source cache,
        the cursor — commits together or not at all. That is what makes "only advance the
        cursor on success" true by construction rather than by remembering to check.
        """
        async with self._container.session_factory() as session:
            try:
                data_source_id = await self._data_source_id(session, database_id)
                cursor_key = f"{CURSOR_KEY_PREFIX}{source_db}"
                cursor = _parse_cursor(await state_repo.get_str(session, cursor_key))
                # FR-4's "never send late" and the missed-window rule are the planner's
                # business; this only decides *which pages to read*.
                since = None if cursor is None else cursor - SYNC_OVERLAP

                pages = await self._container.notion.list_changed_pages(data_source_id, since=since)
                counts = await self._apply_pages(session, pages, source_db, now)

                await state_repo.set_str(session, cursor_key, iso_utc(now))
                await audit_service.record(
                    session,
                    "notion_sync",
                    payload={
                        "source_db": source_db,
                        "database_id": database_id,
                        "data_source_id": data_source_id,
                        "pages": counts.pages,
                        "type_mismatches": counts.type_mismatches,
                        "since": iso_utc(since) if since is not None else None,
                        "cursor": iso_utc(now),
                    },
                    result="ok",
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                await self._audit_failure(session, "notion_sync", source_db, exc)
                log.error("notion_sync_failed", source_db=source_db, error=str(exc))
                raise

    async def _apply_pages(
        self, session: AsyncSession, pages: list[NotionPage], source_db: SourceDb, now: datetime
    ) -> _ApplyCounts:
        """Normalize → resolve course → upsert → reconcile, for each page. Order preserved."""
        counts = _ApplyCounts()
        for page in pages:
            if await self._upsert_page(session, page, source_db, now):
                counts.type_mismatches += 1
            counts.pages += 1
        return counts

    # ── Full reconcile ──────────────────────────────────────────────────────

    async def _reconcile_source(self, source_db: SourceDb, database_id: str, now: datetime) -> None:
        """One source database's full pass: re-read everything, then deactivate the rest."""
        async with self._container.session_factory() as session:
            try:
                data_source_id = await self._data_source_id(session, database_id)
                pages = await self._container.notion.list_all_pages(data_source_id)

                # Read the baseline *before* writing anything. `list_active_page_ids` is the
                # authority on "was this item active a moment ago", and an upsert can change
                # `is_active` (a trashed page carries in_trash=True), so reading it later
                # would erase the evidence the deactivation decision needs.
                active_before = await items_repo.list_active_page_ids(session, source_db)
                live_ids = {page.page_id for page in pages if not page.in_trash}
                trashed_ids = {page.page_id for page in pages if page.in_trash}

                counts = _ApplyCounts()
                for page in pages:
                    # A trashed page is not re-normalized or re-written: the only fact that
                    # matters about it is that it is gone, and that is handled by the diff
                    # below. Rewriting its fields from a deleted page would be noise.
                    if page.in_trash:
                        continue
                    if await self._upsert_page(session, page, source_db, now):
                        counts.type_mismatches += 1
                    counts.pages += 1

                deactivated = 0
                for page_id in active_before:
                    if page_id in live_ids:
                        continue
                    item = await items_repo.get_by_page_id(session, page_id)
                    if item is None:  # deleted locally between the two reads; nothing to do
                        continue
                    await items_repo.deactivate(session, item)
                    # `deactivate` writes through a Core UPDATE and sets the attribute on
                    # the object it was handed; refreshing anyway keeps the guarantee the
                    # planner depends on (the row it is given *is* the row in the database,
                    # see `_upsert_page`).
                    await session.refresh(item)
                    await audit_service.record(
                        session,
                        "item_deactivated",
                        item_id=item.id,
                        notion_page_id=page_id,
                        payload={
                            "source_db": source_db,
                            "reason": "in_trash" if page_id in trashed_ids else "absent",
                        },
                    )
                    # FR-9: pending reminders are skipped with `item_inactive`. The planner
                    # makes that decision from `is_active`; this pass only supplies the fact.
                    await reminders_repo.reconcile_reminders(session, item, now, self._policy)
                    deactivated += 1

                await audit_service.record(
                    session,
                    "notion_full_reconcile",
                    payload={
                        "source_db": source_db,
                        "database_id": database_id,
                        "data_source_id": data_source_id,
                        "pages": len(pages),
                        "live": len(live_ids),
                        "trashed": len(trashed_ids),
                        "deactivated": deactivated,
                        "type_mismatches": counts.type_mismatches,
                    },
                    result="ok",
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                await self._audit_failure(session, "notion_full_reconcile", source_db, exc)
                log.error("notion_full_reconcile_failed", source_db=source_db, error=str(exc))
                raise

    # ── Per-page work ───────────────────────────────────────────────────────

    async def _upsert_page(
        self, session: AsyncSession, page: NotionPage, source_db: SourceDb, now: datetime
    ) -> bool:
        """Normalize one page, resolve its course, upsert it, and reconcile. Returns mismatch.

        The `type_mismatch` flag is returned rather than only audited so the caller can put
        a count in the `notion_sync` payload without querying the audit table back.
        """
        item = self._normalize(page, source_db)
        course = await self._course_name(session, item, now)
        if course is not None:
            item = replace(item, course=course)

        stored = await items_repo.upsert(session, item)
        # Load-bearing, not defensive. `upsert` writes through a Core statement and then
        # re-selects the row; when the object is already in the session's identity map,
        # SQLAlchemy refreshes only *unloaded* attributes and the Core write does not expire
        # it. Upserting the same page twice in one session (which the full reconcile can do
        # across two sources, and a caller can always do) therefore hands back the *previous*
        # `due_at` — and `reconcile_reminders` would then plan the reminder schedule against
        # the wrong due date, which is a silently wrong reminder rather than a visible error.
        # `refresh` makes the object the row that is actually stored. See
        # Reproduced by `TestRepeatedUpsertInOneSession` in
        # `tests/integration/test_sync_service.py`.
        await session.refresh(stored)

        mismatched = type_mismatch(item)
        if mismatched:
            # The database wins (§1.2): `item_kind` already came from `source_db` and is
            # deliberately not "corrected" to match the `Type` select. The event records the
            # disagreement so a database that gets merged or remapped later is visible.
            await audit_service.record(
                session,
                "type_mismatch",
                item_id=stored.id,
                notion_page_id=item.notion_page_id,
                payload={
                    "source_db": item.source_db,
                    "item_kind": item.item_kind,
                    "notion_type": item.notion_type,
                },
            )

        await reminders_repo.reconcile_reminders(session, stored, now, self._policy)
        return mismatched

    def _normalize(self, page: NotionPage, source_db: SourceDb) -> NormalizedItem:
        """Reduce a page to a `NormalizedItem`, with the property names from config."""
        settings = self._container.settings
        return normalize_page(
            page,
            source_db=source_db,
            tz=settings.tz,
            default_due_time=settings.default_due_time,
            prop_course=settings.notion_prop_course,
            prop_type=settings.notion_prop_type,
            prop_due=settings.notion_prop_due,
            prop_status=settings.notion_prop_status,
            prop_done=settings.notion_prop_done,
        )

    async def _course_name(
        self, session: AsyncSession, item: NormalizedItem, now: datetime
    ) -> str | None:
        """The `Course` relation's page id resolved to a display name (§1.2, §2.3.2.A).

        `Course` is a Relation, so normalization can only produce a page id. The name comes
        from the local `courses` cache; on a miss — or once every
        `NOTION_COURSE_CACHE_TTL_HOURS`, so a renamed course is eventually picked up — the
        page is fetched once and cached.

        `None` is a normal answer, not an error: an unlinked relation, or a course page that
        has been deleted, both leave the item's `course` as None and the email simply omits
        it. The app never writes to `Course` (this is a read).
        """
        if item.course_page_id is None:
            return None

        cached = await courses_repo.get_cached(
            session,
            item.course_page_id,
            ttl_hours=self._container.settings.notion_course_cache_ttl_hours,
            now=now,
        )
        if cached is not None:
            return cached

        course_page = await self._container.notion.get_page(item.course_page_id)
        if course_page is None:
            log.warning("notion_course_page_missing", course_page_id=item.course_page_id)
            return None
        name = title_of(course_page)
        if not name:
            # An untitled course page would put an empty string in the email's course slot.
            # Leaving it None is the same as "no course linked", which the templates already
            # handle; caching the empty string would freeze the miss for a whole TTL.
            log.warning("notion_course_page_untitled", course_page_id=item.course_page_id)
            return None
        await courses_repo.upsert(session, item.course_page_id, name, now)
        return name

    # ── Failure recording ───────────────────────────────────────────────────

    async def _audit_failure(
        self,
        session: AsyncSession,
        event_type: AuditEvent,
        source_db: SourceDb,
        exc: Exception,
    ) -> None:
        """Record a failed sync in its own transaction, then let the caller re-raise.

        The main transaction has already been rolled back, so the failure record cannot
        ride along with the work it describes — it is committed separately, and only then
        does the exception propagate. A failure to write even this is logged and swallowed:
        losing the audit row must not replace the real error with a confusing secondary one.
        """
        try:
            await audit_service.record(
                session,
                event_type,
                payload={"source_db": source_db},
                result="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()
        except Exception:
            log.warning("notion_sync_failure_audit_failed", source_db=source_db, exc_info=True)

    @property
    def _policy(self) -> PlannerPolicy:
        """The one planner input this service supplies: what "completed" means here."""
        return PlannerPolicy(
            completed_status_value=self._container.settings.notion_status_completed
        )
