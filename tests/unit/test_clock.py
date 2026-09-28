"""The Clock port: aware UTC always, local conversion only for human-facing rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from app.clock import Clock, FrozenClock, SystemClock

NYC = ZoneInfo("America/New_York")


class TestSystemClock:
    def test_exposes_the_configured_zone(self) -> None:
        assert SystemClock("America/New_York").tz == NYC

    def test_unknown_zone_fails_at_construction(self) -> None:
        # Fail at startup, not at the first reminder.
        with pytest.raises(ZoneInfoNotFoundError):
            SystemClock("Not/AZone")

    def test_now_is_aware_utc(self) -> None:
        now = SystemClock("America/New_York").now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_now_local_matches_the_zone(self) -> None:
        clock = SystemClock("America/New_York")
        assert clock.now_local().tzinfo is not None
        assert clock.now_local().utcoffset() == clock.now().astimezone(NYC).utcoffset()

    def test_today_local_is_a_date(self) -> None:
        assert isinstance(SystemClock("America/New_York").today_local(), date)

    def test_satisfies_the_clock_protocol(self) -> None:
        clock: Clock = SystemClock("America/New_York")
        assert clock.today_local() == clock.now_local().date()


class TestFrozenClock:
    def test_now_returns_the_frozen_instant_in_utc(self) -> None:
        clock = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
        assert clock.now() == datetime(2026, 10, 1, tzinfo=UTC)

    def test_a_naive_instant_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            FrozenClock("America/New_York", datetime(2026, 10, 1))

    def test_advance_moves_time_forward(self) -> None:
        clock = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
        clock.advance(timedelta(hours=48))
        assert clock.now() == datetime(2026, 10, 3, tzinfo=UTC)

    def test_set_replaces_the_instant(self) -> None:
        clock = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
        clock.set(datetime(2026, 12, 25, 12, 0, tzinfo=UTC))
        assert clock.now() == datetime(2026, 12, 25, 12, 0, tzinfo=UTC)

    def test_set_rejects_a_naive_instant(self) -> None:
        clock = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
        with pytest.raises(ValueError, match="timezone-aware"):
            clock.set(datetime(2026, 10, 2))

    def test_advance_does_not_move_the_real_clock(self) -> None:
        # Advancing a frozen clock must not perturb anything global.
        before = SystemClock("America/New_York").now()
        far_future = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
        far_future.advance(timedelta(days=365))
        assert SystemClock("America/New_York").now() - before < timedelta(minutes=1)

    def test_today_local_uses_the_zone_not_utc(self) -> None:
        # 02:00 UTC on Oct 4 is still 22:00 on Oct 3 in Eastern. If today_local() read
        # the UTC date, "is this due date in the past?" would be off by a day near
        # midnight — which is exactly when most reminders fire.
        clock = FrozenClock("America/New_York", datetime(2026, 10, 4, 2, 0, tzinfo=UTC))
        assert clock.today_local() == date(2026, 10, 3)
        assert clock.now().date() == date(2026, 10, 4)

    def test_today_local_at_eastern_midnight(self) -> None:
        clock = FrozenClock("America/New_York", datetime(2026, 10, 4, 4, 0, tzinfo=UTC))
        assert clock.today_local() == date(2026, 10, 4)

    def test_satisfies_the_clock_protocol(self) -> None:
        clock: Clock = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
        assert clock.now_local().tzinfo is not None


def test_frozen_clocks_are_independent() -> None:
    a = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
    b = FrozenClock("America/New_York", datetime(2026, 10, 1, tzinfo=UTC))
    a.advance(timedelta(days=1))
    assert a.now() != b.now()


def test_default_due_time_is_reachable_from_the_zone() -> None:
    # A sanity tie between the clock and the due-time rule, using the configured zone.
    clock = FrozenClock("America/New_York", datetime(2026, 10, 3, 23, 59, tzinfo=NYC))
    assert clock.now_local().time() == time(23, 59)
