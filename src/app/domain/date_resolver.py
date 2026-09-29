"""The date resolver (spec §2.3.4, "Date resolver (deterministic, grammar-first)").

The model is allowed to hand back only the phrase the owner actually wrote — "Friday",
"Oct 3" — and this module turns that phrase into an absolute calendar date. No LLM is
involved here, and that is the whole point: §2.3.4 and the intent schema both forbid the
model from doing date arithmetic, so the arithmetic lives in a pure function that can be
tested exhaustively.

The bias is deliberately asymmetric. The spec's table has a closed Resolved column and an
open Ambiguous one, so **under-matching is safe and over-matching is not**: an unrecognised
phrase costs the owner one clarification email, while a phrase matched too eagerly moves a
real deadline. Every rule below therefore anchors to a full-string match, and whatever is
left over becomes `Ambiguous` rather than a guess. The `reason` strings are written to be
shown to the owner verbatim — they end up in a clarification email.

Two rules are easy to get wrong and are pinned by tests:

* A bare weekday means the **next** occurrence *strictly after* today, so on a Friday,
  "Friday" is a week away — not today.
* "Today" and "tomorrow" are relative to `received_at` in `tz`, not in UTC. At 22:00
  Eastern it is already tomorrow in UTC, and the owner means their own calendar.

Pure: no I/O, no config, no clock. The reference instant arrives as `received_at` and the
zone arrives as a `ZoneInfo`. Nothing here reads `Settings` — which is also what lets the
tests prove the zone is honoured rather than assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# ── Result types ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Resolved:
    """A phrase with exactly one sensible reading.

    `date` is date-only on purpose: no time is ever attached here. The 11:59 PM rule is
    `due_time.compute_due_at`'s job, applied later to whatever date comes out of here.
    """

    date: date


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """A phrase that cannot be turned into one date — or must not be.

    `reason` is owner-facing prose. The caller passes it straight into a clarification
    email, so it is written as a complete sentence that asks for the exact date.
    """

    reason: str


Resolution = Resolved | Ambiguous


# ── Vocabularies ────────────────────────────────────────────────────────────

# `date.weekday()` numbering: Monday is 0.
_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_MONTHS: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Vague phrases from the spec's Ambiguous row, plus the handful of near-neighbours that
# would otherwise slip into "I couldn't parse that". Each maps to the reason *why* it is
# not a date, so the clarification email can explain itself.
_VAGUE: dict[str, str] = {
    "this weekend": "a weekend covers two days",
    "next weekend": "a weekend covers two days",
    "end of the week": "the end of the week is not one date",
    "end of week": "the end of the week is not one date",
    "in a few days": '"a few days" is not an exact number of days',
    "in a couple of days": '"a couple of days" is not an exact number of days',
    "later": '"later" is not a date',
    "later on": '"later" is not a date',
    "push it back": "I don't know how far back you want to push it",
    "push it back a bit": "I don't know how far back you want to push it",
    "next week": "a week is seven days, not one date",
    "next month": "a month is not one date",
    "soon": '"soon" is not a date',
    "asap": '"asap" is not a date',
}

# "next Friday", "this Friday", "last Friday" — all of them need a follow-up question,
# because the offset the owner has in mind ("the Friday of next week"?) is unknowable.
_MODIFIER_PREFIXES = ("next", "this", "last", "coming", "following", "the coming")

_WEEKDAY_WORDS = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MODIFIED_WEEKDAY_RE = re.compile(rf"^(?:{'|'.join(_MODIFIER_PREFIXES)})\s+(?:{_WEEKDAY_WORDS})$")

# "Oct 3", "October 3rd", "Oct. 3", "October 3, 2027", "Oct 3 2027".
_MONTH_DAY_RE = re.compile(
    r"^(?P<month>[a-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<year>\d{4}))?$"
)

# "10/3", "10/03/2027". Month first, US style — the spec's own example.
_NUMERIC_MONTH_DAY_RE = re.compile(r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{4}))?$")

# How far ahead a yearless month/day is allowed to search for a real date. Four years
# covers a leap day ("Feb 29" said in a non-leap year), doubled for margin.
_YEAR_SEARCH_LIMIT = 8


# ── Owner-facing messages ───────────────────────────────────────────────────


def _human(d: date) -> str:
    """`October 3, 2027`. Built from a literal table so it never depends on locale."""
    return f"{_MONTH_NAMES[d.month - 1]} {d.day}, {d.year}"


def _ask_for_exact_date(display: str) -> str:
    return (
        f'I couldn\'t turn "{display}" into an exact date. '
        'Reply with the exact date you want, for example "Oct 3".'
    )


_EMPTY_REASON = (
    "I couldn't find a date in your reply. "
    'Reply with the exact date you want, for example "Oct 3".'
)


# ── Parsing ─────────────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, and drop wrapping quotes and trailing punctuation."""
    cleaned = " ".join(text.split()).strip().strip("\"'").strip()
    return cleaned.rstrip(".,;:!?").strip().lower()


def _vague_reason(normalized: str, display: str) -> str | None:
    """A targeted reason for a phrase that is known to be un-resolvable, or `None`."""
    detail = _VAGUE.get(normalized)
    if detail is not None:
        return (
            f'"{display}" is ambiguous — {detail}. '
            'Reply with the exact date you want, for example "Oct 3".'
        )
    if _MODIFIED_WEEKDAY_RE.match(normalized):
        return (
            f'"{display}" is ambiguous — I can only resolve a bare weekday. '
            'Reply with the exact date you want, for example "Oct 10".'
        )
    return None


def _next_weekday(today: date, weekday: int) -> date:
    """The next occurrence of `weekday` **strictly after** `today` (spec §2.3.4).

    On a Friday, "Friday" is seven days away. This is the single easiest thing to get
    wrong, so it is written out rather than folded into the arithmetic.
    """
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _month_day(month: int, day: int, year_text: str | None, today: date) -> date | None:
    """A validated calendar date, or `None` if no such date exists.

    With an explicit year the date is taken at face value (a past year is rejected by the
    caller's past-date check, exactly like any other past date). With no year, this is
    "the next such date": the first occurrence that is today or later. Today counts —
    §2.3.4 spells out "strictly after" for bare weekdays only, and a deadline named for
    today is still ahead of the owner until 11:59 PM.
    """
    if year_text is not None:
        try:
            return date(int(year_text), month, day)
        except ValueError:
            return None

    for offset in range(_YEAR_SEARCH_LIMIT):
        try:
            candidate = date(today.year + offset, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _parse(normalized: str, today: date) -> date | None:
    """The grammar. Returns a date, or `None` for "not a phrase I recognise"."""
    if normalized == "today":
        return today
    if normalized == "tomorrow":
        return today + timedelta(days=1)

    weekday = _WEEKDAYS.get(normalized)
    if weekday is not None:
        return _next_weekday(today, weekday)

    match = _MONTH_DAY_RE.match(normalized)
    if match is not None:
        month = _MONTHS.get(match.group("month"))
        if month is None:
            return None
        return _month_day(month, int(match.group("day")), match.group("year"), today)

    match = _NUMERIC_MONTH_DAY_RE.match(normalized)
    if match is not None:
        return _month_day(
            int(match.group("month")), int(match.group("day")), match.group("year"), today
        )

    return None


# ── Entry point ─────────────────────────────────────────────────────────────


def resolve_date(
    text: str,
    received_at: datetime,
    tz: ZoneInfo,
    *,
    current_due: date | None = None,
    max_shift_days: int | None = None,
) -> Resolution:
    """Turn the owner's date phrase into an absolute date (spec §2.3.4).

    `received_at` must be timezone-aware; it is the reply's own timestamp, and "today"
    means today in `tz`, not in UTC.

    `current_due` and `max_shift_days` are the guardrail on a surprising jump: when both
    are given, a resolved date further than `max_shift_days` from the item's current due
    date is refused with a question rather than applied. Either one alone is ignored, so
    the guard is opt-in from the caller.
    """
    if received_at.tzinfo is None:
        raise ValueError("received_at must be timezone-aware; every instant is timezone-aware")

    display = " ".join(text.split())
    normalized = _normalize(text)
    if not normalized:
        return Ambiguous(reason=_EMPTY_REASON)

    today = received_at.astimezone(tz).date()

    vague = _vague_reason(normalized, display)
    if vague is not None:
        return Ambiguous(reason=vague)

    candidate = _parse(normalized, today)
    if candidate is None:
        return Ambiguous(reason=_ask_for_exact_date(display))

    if candidate < today:
        return Ambiguous(
            reason=(
                f'"{display}" is {_human(candidate)}, which is already in the past. '
                'Reply with the exact date you want, for example "Oct 3".'
            )
        )

    if current_due is not None and max_shift_days is not None:
        shift = abs((candidate - current_due).days)
        if shift > max_shift_days:
            return Ambiguous(
                reason=(
                    f'"{display}" is {_human(candidate)}, more than {max_shift_days} days '
                    f"from the current due date ({_human(current_due)}). That's a big jump — "
                    "please confirm the exact date you want."
                )
            )

    return Resolved(date=candidate)
