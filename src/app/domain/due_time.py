"""The due-time rule (FR-1, spec §2.3.4).

A date-only due date means 11:59 PM in the configured timezone, stored as UTC. If the
Notion date carries a time, that time is used instead.

Pure: the zone always arrives as a `ZoneInfo` argument. Nothing here reads config.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# A bare calendar date, e.g. "2026-10-03" — the date-only Notion form.
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_due_time(raw: str) -> time:
    """Parse `DEFAULT_DUE_TIME` ("23:59"). Raises `ValueError` on anything else."""
    match = _HHMM_RE.match(raw.strip())
    if match is None:
        raise ValueError(f"due time must be HH:MM in 24-hour form, got {raw!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"due time out of range: {raw!r}")
    return time(hour, minute)


def compute_due_at(d: date, tz: ZoneInfo, time_of_day: time = time(23, 59)) -> datetime:
    """A calendar date plus a local wall-clock time, as an aware UTC instant.

    `time_of_day` must be naive; the zone comes from `tz`.

    `fold=0` is explicit: a fall-back DST transition can make a local wall time ambiguous.
    23:59 is never ambiguous in `America/New_York`, but the argument is written down so
    nobody later "simplifies" it away.
    """
    if time_of_day.tzinfo is not None:
        raise ValueError("time_of_day must be naive; the zone comes from tz")
    local = datetime.combine(d, time_of_day).replace(tzinfo=tz, fold=0)
    return local.astimezone(UTC)


def compute_due_at_from_notion_date(
    start: str, tz: ZoneInfo, default_time_of_day: time
) -> tuple[date, datetime, bool]:
    """Interpret a Notion date property's `start` value.

    Returns `(due_date, due_at_utc, due_has_time)`.

    - A bare `YYYY-MM-DD` is date-only: the due time is `default_time_of_day` in `tz`.
    - A value with a time uses that time. An explicit offset is honoured; a time without
      an offset is interpreted in `tz`.

    `due_date` stays "as entered": for a value with a time it is that date in `tz`.
    """
    raw = start.strip()
    if _DATE_ONLY_RE.match(raw):
        due_date = date.fromisoformat(raw)
        return due_date, compute_due_at(due_date, tz, default_time_of_day), False

    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz, fold=0)
    due_at = parsed.astimezone(UTC)
    due_date = parsed.astimezone(tz).date()
    return due_date, due_at, True
