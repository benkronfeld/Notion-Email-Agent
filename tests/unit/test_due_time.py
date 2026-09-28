"""FR-1: a date-only due date means 11:59 PM in the configured zone, stored as UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.due_time import (
    compute_due_at,
    compute_due_at_from_notion_date,
    parse_due_time,
)

# Tests pin Eastern deliberately: it is the deployment zone and the spec's worked
# examples are in it. Production code never hardcodes the zone — it always arrives
# as a ZoneInfo argument or from Settings.tz.
NYC = ZoneInfo("America/New_York")


class TestComputeDueAt:
    def test_edt_date_becomes_0359_utc_next_day(self) -> None:
        # The spec's own example: Oct 3 2026 is 2026-10-04 03:59 UTC while EDT is in
        # effect — not 2026-10-03 23:59 UTC (CLAUDE.md constraint 3).
        assert compute_due_at(date(2026, 10, 3), NYC) == datetime(2026, 10, 4, 3, 59, tzinfo=UTC)

    def test_est_date_uses_the_other_offset(self) -> None:
        # Nov 2 2026 is EST (-05:00). If the offset were hardcoded this would be wrong.
        assert compute_due_at(date(2026, 11, 2), NYC) == datetime(2026, 11, 3, 4, 59, tzinfo=UTC)

    def test_fall_back_boundary_is_49_hours_not_48(self) -> None:
        # Oct 31 (EDT) -> Nov 2 (EST) is 49 real hours because the clocks go back.
        # This is the concrete proof that zoneinfo is doing the work.
        before = compute_due_at(date(2026, 10, 31), NYC)
        after = compute_due_at(date(2026, 11, 2), NYC)
        assert after - before == timedelta(hours=49)

    def test_spring_forward_boundary_is_47_hours(self) -> None:
        # Mar 6 (EST) -> Mar 8 (EDT) 2026, clocks go forward.
        before = compute_due_at(date(2026, 3, 6), NYC)
        after = compute_due_at(date(2026, 3, 8), NYC)
        assert after - before == timedelta(hours=47)

    def test_result_is_aware_utc(self) -> None:
        result = compute_due_at(date(2026, 10, 3), NYC)
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_time_of_day_is_configurable(self) -> None:
        assert compute_due_at(date(2026, 10, 3), NYC, time(9, 0)) == datetime(
            2026, 10, 3, 13, 0, tzinfo=UTC
        )

    def test_midnight_due_time(self) -> None:
        assert compute_due_at(date(2026, 10, 3), NYC, time(0, 0)) == datetime(
            2026, 10, 3, 4, 0, tzinfo=UTC
        )

    def test_zone_is_a_parameter_not_a_constant(self) -> None:
        # A zone with no DST behaves differently for the same date, proving the zone
        # is honoured rather than assumed.
        assert compute_due_at(date(2026, 10, 3), ZoneInfo("UTC")) == datetime(
            2026, 10, 3, 23, 59, tzinfo=UTC
        )

    def test_rejects_aware_time_of_day(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            compute_due_at(date(2026, 10, 3), NYC, time(23, 59, tzinfo=UTC))


class TestParseDueTime:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("23:59", time(23, 59)),
            ("00:00", time(0, 0)),
            ("7:05", time(7, 5)),
            ("07:05", time(7, 5)),
        ],
    )
    def test_valid(self, raw: str, expected: time) -> None:
        assert parse_due_time(raw) == expected

    @pytest.mark.parametrize("raw", ["24:00", "23:60", "abc", "", "23", "23:59:00", "-1:00"])
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_due_time(raw)


class TestComputeDueAtFromNotionDate:
    def test_date_only_uses_the_default_due_time(self) -> None:
        due_date, due_at, has_time = compute_due_at_from_notion_date(
            "2026-10-03", NYC, time(23, 59)
        )
        assert due_date == date(2026, 10, 3)
        assert due_at == datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
        assert has_time is False

    def test_date_with_time_and_offset_honours_the_offset(self) -> None:
        due_date, due_at, has_time = compute_due_at_from_notion_date(
            "2026-10-03T14:30:00-04:00", NYC, time(23, 59)
        )
        assert due_at == datetime(2026, 10, 3, 18, 30, tzinfo=UTC)
        assert due_date == date(2026, 10, 3)
        assert has_time is True

    def test_date_with_time_and_no_offset_is_read_in_the_configured_zone(self) -> None:
        _, due_at, has_time = compute_due_at_from_notion_date(
            "2026-10-03T14:30:00", NYC, time(23, 59)
        )
        assert due_at == datetime(2026, 10, 3, 18, 30, tzinfo=UTC)
        assert has_time is True

    def test_utc_zulu_suffix_is_accepted(self) -> None:
        _, due_at, has_time = compute_due_at_from_notion_date(
            "2026-10-03T14:30:00Z", NYC, time(23, 59)
        )
        assert due_at == datetime(2026, 10, 3, 14, 30, tzinfo=UTC)
        assert has_time is True

    def test_due_date_is_the_local_date_for_a_timed_value(self) -> None:
        # 2026-10-04T02:00Z is still Oct 3 in Eastern. due_date stays "as entered".
        due_date, due_at, _ = compute_due_at_from_notion_date(
            "2026-10-04T02:00:00Z", NYC, time(23, 59)
        )
        assert due_date == date(2026, 10, 3)
        assert due_at == datetime(2026, 10, 4, 2, 0, tzinfo=UTC)

    def test_default_due_time_is_configurable(self) -> None:
        _, due_at, has_time = compute_due_at_from_notion_date("2026-10-03", NYC, time(17, 0))
        assert due_at == datetime(2026, 10, 3, 21, 0, tzinfo=UTC)
        assert has_time is False
