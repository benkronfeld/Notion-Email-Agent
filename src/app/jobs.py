"""APScheduler wiring and the cross-instance advisory lock (spec §2.3.2.C, §2.3.2.F).

Three jobs run in-process from the FastAPI lifespan: an hourly Notion sync, a daily full
reconcile at ``settings.full_reconcile_at`` local time, and the reminder scheduler tick.
There is deliberately **no Gmail poll job** — inbound email is V1 (build phase 4).

Every job body runs inside a Postgres advisory lock so that two app instances during a
deploy can never both process reminders (spec §2.3.2.C). The lock is *session*-scoped, so
it is taken on a dedicated connection pulled straight from the engine and held for the
whole job — never on a pooled session, which could be returned (and the lock silently
dropped, or held forever) partway through.

A job body that raises must never escape: APScheduler permanently disables a job whose
callable raises, which is exactly how reminders stop silently. ``run_job_once`` therefore
catches ``Exception``, logs it, and raises a rate-limited owner alert.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from uuid import uuid4

# APScheduler 3.x ships no `py.typed` marker and has no stubs; the ignores are permanent
# until 4.x. Adding an `ignore_missing_imports` override for it in `pyproject.toml` would
# be the alternative, but that file is frozen for this workstream.
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import (  # type: ignore[import-untyped]
    IntervalTrigger,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.container import AppContainer
from app.db.repositories import state as state_repo
from app.logging import get_logger, job_context

# ── Job names (also the `last_success:{job}` system_state key suffix) ────────

JOB_NOTION_SYNC = "notion_sync"
JOB_FULL_RECONCILE = "full_reconcile"
JOB_REMINDER_SCHEDULER = "reminder_scheduler"

# Fixed 64-bit advisory lock key. A literal, never `hashtext(...)` at runtime: the key must
# be identical in every process and every deploy, and a hash of a string is neither
# guaranteed stable nor obviously unique. This spells "NRMD" (Notion ReMinDer) in ASCII.
ADVISORY_LOCK_KEY = 0x4E524D44

# Per-job misfire grace. A run that fires late by less than this still runs; beyond it
# APScheduler skips it as stale rather than firing a pile of backlogged jobs.
_FULL_RECONCILE_MISFIRE_GRACE_SEC = 3600


def last_success_key(job_name: str) -> str:
    """`system_state` key holding the ISO-8601 instant of a job's last success."""
    return f"last_success:{job_name}"


def _engine(container: AppContainer) -> AsyncEngine:
    """The engine behind the container's session factory.

    The advisory lock needs a connection of its own, so it is taken from the engine rather
    than from a pooled session.
    """
    bind = container.session_factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError(
            "container.session_factory is not bound to an AsyncEngine; "
            "cannot take the advisory lock"
        )
    return bind


# ── Job bodies (imported lazily: services may not exist yet) ────────────────


async def _run_notion_sync(container: AppContainer) -> None:
    """Incremental Notion sync (build phase 1/2)."""
    from app.services.sync_service import SyncService

    await SyncService(container).run_incremental()


async def _run_full_reconcile(container: AppContainer) -> None:
    """Daily full reconcile — detects archived/deleted pages (FR-9)."""
    from app.services.sync_service import SyncService

    await SyncService(container).run_full_reconcile()


async def _run_reminder_tick(container: AppContainer) -> None:
    """One reminder scheduler pass.

    Stale claims are recovered first: a reminder left `claimed` by a crash between claim
    and send is resolved (sent or returned to pending) before the tick claims new work, so
    a crashed send cannot starve. Both are imported lazily for the same reason as above.
    """
    from app.services.reminder_service import ReminderService

    service = ReminderService(container)
    await service.recover_stale_claims()
    await service.tick()


_JOB_BODIES: dict[str, Callable[[AppContainer], Awaitable[None]]] = {
    JOB_NOTION_SYNC: _run_notion_sync,
    JOB_FULL_RECONCILE: _run_full_reconcile,
    JOB_REMINDER_SCHEDULER: _run_reminder_tick,
}


# A crashed job must be reported as the failure it actually is. The alert vocabulary in
# §2.3.4 is closed and `AlertService` rejects anything outside it, so an invented
# `job_failed:<name>` would raise, be swallowed by the handler below, and the owner would
# never hear that syncing or sending had stopped — exactly the silent-failure mode FR-13
# exists to prevent. A sync crash is a sync failure; a scheduler tick crash is a send
# failure.
_JOB_ALERT_TYPES: dict[str, str] = {
    JOB_NOTION_SYNC: "notion_sync_failed",
    JOB_FULL_RECONCILE: "notion_sync_failed",
    JOB_REMINDER_SCHEDULER: "reminder_send_failed",
}


# ── Success bookkeeping and alerting ────────────────────────────────────────


async def _record_success(container: AppContainer, job_name: str) -> None:
    """Stamp the instant a job succeeded, in `system_state`.

    The timestamp is the injected clock's, never the wall clock (CLAUDE.md constraint 4),
    and it lives in the database rather than in process memory so `/readyz` reports the
    same answer on every instance and across restarts. A failure here must not fail the
    job — the job's own work already committed — so it is logged and swallowed.
    """
    log = get_logger(__name__)
    try:
        async with container.session_factory() as session:
            await state_repo.set_str(
                session, last_success_key(job_name), container.clock.now().isoformat()
            )
            await session.commit()
    except Exception:
        log.warning("job_success_stamp_failed", job=job_name, exc_info=True)


async def _alert_job_failure(container: AppContainer, job_name: str, exc: BaseException) -> None:
    """Best-effort, rate-limited owner alert. Never lets its own failure escape."""
    log = get_logger(__name__)
    try:
        from app.services.alert_service import AlertService
    except Exception:  # the alert service has not landed yet — nothing to alert with
        log.warning("job_alert_unavailable", job=job_name)
        return
    alert_type = _JOB_ALERT_TYPES.get(job_name)
    if alert_type is None:
        # A job this mapping does not cover. Log loudly rather than mislabel the alert
        # under an unrelated failure type.
        log.warning("job_alert_unmapped", job=job_name)
        return
    try:
        await AlertService(container).alert(
            alert_type,
            f"Scheduled job {job_name} failed",
            f"The `{job_name}` job raised {type(exc).__name__}: {exc}",
        )
    except Exception:
        log.warning("job_alert_failed", job=job_name, exc_info=True)


# ── The locked runner ───────────────────────────────────────────────────────


async def _run_locked(
    container: AppContainer,
    job_name: str,
    body: Callable[[], Awaitable[None]],
) -> bool:
    """Run ``body`` under the advisory lock. Returns False if another instance holds it.

    The lock connection is dedicated and held for the whole body. Autocommit keeps it out
    of an idle-in-transaction state for the duration of a long job, which would otherwise
    pin a snapshot and block vacuuming.
    """
    log = get_logger(__name__)
    engine = _engine(container)
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        )
        if not acquired:
            log.info("job_skipped_lock_held", job=job_name)
            return False
        try:
            await body()
        finally:
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
                )
            except Exception:  # never mask the body's own exception
                log.warning("job_advisory_unlock_failed", job=job_name, exc_info=True)
        return True


async def run_job_once(container: AppContainer, job_name: str) -> bool:
    """Run one named job now, under the advisory lock. Used by the admin API and by tests.

    Returns True when the body ran, False when the lock was held elsewhere or the body
    raised. An exception is never allowed to escape: it is logged and raised as a
    rate-limited alert instead, because APScheduler disables a job whose callable raises.
    """
    log = get_logger(__name__)
    body = _JOB_BODIES.get(job_name)
    if body is None:
        known = ", ".join(sorted(_JOB_BODIES))
        raise ValueError(f"unknown job {job_name!r}; known jobs: {known}")

    with job_context(job_name, uuid4().hex):
        try:
            ran = await _run_locked(container, job_name, functools.partial(body, container))
        except Exception as exc:
            log.exception("job_failed", job=job_name)
            await _alert_job_failure(container, job_name, exc)
            return False
        if ran:
            await _record_success(container, job_name)
        return ran


# ── Scheduler lifecycle ─────────────────────────────────────────────────────


def _job_entry(container: AppContainer, job_name: str) -> Callable[[], Awaitable[None]]:
    """A zero-argument coroutine APScheduler can schedule for ``job_name``.

    A plain closure rather than ``functools.partial``: APScheduler decides whether a job is
    a coroutine function with ``asyncio.iscoroutinefunction``, which is unambiguous for a
    real ``async def``.
    """

    async def _entry() -> None:
        await run_job_once(container, job_name)

    return _entry


def start_scheduler(container: AppContainer) -> AsyncIOScheduler:
    """Build and start the scheduler. Called from the FastAPI lifespan only.

    The scheduler's timezone is ``settings.tz``; the daily trigger is derived from
    ``settings.full_reconcile_at`` in that zone. No zone is hardcoded (CLAUDE.md
    constraint 3).
    """
    settings = container.settings
    tz = settings.tz
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        _job_entry(container, JOB_NOTION_SYNC),
        trigger=IntervalTrigger(minutes=settings.notion_sync_interval_min, timezone=tz),
        id=JOB_NOTION_SYNC,
        name="Incremental Notion sync",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.notion_sync_interval_min * 60,
        replace_existing=True,
    )

    reconcile_at = settings.full_reconcile_at
    scheduler.add_job(
        _job_entry(container, JOB_FULL_RECONCILE),
        trigger=CronTrigger(hour=reconcile_at.hour, minute=reconcile_at.minute, timezone=tz),
        id=JOB_FULL_RECONCILE,
        name="Daily full reconcile",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_FULL_RECONCILE_MISFIRE_GRACE_SEC,
        replace_existing=True,
    )

    scheduler.add_job(
        _job_entry(container, JOB_REMINDER_SCHEDULER),
        trigger=IntervalTrigger(seconds=settings.reminder_scheduler_interval_sec, timezone=tz),
        id=JOB_REMINDER_SCHEDULER,
        name="Reminder scheduler",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.reminder_scheduler_interval_sec,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler


def shutdown_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Stop the scheduler. Sync, because APScheduler's `shutdown` is sync.

    `wait=False` so shutdown does not block the event loop the scheduler itself runs on.
    """
    scheduler.shutdown(wait=False)


# Kept for callers that want to inspect the registered jobs without starting anything.
def configured_job_names() -> tuple[str, ...]:
    """The job names this module knows how to run."""
    return tuple(sorted(_JOB_BODIES))


__all__: list[str] = [
    "ADVISORY_LOCK_KEY",
    "JOB_FULL_RECONCILE",
    "JOB_NOTION_SYNC",
    "JOB_REMINDER_SCHEDULER",
    "configured_job_names",
    "last_success_key",
    "run_job_once",
    "shutdown_scheduler",
    "start_scheduler",
]
