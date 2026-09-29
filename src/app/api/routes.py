"""The ops HTTP surface (spec §2.3.6). No public endpoints, no inbound webhooks.

`/healthz` is the only unauthenticated route. Everything else — readiness included —
requires the admin bearer token, enforced by ``require_admin_token`` on the secured
sub-router so a new route is protected by construction rather than by remembering.

`POST /admin/gmail/poll` is **deliberately absent**: inbound polling is V1 (build phase 4).
It must 404, and ``tests/integration/test_admin_api.py`` asserts that rather than assuming
it, so a future half-built poll endpoint cannot quietly appear.

Admin triggers do their work *in the request* (via ``run_job_once``), so a caller that
POSTs then reads observes the effect immediately.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ContainerDep, SessionDep, require_admin_token
from app.container import AppContainer
from app.db.models import AuditLog, Item, Reminder
from app.db.repositories import audit as audit_repo
from app.db.repositories import state as state_repo
from app.jobs import (
    JOB_FULL_RECONCILE,
    JOB_GMAIL_POLLER,
    JOB_NOTION_SYNC,
    JOB_REMINDER_SCHEDULER,
    last_success_key,
    run_job_once,
)

router = APIRouter()
secured = APIRouter(dependencies=[Depends(require_admin_token)])


# ── Response models (typed, never bare dicts) ───────────────────────────────


class HealthResponse(BaseModel):
    """Liveness: the process is up. Says nothing about dependencies."""

    status: str


class ReadinessResponse(BaseModel):
    """Readiness: the database answers, the scheduler is running, and how stale data is."""

    database_reachable: bool
    scheduler_running: bool
    last_sync_age_seconds: float | None
    last_tick_age_seconds: float | None


class StatusResponse(BaseModel):
    """Operational snapshot for the owner."""

    last_successful_sync_at: datetime | None
    last_scheduler_tick_at: datetime | None
    scheduler_running: bool
    pending_reminders: int
    failed_reminders: int


class ItemResponse(BaseModel):
    """A local item with its computed due time, in UTC and in the configured zone."""

    id: UUID
    notion_page_id: str
    name: str
    course: str | None
    source_db: str
    item_kind: str
    status: str
    done: bool
    due_date: date | None
    due_at: datetime | None
    due_at_local: datetime | None
    due_has_time: bool
    is_active: bool


class ReminderResponse(BaseModel):
    """A reminder row and the target it fires at."""

    id: UUID
    item_id: UUID
    reminder_type: str
    status: str
    target_at: datetime
    due_at_snapshot: datetime
    skip_reason: str | None
    attempt_count: int
    sent_at: datetime | None
    last_error: str | None


class AuditResponse(BaseModel):
    """One append-only audit row."""

    id: int
    event_type: str
    item_id: UUID | None
    notion_page_id: str | None
    result: str | None
    error: str | None
    payload: dict[str, Any]
    created_at: datetime


class SyncRequest(BaseModel):
    """`{"full": false}` runs the incremental sync; `true` runs the full reconcile."""

    full: bool = False


class JobRunResponse(BaseModel):
    """The outcome of an on-demand job run. `ran` is False when the lock was held."""

    job: str
    ran: bool


class ReminderCancelResponse(BaseModel):
    """The reminder after a manual cancel."""

    id: UUID
    status: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _newest(*values: datetime | None) -> datetime | None:
    """The latest of the given instants, ignoring None."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _age_seconds(now: datetime, then: datetime | None) -> float | None:
    """Seconds since `then`, clamped at zero. None when `then` is None."""
    if then is None:
        return None
    return max(0.0, (now - then).total_seconds())


async def _last_success(session: AsyncSession, job_name: str) -> datetime | None:
    """Read a job's last-success instant from `system_state`.

    A malformed stored value degrades to None rather than failing the status endpoint.
    """
    raw = await state_repo.get_str(session, last_success_key(job_name))
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _item_response(item: Item, container: AppContainer) -> ItemResponse:
    return ItemResponse(
        id=item.id,
        notion_page_id=item.notion_page_id,
        name=item.name,
        course=item.course,
        source_db=item.source_db,
        item_kind=item.item_kind,
        status=item.status,
        done=item.done,
        due_date=item.due_date,
        due_at=item.due_at,
        due_at_local=item.due_at.astimezone(container.settings.tz) if item.due_at else None,
        due_has_time=item.due_has_time,
        is_active=item.is_active,
    )


def _reminder_response(reminder: Reminder) -> ReminderResponse:
    return ReminderResponse(
        id=reminder.id,
        item_id=reminder.item_id,
        reminder_type=reminder.reminder_type,
        status=reminder.status,
        target_at=reminder.target_at,
        due_at_snapshot=reminder.due_at_snapshot,
        skip_reason=reminder.skip_reason,
        attempt_count=reminder.attempt_count,
        sent_at=reminder.sent_at,
        last_error=reminder.last_error,
    )


# ── Liveness (no auth) ──────────────────────────────────────────────────────


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness only. Deliberately does not touch the database."""
    return HealthResponse(status="ok")


# ── Readiness and status ────────────────────────────────────────────────────


@secured.get("/readyz", response_model=ReadinessResponse)
async def readyz(
    request: Request, container: ContainerDep, session: SessionDep
) -> ReadinessResponse:
    """Readiness: DB reachable, scheduler running, and the ages of the last sync and tick.

    Never raises when the database is down — a readiness probe is needed *most* when a
    dependency has failed. The in-process timestamps on `app.state` are still reported; the
    persisted ones are read only when the probe succeeded, since a second statement on a
    connection that just failed would raise.
    """
    try:
        await session.execute(text("SELECT 1"))
        database_reachable = True
    except Exception:
        database_reachable = False

    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_running = bool(scheduler is not None and scheduler.running)

    now = container.clock.now()
    last_sync = getattr(request.app.state, "last_sync_at", None)
    last_tick = getattr(request.app.state, "last_tick_at", None)
    if database_reachable:
        try:
            last_sync = _newest(
                last_sync,
                await _last_success(session, JOB_NOTION_SYNC),
                await _last_success(session, JOB_FULL_RECONCILE),
            )
            last_tick = _newest(last_tick, await _last_success(session, JOB_REMINDER_SCHEDULER))
        except Exception:
            database_reachable = False

    return ReadinessResponse(
        database_reachable=database_reachable,
        scheduler_running=scheduler_running,
        last_sync_age_seconds=_age_seconds(now, last_sync),
        last_tick_age_seconds=_age_seconds(now, last_tick),
    )


@secured.get("/admin/status", response_model=StatusResponse)
async def admin_status(
    request: Request, container: ContainerDep, session: SessionDep
) -> StatusResponse:
    """Last successful sync/tick, plus counts of pending and failed reminders.

    A database failure is reported as 503 rather than allowed to surface as a 500 traceback:
    the whole point of this endpoint is to say what state the system is in.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    try:
        last_sync = _newest(
            await _last_success(session, JOB_NOTION_SYNC),
            await _last_success(session, JOB_FULL_RECONCILE),
            getattr(request.app.state, "last_sync_at", None),
        )
        last_tick = _newest(
            await _last_success(session, JOB_REMINDER_SCHEDULER),
            getattr(request.app.state, "last_tick_at", None),
        )
        pending = await session.scalar(
            select(func.count()).select_from(Reminder).where(Reminder.status == "pending")
        )
        failed = await session.scalar(
            select(func.count()).select_from(Reminder).where(Reminder.status == "failed")
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc
    return StatusResponse(
        last_successful_sync_at=last_sync,
        last_scheduler_tick_at=last_tick,
        scheduler_running=bool(scheduler is not None and scheduler.running),
        pending_reminders=pending or 0,
        failed_reminders=failed or 0,
    )


# ── Reads ───────────────────────────────────────────────────────────────────


@secured.get("/admin/items", response_model=list[ItemResponse])
async def admin_items(
    container: ContainerDep,
    session: SessionDep,
    active: Annotated[bool, Query()] = True,
    upcoming: Annotated[bool, Query()] = True,
) -> list[ItemResponse]:
    """Local items with computed due times. `upcoming` keeps only due dates in the future."""
    stmt = select(Item).where(Item.is_active.is_(active))
    if upcoming:
        stmt = stmt.where(Item.due_at.is_not(None), Item.due_at >= container.clock.now())
    stmt = stmt.order_by(Item.due_at.asc().nulls_last(), Item.name.asc())
    result = await session.execute(stmt)
    return [_item_response(item, container) for item in result.scalars().all()]


@secured.get("/admin/reminders", response_model=list[ReminderResponse])
async def admin_reminders(
    session: SessionDep,
    reminder_status: Annotated[str | None, Query(alias="status")] = None,
    item_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[ReminderResponse]:
    """Reminder rows and their targets, newest target first."""
    stmt = select(Reminder)
    if reminder_status is not None:
        stmt = stmt.where(Reminder.status == reminder_status)
    if item_id is not None:
        stmt = stmt.where(Reminder.item_id == item_id)
    stmt = stmt.order_by(Reminder.target_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_reminder_response(reminder) for reminder in result.scalars().all()]


@secured.get("/admin/audit", response_model=list[AuditResponse])
async def admin_audit(
    session: SessionDep,
    event_type: str | None = None,
    item_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[AuditResponse]:
    """The audit trail, newest first. Filterable by event type and item."""
    stmt = select(AuditLog)
    if event_type is not None:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if item_id is not None:
        stmt = stmt.where(AuditLog.item_id == item_id)
    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return [
        AuditResponse(
            id=row.id,
            event_type=row.event_type,
            item_id=row.item_id,
            notion_page_id=row.notion_page_id,
            result=row.result,
            error=row.error,
            payload=row.payload,
            created_at=row.created_at,
        )
        for row in result.scalars().all()
    ]


# ── Triggers ────────────────────────────────────────────────────────────────


@secured.post("/admin/sync", response_model=JobRunResponse)
async def trigger_sync(
    payload: SyncRequest, request: Request, container: ContainerDep
) -> JobRunResponse:
    """Run a Notion sync now, in the request. `{"full": true}` runs the full reconcile."""
    job = JOB_FULL_RECONCILE if payload.full else JOB_NOTION_SYNC
    ran = await run_job_once(container, job)
    if ran:
        request.app.state.last_sync_at = container.clock.now()
    return JobRunResponse(job=job, ran=ran)


@secured.post("/admin/scheduler/run", response_model=JobRunResponse)
async def trigger_scheduler_run(request: Request, container: ContainerDep) -> JobRunResponse:
    """Run one reminder scheduler pass now, in the request."""
    ran = await run_job_once(container, JOB_REMINDER_SCHEDULER)
    if ran:
        request.app.state.last_tick_at = container.clock.now()
    return JobRunResponse(job=JOB_REMINDER_SCHEDULER, ran=ran)


@secured.post("/admin/gmail/poll", response_model=JobRunResponse)
async def trigger_gmail_poll(container: ContainerDep) -> JobRunResponse:
    """Run one inbound Gmail poll now, in the request (§2.3.6).

    It runs the same job body the scheduler runs — poll, then flag stuck rows — so an
    operator-triggered poll cannot diverge from the scheduled one. The reply pipeline can
    write to Notion, so this is not read-only: it is the one trigger that can change a
    deadline, and it is behind the admin token for that reason.
    """
    ran = await run_job_once(container, JOB_GMAIL_POLLER)
    return JobRunResponse(job=JOB_GMAIL_POLLER, ran=ran)


# ── Manual reminder kill ────────────────────────────────────────────────────


@secured.post("/admin/reminders/{reminder_id}/cancel", response_model=ReminderCancelResponse)
async def cancel_reminder(reminder_id: UUID, session: SessionDep) -> ReminderCancelResponse:
    """Mark a *pending* reminder `skipped` — the manual kill switch.

    Guarded in the WHERE clause so it can never overwrite a `sent` or `claimed` row: only
    a pending reminder may be cancelled, and the transition happens at most once. No
    `skip_reason` is invented — the vocabulary in `app.domain.types.SkipReason` is closed
    and has no "manual cancel" value.
    """
    result = await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id, Reminder.status == "pending")
        .values(status="skipped", updated_at=func.now())
        .returning(Reminder.item_id)
    )
    row = result.first()
    if row is None:
        existing = await session.get(Reminder, reminder_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="reminder not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"reminder is {existing.status!r}, not pending",
        )

    await audit_repo.append(
        session,
        "reminder_skipped",
        item_id=row.item_id,
        payload={"source": "admin_api", "manual_cancel": True},
        result="skipped",
    )
    await session.commit()
    return ReminderCancelResponse(id=reminder_id, status="skipped")


# Both routers are exported as one so `create_app` includes a single router.
router.include_router(secured)
