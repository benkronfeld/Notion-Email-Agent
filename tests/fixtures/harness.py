"""The shared test harness: a real Postgres, fake Notion/Gmail, never a live service.

This module defines the fixtures that every database-backed test directory needs. It is not
a plugin — `pytest_plugins` is only honoured in a rootdir conftest, and ours is
`tests/` — so each conftest that wants them imports them by name:

    from fixtures.harness import client, session, ...  # noqa: F401

pytest picks up fixtures from a conftest's namespace whether they are defined there or
imported into it, so importing is all that is required.

Two directories use this: `tests/integration/` and `tests/e2e/`. They are separate
directories but want exactly the same wiring, and duplicating it would let the two drift.

Schema lifecycle: `alembic upgrade head` runs once per session against the test database.
Running the real migration is itself the migration's test — `metadata.create_all` would
happily produce a schema the migration cannot. Each test then starts from a truncated
database, which is fast and keeps tests independent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.clock import FrozenClock
from app.config import Settings
from app.container import AppContainer
from fixtures import db as db_helpers
from fixtures.fakes import FakeMailClient, FakeNotionClient, make_container

ROOT = Path(__file__).resolve().parents[2]

_START_DB = "docker compose up -d db"

__all__: list[str] = [
    "admin_headers",
    "app",
    "app_container",
    "client",
    "engine",
    "mail",
    "migrated_database",
    "notion",
    "session",
]


def _ensure_test_database(url: str) -> None:
    """Create the test database if it is missing, so `docker compose up -d db` is enough.

    Refuses a non-loopback host: the compose server is the only database a test may touch,
    and `CREATE DATABASE` against someone else's server would be a real change to a real
    system (CLAUDE.md constraint 1).
    """
    if not db_helpers.is_local(url):
        raise RuntimeError(
            f"refusing to create a database on {url!r}: it is not a localhost database. "
            "Tests may only touch the docker-compose Postgres (CLAUDE.md constraint 1)."
        )

    parsed = make_url(url)
    name = parsed.database
    if not name:
        raise RuntimeError(f"TEST_DATABASE_URL has no database name: {url!r}")

    with psycopg.connect(
        db_helpers.maintenance_dsn(url), autocommit=True, connect_timeout=2
    ) as conn:
        existing = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if existing is None:
            # Identifier-quoted rather than interpolated: the name comes from config, but
            # building DDL by string concatenation is a habit worth not having.
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _run_migrations(url: str) -> None:
    """Apply the real migration to the test database."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """The migrated test database, or a skip when Postgres is not reachable.

    Every connection-needing fixture depends on this, so one probe decides the fate of the
    whole directory — there is no half-state where some tests run against a schema that was
    never applied.
    """
    url = db_helpers.test_database_url()
    if not db_helpers.postgres_available():
        pytest.skip(f"no Postgres reachable — start it with `{_START_DB}`")
    _ensure_test_database(url)
    _run_migrations(url)
    return url


@pytest.fixture(autouse=True)
def _require_postgres() -> None:
    """Skip every database-backed test in this directory when there is no database."""
    if not db_helpers.postgres_available():
        pytest.skip(f"no Postgres reachable — start it with `{_START_DB}`")


@pytest.fixture
async def engine(settings: Settings, migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """An engine bound to the database the migration actually ran against.

    Built from `migrated_database`, NOT `settings.database_url`. The two differ whenever
    `TEST_DATABASE_URL` selects an isolated database: binding to the literal settings URL
    would create and migrate one database and then connect the tests to a different one, so
    the suite would run against an unmigrated schema — or against another run's data.
    """
    from app.db.session import create_engine

    engine = create_engine(settings.model_copy(update={"database_url": migrated_database}))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session onto an empty database.

    Truncating up front rather than at teardown means a failed test leaves its rows behind
    for inspection, and the next test still starts clean.

    A test that reads the database must request this fixture even if it only writes through
    the API — otherwise nothing truncates and it inherits the previous test's rows.
    """
    from app.db.session import create_session_factory

    factory = create_session_factory(engine)
    async with factory() as session:
        await db_helpers.truncate_all(session)
        yield session


@pytest.fixture
def notion() -> FakeNotionClient:
    """A fake Notion with no pages. Tests add what they need."""
    return FakeNotionClient()


@pytest.fixture
def mail() -> FakeMailClient:
    """A fake Gmail that records sends and can be made to fail."""
    return FakeMailClient()


@pytest.fixture
def app_container(
    settings: Settings,
    frozen_clock: FrozenClock,
    engine: AsyncEngine,
    notion: FakeNotionClient,
    mail: FakeMailClient,
) -> AppContainer:
    """A fully wired container of fakes over the real test database."""
    from app.db.session import create_session_factory

    return make_container(
        settings=settings,
        clock=frozen_clock,
        session_factory=create_session_factory(engine),
        notion=notion,
        mail=mail,
    )


@pytest.fixture
async def app(app_container: AppContainer) -> AsyncIterator[FastAPI]:
    """The FastAPI app over that container, with its lifespan running.

    `httpx.ASGITransport` does not run startup/shutdown, and the scheduler is started from
    the lifespan — so the lifespan context is entered explicitly. Without it `/readyz`
    would report a scheduler that never started and the test would prove nothing.
    """
    from app.main import create_app

    application = create_app(app_container.settings, app_container)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client against the app, with the lifespan already running."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def admin_headers(settings: Settings) -> dict[str, str]:
    """The bearer token every route except `/healthz` requires."""
    return {"Authorization": f"Bearer {settings.admin_api_token}"}
