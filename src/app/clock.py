"""The `Clock` port (spec §2.3.6).

CLAUDE.md constraint 4: all time comes from an injected `Clock`; application code never
calls `datetime.now()`, `date.today()`, or `time.time()`. `SystemClock` is the single
sanctioned adapter onto the real wall clock — nothing else in `src/` may reach for it.

Every instant returned is timezone-aware. Naive datetimes are never produced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    """Time, as the application sees it."""

    tz: ZoneInfo

    def now(self) -> datetime:
        """The current instant, aware and in UTC — the only form ever stored or compared."""
        ...

    def now_local(self) -> datetime:
        """The current instant converted to `tz`, for human-facing rendering only."""
        ...

    def today_local(self) -> date:
        """Today's date in `tz`. Use for "is this date in the past" questions."""
        ...


class SystemClock:
    """The real clock. Constructed once at startup and injected everywhere."""

    def __init__(self, tz_name: str) -> None:
        # Raises ZoneInfoNotFoundError here, at startup, rather than at the first reminder.
        self.tz = ZoneInfo(tz_name)

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_local(self) -> datetime:
        return self.now().astimezone(self.tz)

    def today_local(self) -> date:
        return self.now_local().date()


class FrozenClock:
    """A clock that only moves when a test (or a dry run) says so.

    Ships in `src/` rather than `tests/` because the frozen-clock end-to-end dry run and
    any future manual dry-run tool both need it.
    """

    def __init__(self, tz_name: str, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware instant")
        self.tz = ZoneInfo(tz_name)
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def now_local(self) -> datetime:
        return self._instant.astimezone(self.tz)

    def today_local(self) -> date:
        return self.now_local().date()

    def advance(self, delta: timedelta) -> None:
        self._instant = self._instant + delta

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock.set requires a timezone-aware instant")
        self._instant = instant.astimezone(UTC)
