"""Application configuration (spec §2.2).

Every configurable value is read from the environment. `.env` is loaded for local
development convenience, but **tests must never inherit it** — a test that picked up a
real `DATABASE_URL` could reach production, which CLAUDE.md constraint 1 forbids.
Tests therefore build `Settings` explicitly and pass `_env_file=None`.
"""

from __future__ import annotations

import re
from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class Settings(BaseSettings):
    """Environment-backed settings. See `.env.example` for documentation of each."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # ── Core ────────────────────────────────────────────────────────────────
    database_url: str
    timezone: str = "America/New_York"
    default_due_time: time = time(23, 59)
    reminder_recipient: str
    admin_api_token: str

    # ── Notion ──────────────────────────────────────────────────────────────
    notion_token: str
    notion_version: str = "2025-09-03"
    notion_db_assignments_readings: str
    notion_db_exams_projects: str

    notion_prop_title: str = ""
    notion_prop_course: str = "Course"
    notion_prop_type: str = "Type"
    notion_prop_due: str = "Due Date"
    notion_prop_status: str = "Status"
    notion_prop_done: str = "Done"

    notion_status_completed: str = "Completed"
    notion_assignments_readings_has_done: bool = True
    notion_exams_projects_has_done: bool = True

    notion_sync_interval_min: int = 60
    notion_course_cache_ttl_hours: int = 24
    notion_full_reconcile_cron: str = "04:00"

    # ── Gmail ───────────────────────────────────────────────────────────────
    gmail_sender_address: str
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    gmail_poll_interval_sec: int = 90

    # ── Scheduling & safety ─────────────────────────────────────────────────
    reminder_scheduler_interval_sec: int = 300
    max_outbound_emails_per_hour: int = 20
    alert_cooldown_hours: int = 6

    # ── V1 only. Declared so the config surface matches §2.2; unused by the MVP. ──
    allowed_reply_senders: str = "bek229@lehigh.edu"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    max_clarification_rounds: int = 3
    max_date_shift_days: int = 60

    # ── Validators ──────────────────────────────────────────────────────────

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Fail at startup rather than at the first reminder if the zone is unknown."""
        try:
            ZoneInfo(value)
        except Exception as exc:  # re-raised with clearer context
            raise ValueError(f"unknown TIMEZONE {value!r}: {exc}") from exc
        return value

    @field_validator("notion_full_reconcile_cron")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM_RE.match(value):
            raise ValueError(f"NOTION_FULL_RECONCILE_CRON must be HH:MM (24h), got {value!r}")
        return value

    # ── Derived helpers ─────────────────────────────────────────────────────

    @property
    def tz(self) -> ZoneInfo:
        """The configured timezone. Never hardcode a zone or a fixed offset (constraint 3)."""
        return ZoneInfo(self.timezone)

    @property
    def full_reconcile_at(self) -> time:
        """Local time of day the daily full reconcile runs (default 04:00 Eastern)."""
        hour, minute = self.notion_full_reconcile_cron.split(":")
        return time(int(hour), int(minute))

    @property
    def reply_senders(self) -> frozenset[str]:
        """Allowlisted reply senders, lowercased. V1 only."""
        return frozenset(
            part.strip().lower() for part in self.allowed_reply_senders.split(",") if part.strip()
        )

    def has_done_property(self, source_db: str) -> bool:
        """Whether the `Done` checkbox exists in the given database (spec §2.2)."""
        if source_db == "assignments_readings":
            return self.notion_assignments_readings_has_done
        if source_db == "exams_projects":
            return self.notion_exams_projects_has_done
        raise ValueError(f"unknown source_db {source_db!r}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. The app calls this; tests build `Settings` directly."""
    return Settings()
