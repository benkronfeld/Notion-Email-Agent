"""Frozen-clock helpers.

`FrozenClock` itself lives in `src/app/clock.py` (the frozen-clock end-to-end dry run needs
it in the app, not just in tests); this module re-exports it and adds the constructors that
keep tests short.

CLAUDE.md constraint 4: no fixture calls `datetime.now()`. Every instant built here is
timezone-aware, and the zone always arrives through `tz_name` — the default matches the
deployment timezone (§2.2) but the value is a parameter, never a hardcoded offset.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.clock import FrozenClock
from app.domain.due_time import compute_due_at

__all__ = [
    "DEFAULT_DUE_DATE",
    "DEFAULT_TZ",
    "FrozenClock",
    "at_due_minus",
    "at_due_plus",
    "default_due_at",
    "due_at_from",
    "frozen_at",
]

DEFAULT_TZ = "America/New_York"

# A specific date a test can reason about. 2026-10-03 is inside EDT, so its due instant is
# 2026-10-04T03:59Z — the case CLAUDE.md constraint 3 calls out explicitly.
DEFAULT_DUE_DATE = date(2026, 10, 3)


def _as_aware(value: str | datetime, tz_name: str) -> datetime:
    """An ISO string or datetime as an aware instant. A naive value is read in `tz_name`."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed


def frozen_at(iso_or_datetime: str | datetime, tz_name: str = DEFAULT_TZ) -> FrozenClock:
    """A clock stopped at `iso_or_datetime`, e.g. `frozen_at("2026-10-01T00:00:00Z")`.

    A string with no offset (`"2026-10-01 00:00"`) is interpreted in `tz_name`.
    """
    return FrozenClock(tz_name, _as_aware(iso_or_datetime, tz_name))


def default_due_at(tz_name: str = DEFAULT_TZ) -> datetime:
    """The aware UTC due instant for `DEFAULT_DUE_DATE` (date-only -> 11:59 PM `tz_name`)."""
    return compute_due_at(DEFAULT_DUE_DATE, ZoneInfo(tz_name))


def due_at_from(day: date, tz_name: str = DEFAULT_TZ) -> datetime:
    """The aware UTC due instant for any date-only Notion due date (FR-1)."""
    return compute_due_at(day, ZoneInfo(tz_name))


def at_due_minus(
    hours: float,
    *,
    due_at: datetime | None = None,
    tz_name: str = DEFAULT_TZ,
) -> FrozenClock:
    """A clock stopped exactly `hours` before the due instant.

    `at_due_minus(48)` sits on the `assignment_48h` target itself: the boundary the
    scheduler claims at `target_at <= now`, and the instant FR-4's "never send once
    `now >= due_at`" is measured against.
    """
    base = due_at if due_at is not None else default_due_at(tz_name)
    return FrozenClock(tz_name, base - timedelta(hours=hours))


def at_due_plus(
    hours: float,
    *,
    due_at: datetime | None = None,
    tz_name: str = DEFAULT_TZ,
) -> FrozenClock:
    """A clock stopped `hours` after the due instant — a past-due item (FR-4)."""
    base = due_at if due_at is not None else default_due_at(tz_name)
    return FrozenClock(tz_name, base + timedelta(hours=hours))
