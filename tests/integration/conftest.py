"""Integration-test fixtures.

The fixtures themselves live in `fixtures/harness.py`, because `tests/e2e/` needs exactly
the same wiring and duplicating it would let the two drift. Importing them into this
conftest's namespace is what makes pytest discover them here.

Write tests in this directory with a module-level::

    pytestmark = pytest.mark.integration

so `pytest -m "not integration"` deselects them on a machine without a database. They also
skip on their own when no Postgres is reachable, so `pytest` alone stays green without
Docker.
"""

from __future__ import annotations

from fixtures.harness import (  # noqa: F401  (pytest finds fixtures by name)
    admin_headers,
    app,
    app_container,
    client,
    engine,
    mail,
    migrated_database,
    notion,
    session,
)
