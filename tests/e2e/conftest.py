"""End-to-end dry-run fixtures.

Same harness as `tests/integration/` — the frozen-clock dry run drives the real app over a
real Postgres with fake adapters, so it needs identical wiring. The fixtures live in
`fixtures/harness.py`; importing them here is what makes pytest discover them.
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
