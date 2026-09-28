"""FastAPI application entrypoint (spec §2.3.1, §2.3.6).

``create_app`` builds the app around an injected ``AppContainer`` so that tests boot the
real application over fake adapters and never touch a live service (CLAUDE.md constraint
5). The scheduler starts and stops with the app's lifespan, and is stored on ``app.state``
so ``/readyz`` can report whether it is running.

No engine is created at import time: the container already carries the session factory,
and an import-time side effect would make the module unimportable on a machine with no
database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings
from app.container import AppContainer
from app.db.session import configure_event_loop_policy
from app.jobs import shutdown_scheduler, start_scheduler
from app.logging import configure_logging


def create_app(settings: Settings, container: AppContainer) -> FastAPI:
    """Build the FastAPI app. Signature is frozen: the integration suite calls this exact form."""

    # psycopg's async mode cannot run on Windows' default ProactorEventLoop, so the
    # selector loop must be selected before the running loop opens its first connection.
    # A no-op on Linux, where production runs.
    configure_event_loop_policy()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        app.state.scheduler = start_scheduler(container)
        try:
            yield
        finally:
            scheduler = app.state.scheduler
            if scheduler is not None:
                shutdown_scheduler(scheduler)
            app.state.scheduler = None

    application = FastAPI(title="notion-email-agent", version="0.1.0", lifespan=lifespan)

    # The container rides on state, which is how every dependency reaches its adapters
    # (`app.api.deps.get_container`, `app.db.session.get_session`).
    application.state.container = container
    application.state.scheduler = None
    application.state.last_tick_at = None
    application.state.last_sync_at = None

    application.include_router(router)
    return application
