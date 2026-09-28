"""Shared pytest fixtures.

Two rules shape this file, and both exist to keep a test from touching the real world
(CLAUDE.md constraints 1 and 5):

1. **No test ever reads `.env` or inherits a process environment value.** The `settings`
   fixture passes `_env_file=None` *and* an explicit value for every field `Settings`
   declares. `_env_file=None` alone would not be enough: pydantic-settings still reads
   `os.environ`, so a real `DATABASE_URL`, `REMINDER_RECIPIENT`, or `NOTION_TOKEN` exported
   in the shell would silently become the test's value. A test that picked up the
   production `DATABASE_URL` could move a real deadline; one that picked up a real sender
   account could mail a real person. Explicit values outrank environment variables, so
   passing them all is what makes that impossible.
2. **No fixture calls `datetime.now()`.** `frozen_clock` is a `FrozenClock` (constraint 4).

Fakes live in `tests/fixtures/fakes.py`; page builders in `tests/fixtures/pages.py`;
database helpers in `tests/fixtures/db.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.clock import FrozenClock
from app.config import Settings
from app.db.session import configure_event_loop_policy
from fixtures.db import postgres_available as _postgres_available

# Before pytest-asyncio creates any loop: psycopg's async mode cannot run on Windows'
# default ProactorEventLoop. No-op on Linux.
configure_event_loop_policy()

# A fixed instant (2026-10-01T00:00:00Z), well before the default due date in
# `fixtures.pages`, so "is this target still in the future?" has a stable answer.
FROZEN_INSTANT = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

# Every field `app.config.Settings` declares, with obviously fake values. No real
# credential, token, endpoint, or production ID belongs in a test (constraint 2), and the
# database URL is the localhost test database from `docker-compose.yml` (constraint 1).
TEST_SETTINGS_ENV: dict[str, Any] = {
    # ── Core ──
    "database_url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/notion_agent_test",
    "timezone": "America/New_York",
    "default_due_time": "23:59",
    "reminder_recipient": "owner@example.test",
    "admin_api_token": "test-admin-token",
    # ── Notion ──
    "notion_token": "test-notion-token",
    "notion_version": "2025-09-03",
    "notion_db_assignments_readings": "db-assignments",
    "notion_db_exams_projects": "db-exams",
    "notion_prop_title": "",
    "notion_prop_course": "Course",
    "notion_prop_type": "Type",
    "notion_prop_due": "Due Date",
    "notion_prop_status": "Status",
    "notion_prop_done": "Done",
    "notion_status_completed": "Completed",
    "notion_assignments_readings_has_done": True,
    "notion_exams_projects_has_done": True,
    "notion_sync_interval_min": 60,
    "notion_course_cache_ttl_hours": 24,
    "notion_full_reconcile_cron": "04:00",
    # ── Gmail (never contacted: tests use FakeMailClient) ──
    "gmail_sender_address": "sender@example.test",
    "gmail_client_id": "test-client-id",
    "gmail_client_secret": "test-client-secret",
    "gmail_refresh_token": "test-refresh-token",
    "gmail_poll_interval_sec": 90,
    # ── Scheduling & safety ──
    "reminder_scheduler_interval_sec": 300,
    "max_outbound_emails_per_hour": 20,
    "alert_cooldown_hours": 6,
    # ── V1 surface (declared so the config shape matches §2.2; unused by the MVP) ──
    "allowed_reply_senders": "owner@example.test",
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.invalid",
    "deepseek_model": "deepseek-flash",
    "max_clarification_rounds": 3,
    "max_date_shift_days": 60,
}


@pytest.fixture
def settings() -> Settings:
    """Test settings: no `.env`, and no inherited environment value for any field."""
    return Settings(_env_file=None, **TEST_SETTINGS_ENV)


@pytest.fixture
def frozen_clock(settings: Settings) -> FrozenClock:
    """A clock stopped at `FROZEN_INSTANT`, in the configured (never hardcoded) zone."""
    return FrozenClock(settings.timezone, FROZEN_INSTANT)


@pytest.fixture
def postgres_available() -> bool:
    """Whether the docker-compose test Postgres is reachable.

    Probed once per process with a 1-second connect, so a machine without Docker (or with
    the daemon stopped) makes integration tests *skip* rather than error.
    """
    return _postgres_available()
