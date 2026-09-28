"""config.Settings covers §2.2 and validates the values that would silently break things."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings

# Obviously fake values. No real credential, token, or production ID belongs in a test
# (CLAUDE.md constraint 2).
REQUIRED: dict[str, str] = {
    "database_url": "postgresql+psycopg://postgres:postgres@localhost:5432/notion_agent_test",
    "reminder_recipient": "owner@example.test",
    "admin_api_token": "test-admin-token",
    "notion_token": "test-notion-token",
    "notion_db_assignments_readings": "db-assignments-test",
    "notion_db_exams_projects": "db-exams-test",
    "gmail_sender_address": "sender@example.test",
    "gmail_client_id": "test-client-id",
    "gmail_client_secret": "test-client-secret",
    "gmail_refresh_token": "test-refresh-token",
}


def make_settings(**overrides: Any) -> Settings:
    """Build Settings from explicit values.

    `_env_file=None` is load-bearing: without it pydantic-settings would read the real
    `.env` on this machine. Explicit keyword arguments also outrank real environment
    variables, so a developer with a production DATABASE_URL exported still gets the
    test database (CLAUDE.md constraint 1).
    """
    values: dict[str, Any] = dict(REQUIRED)
    values.update(overrides)
    return Settings(_env_file=None, **values)


class TestRequiredValues:
    @pytest.mark.parametrize("missing", sorted(REQUIRED))
    def test_each_required_value_is_actually_required(self, missing: str) -> None:
        values: dict[str, Any] = {k: v for k, v in REQUIRED.items() if k != missing}
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)

    def test_explicit_values_outrank_environment_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The safety property that keeps a test off production.
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://someone/elsewhere")
        monkeypatch.setenv("NOTION_TOKEN", "a-real-looking-token")
        settings = make_settings()
        assert "notion_agent_test" in settings.database_url
        assert settings.notion_token == "test-notion-token"


class TestDefaults:
    def test_spec_defaults(self) -> None:
        settings = make_settings()
        assert settings.timezone == "America/New_York"
        assert settings.default_due_time == time(23, 59)
        assert settings.notion_version == "2025-09-03"
        assert settings.notion_prop_course == "Course"
        assert settings.notion_prop_type == "Type"
        assert settings.notion_prop_due == "Due Date"
        assert settings.notion_prop_status == "Status"
        assert settings.notion_prop_done == "Done"
        assert settings.notion_status_completed == "Completed"
        assert settings.notion_assignments_readings_has_done is True
        assert settings.notion_exams_projects_has_done is True
        assert settings.notion_sync_interval_min == 60
        assert settings.notion_course_cache_ttl_hours == 24
        assert settings.notion_full_reconcile_cron == "04:00"
        assert settings.reminder_scheduler_interval_sec == 300
        assert settings.gmail_poll_interval_sec == 90
        assert settings.max_outbound_emails_per_hour == 20
        assert settings.alert_cooldown_hours == 6

    def test_the_outbound_cap_defaults_to_the_kill_switch_value(self) -> None:
        # MAX_OUTBOUND_EMAILS_PER_HOUR is a kill switch, not a testing convenience.
        assert make_settings().max_outbound_emails_per_hour == 20

    def test_default_due_time_parses_from_a_string(self) -> None:
        assert make_settings(default_due_time="17:30").default_due_time == time(17, 30)

    def test_extra_environment_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_UNRELATED_VAR", "x")
        assert make_settings().timezone == "America/New_York"


class TestValidation:
    def test_unknown_timezone_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError, match="unknown TIMEZONE"):
            make_settings(timezone="Not/AZone")

    def test_a_real_but_different_zone_is_allowed(self) -> None:
        # The zone is configuration, never a hardcoded constant.
        assert make_settings(timezone="UTC").tz.key == "UTC"

    @pytest.mark.parametrize("cron", ["4:00", "04:0", "0400", "24:00", "04:60", "", "4am"])
    def test_bad_reconcile_cron_is_rejected(self, cron: str) -> None:
        with pytest.raises(ValidationError, match="HH:MM"):
            make_settings(notion_full_reconcile_cron=cron)

    @pytest.mark.parametrize("cron", ["00:00", "04:00", "23:59"])
    def test_good_reconcile_cron_is_accepted(self, cron: str) -> None:
        assert make_settings(notion_full_reconcile_cron=cron).notion_full_reconcile_cron == cron


class TestDerived:
    def test_tz_is_a_zoneinfo_in_the_configured_zone(self) -> None:
        assert make_settings(timezone="America/New_York").tz.key == "America/New_York"

    def test_full_reconcile_at_is_0400_by_default(self) -> None:
        assert make_settings().full_reconcile_at == time(4, 0)

    def test_full_reconcile_at_follows_config(self) -> None:
        assert make_settings(notion_full_reconcile_cron="06:30").full_reconcile_at == time(6, 30)

    def test_reply_senders_are_lowercased_and_split(self) -> None:
        settings = make_settings(allowed_reply_senders="A@Example.test, b@example.test ,")
        assert settings.reply_senders == frozenset({"a@example.test", "b@example.test"})

    def test_has_done_property_per_database(self) -> None:
        settings = make_settings()
        assert settings.has_done_property("assignments_readings") is True
        assert settings.has_done_property("exams_projects") is True

    def test_has_done_property_can_be_disabled_per_database(self) -> None:
        settings = make_settings(notion_assignments_readings_has_done=False)
        assert settings.has_done_property("assignments_readings") is False
        assert settings.has_done_property("exams_projects") is True

    def test_has_done_property_rejects_an_unknown_database(self) -> None:
        with pytest.raises(ValueError, match="unknown source_db"):
            make_settings().has_done_property("something_else")

    def test_settings_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            make_settings().timezone = "UTC"  # type: ignore[misc]


class TestV1Surface:
    def test_v1_config_exists_but_is_unused_by_the_mvp(self) -> None:
        # Declared so the config surface matches §2.2. Nothing in phases 1-3 reads them.
        settings = make_settings()
        assert settings.deepseek_base_url == "https://api.deepseek.com"
        assert settings.deepseek_model == "deepseek-flash"
        assert settings.max_clarification_rounds == 3
        assert settings.max_date_shift_days == 60
        assert settings.deepseek_api_key == ""
