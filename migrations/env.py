"""Alembic environment.

Deliberately does **not** construct `app.config.Settings`: `Settings` requires nine
environment variables that a migration has no business demanding (Notion tokens, Gmail
credentials, …). Only the database URL is needed, resolved in this order:

1. `alembic -x url=...`
2. the `sqlalchemy.url` option in `alembic.ini`
3. the `DATABASE_URL` environment variable

Both offline (`--sql`, no connection) and online (async, psycopg 3) modes are supported.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import MetaData, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Put `src` on sys.path so `app.db.models` imports regardless of the working directory.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_metadata() -> MetaData:
    """Import the models lazily so the sys.path tweak above is already in effect."""
    from app.db.models import Base

    return Base.metadata


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = _load_metadata()


def _database_url() -> str:
    """Resolve the one value migrations need, without building full application Settings."""
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("url") or config.get_main_option("sqlalchemy.url")
    if not url:
        url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database URL for migrations. Pass `-x url=...`, set `sqlalchemy.url` in "
            "alembic.ini, or set the DATABASE_URL environment variable."
        )
    return url


def run_migrations_offline() -> None:
    """Render SQL to stdout (`alembic upgrade head --sql`) without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    # psycopg's async mode cannot use Windows' default ProactorEventLoop; select the
    # loop before asyncio.run creates one. No-op on Linux.
    from app.db.session import configure_event_loop_policy

    configure_event_loop_policy()
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
