"""Async engine and session factory construction (spec §2.2: psycopg 3 + SQLAlchemy 2.0).

The engine is created once at startup and its `async_sessionmaker` is placed on
`AppContainer.session_factory`, so services and the API share one pool.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def configure_event_loop_policy() -> None:
    """Use a selector event loop on Windows.

    psycopg's async mode refuses to run on Windows' default `ProactorEventLoop` — it
    raises `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async
    mode` on the first connection. Every async database path (the app, the migration, and
    the test suite) therefore has to select the loop before it is created.

    This is a no-op everywhere else, and production runs on Linux, so it changes nothing
    about deployed behaviour — without it the app simply cannot start on Windows.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for `settings.database_url` (a `postgresql+psycopg` URL).

    `pool_pre_ping` is on so a connection dropped by a deploy or an idle timeout is
    discarded and re-established instead of failing a reminder send.
    """
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to `engine`.

    `expire_on_commit=False` so an object returned from a repository is still readable
    after the service commits (the app does not re-query on every attribute access).
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a session from the app-wide factory.

    The factory rides on `AppContainer` (exposed as `request.app.state.container`), so
    tests that boot the app with fake adapters swap the database in one place.
    """
    container = request.app.state.container
    session_factory: async_sessionmaker[AsyncSession] = container.session_factory
    async with session_factory() as session:
        yield session
