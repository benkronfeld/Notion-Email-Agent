"""Database helpers for the integration suite.

CLAUDE.md constraint 1: the only database a test may ever touch is the local Postgres from
`docker-compose.yml`. This module therefore reads **`TEST_DATABASE_URL` only** — it never
falls back to `DATABASE_URL`, and it refuses to TRUNCATE a non-local host — so a stray
production connection string in the environment cannot be reached from a test run.

`truncate_all` deliberately uses raw SQL with literal table names instead of importing
`app.db.models`: the integration suite must still be collectable if the models are mid-edit,
and a table rename should show up as a loud SQL error rather than a silently-dropped table.

Note for test authors: pytest collects any module-level name starting with `test` inside a
*test module*, so never write `from fixtures.db import test_database_url` in a `test_*.py`
file — import the module (`from fixtures import db as db_helpers`) and call
`db_helpers.test_database_url()` instead, or it is collected as a test of its own.
"""

from __future__ import annotations

import os
import socket
import time
from functools import lru_cache

import psycopg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

# A loopback default is safe: it can only ever be the docker-compose Postgres.
#
# `127.0.0.1`, NOT `localhost`. Docker publishes the container on IPv4 loopback only, but
# `localhost` resolves to `::1` first on Windows — so libpq would spend its whole connect
# budget on an unreachable IPv6 address and report a misleading auth error.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/notion_agent_test"
)

# Every table in §2.3.5, in dependency order. CASCADE makes the order moot, but keeping it
# readable means a new table is obviously missing from this list.
TABLES: tuple[str, ...] = (
    "items",
    "reminders",
    "email_threads",
    "outbound_messages",
    "processed_inbound_messages",
    "audit_log",
    "system_state",
    "courses",
)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# The database libpq connects to in order to run CREATE DATABASE / probe the server. Every
# Postgres server ships one.
_MAINTENANCE_DB = "postgres"


def test_database_url() -> str:
    """The test database URL. Reads `TEST_DATABASE_URL`; never `DATABASE_URL`."""
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def psycopg_dsn(url: str | None = None) -> str:
    """The libpq form of the test URL.

    `psycopg.connect` rejects SQLAlchemy's `+psycopg` driver marker, so the probe strips it
    rather than keeping a second environment variable in sync.
    """
    return (url if url is not None else test_database_url()).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def is_local(url: str) -> bool:
    """Whether `url` points at loopback (the docker-compose Postgres)."""
    try:
        return (make_url(url).host or "") in _LOCAL_HOSTS
    except Exception:  # a URL we cannot parse is certainly not a database we may truncate
        return False


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Whether a TCP connect completes inside `timeout`, spent across all resolved addresses.

    `psycopg.connect(connect_timeout=1)` is per address and excludes DNS, so on a machine
    where the port is silently dropped (Windows, Docker stopped) it can take ~4 seconds —
    once per process, on every test session. This bounds the whole attempt instead.
    """
    deadline = time.monotonic() + timeout
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not addresses:
        return False
    # IPv4 first, and a fair slice of the budget each. A Docker port mapping binds only
    # 127.0.0.1, so trying the IPv6 address first would consume the entire timeout and
    # never reach the address that actually answers.
    ordered = sorted(addresses, key=lambda entry: entry[0] != socket.AF_INET)
    per_attempt = max(timeout / len(ordered), 0.2)
    for family, socktype, proto, _canonname, sockaddr in ordered:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(min(per_attempt, remaining))
                sock.connect(sockaddr)
                return True
        except OSError:
            continue
    return False


@lru_cache(maxsize=1)
def postgres_available() -> bool:
    """Whether a Postgres *server* is reachable, probed once per process.

    Probes the server's maintenance database, **not** `TEST_DATABASE_URL`'s database. The
    test database is created lazily by the integration harness, so requiring it to already
    exist here would be circular: the check would fail, the harness would never run, and
    the database would never be created. Every integration test would skip forever, even
    against a healthy server.

    Any failure — Docker not running, a firewalled host, a rejected password — is `False`,
    which is what keeps the unit suite green on a machine without a database. Integration
    tests skip on `False` instead of erroring.
    """
    url = make_url(test_database_url())
    if not _tcp_reachable(url.host or "localhost", url.port or 5432, timeout=1.0):
        return False  # nothing is listening; skip the (slower) libpq attempt
    try:
        with psycopg.connect(maintenance_dsn(test_database_url()), connect_timeout=2):
            return True
    except Exception:
        return False


def maintenance_dsn(url: str) -> str:
    """A libpq DSN for the server's maintenance database.

    `render_as_string(hide_password=False)` is load-bearing. `str(URL)` deliberately masks
    the password as `***`, so the obvious `str(url.set(database=...))` sends a literal `***`
    and every connection fails authentication — which the probe below would report as
    "no Postgres", silently skipping the entire integration suite against a healthy server.
    """
    parsed = make_url(url).set(database=_MAINTENANCE_DB)
    return psycopg_dsn(parsed.render_as_string(hide_password=False))


async def truncate_all(session: AsyncSession, *, allow_non_local: bool = False) -> None:
    """Empty every table and restart identities. Integration-test teardown/setup.

    `audit_log` is append-only *by the application* — truncating it here is test setup, not
    the app issuing a DELETE (the app never does; §2.3.5).

    Refuses a non-localhost database unless `allow_non_local=True`, so a mis-set
    `TEST_DATABASE_URL` fails loudly instead of destroying a real deployment's data.
    """
    url = test_database_url()
    if not allow_non_local and not is_local(url):
        raise RuntimeError(
            f"refusing to TRUNCATE {url!r}: it is not a localhost database. Tests may only "
            "touch the docker-compose Postgres (CLAUDE.md constraint 1)."
        )
    await session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    await session.commit()
