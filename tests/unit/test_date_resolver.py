"""The date resolver table and its edges (spec §2.3.4).

Every row of the spec's table has its own assertion, plus the cases the table implies but
does not spell out: the strictly-after weekday rule, "today" across a timezone boundary
where UTC and Eastern disagree on the date, past dates, the `MAX_DATE_SHIFT_DAYS` boundary,
and input that matches nothing.

Eastern is pinned deliberately — it is the deployment zone and the spec's worked examples
are in it. Production code never hardcodes the zone; it arrives as a `ZoneInfo` argument,
which `test_the_zone_is_a_parameter_not_a_constant` proves.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.date_resolver import Ambiguous, Resolution, Resolved, resolve_date

NYC = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")

# Tuesday 29 September 2026, 2 PM Eastern — a fixed reference instant, never the real clock.
REFERENCE = datetime(2026, 9, 29, 14, 0, tzinfo=NYC)
TODAY = date(2026, 9, 29)


def resolve(
    text: str,
    *,
    received_at: datetime = REFERENCE,
    tz: ZoneInfo = NYC,
    current_due: date | None = None,
    max_shift_days: int | None = None,
) -> Resolution:
    return resolve_date(
        text, received_at, tz, current_due=current_due, max_shift_days=max_shift_days
    )


def resolved_date(
    text: str,
    *,
    received_at: datetime = REFERENCE,
    tz: ZoneInfo = NYC,
    current_due: date | None = None,
    max_shift_days: int | None = None,
) -> date:
    result = resolve(
        text,
        received_at=received_at,
        tz=tz,
        current_due=current_due,
        max_shift_days=max_shift_days,
    )
    assert isinstance(result, Resolved), f"{text!r} -> {result!r}"
    return result.date


def ambiguous_reason(
    text: str,
    *,
    received_at: datetime = REFERENCE,
    tz: ZoneInfo = NYC,
    current_due: date | None = None,
    max_shift_days: int | None = None,
) -> str:
    result = resolve(
        text,
        received_at=received_at,
        tz=tz,
        current_due=current_due,
        max_shift_days=max_shift_days,
    )
    assert isinstance(result, Ambiguous), f"{text!r} -> {result!r}"
    return result.reason


class TestSpecTable:
    """The §2.3.4 table, one row per assertion."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("today", date(2026, 9, 29)),
            ("tomorrow", date(2026, 9, 30)),
            ("Friday", date(2026, 10, 2)),
            ("Oct 3", date(2026, 10, 3)),
            ("10/3", date(2026, 10, 3)),
            ("October 3rd", date(2026, 10, 3)),
        ],
    )
    def test_resolved_rows(self, text: str, expected: date) -> None:
        assert resolved_date(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "next Friday",
            "this weekend",
            "end of the week",
            "in a few days",
            "later",
            "push it back",
        ],
    )
    def test_ambiguous_rows(self, text: str) -> None:
        reason = ambiguous_reason(text)
        assert text.lower() in reason.lower()
        assert "exact date" in reason

    def test_next_friday_stays_a_question(self) -> None:
        # "next Friday" is never guessed at as this coming Friday.
        assert isinstance(resolve("next Friday"), Ambiguous)
        assert isinstance(resolve("Friday"), Resolved)


class TestBareWeekdayStrictlyAfter:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Monday", date(2026, 10, 5)),
            ("Tuesday", date(2026, 10, 6)),
            ("Wednesday", date(2026, 9, 30)),
            ("Thursday", date(2026, 10, 1)),
            ("Friday", date(2026, 10, 2)),
            ("Saturday", date(2026, 10, 3)),
            ("Sunday", date(2026, 10, 4)),
        ],
    )
    def test_each_weekday(self, text: str, expected: date) -> None:
        assert resolved_date(text) == expected

    def test_today_is_that_weekday_so_it_means_the_following_one(self) -> None:
        # The reference instant is a Tuesday. "Tuesday" must be NEXT Tuesday, not today.
        assert TODAY.weekday() == 1
        assert resolved_date("Tuesday") == date(2026, 10, 6)
        assert resolved_date("Tuesday") != TODAY

    def test_abbreviations_and_case_are_accepted(self) -> None:
        assert resolved_date("fri") == date(2026, 10, 2)
        assert resolved_date("FRIDAY") == date(2026, 10, 2)
        assert resolved_date("  friday. ") == date(2026, 10, 2)

    def test_a_modified_weekday_is_never_a_bare_weekday(self) -> None:
        for text in ("next Friday", "this Friday", "last Friday", "coming Friday"):
            assert isinstance(resolve(text), Ambiguous), text

    def test_a_bare_weekday_is_always_within_seven_days(self) -> None:
        for offset in range(7):
            reference = REFERENCE + timedelta(days=offset)
            result = resolve("Friday", received_at=reference)
            assert isinstance(result, Resolved)
            assert 1 <= (result.date - reference.astimezone(NYC).date()).days <= 7


class TestTodayAndTomorrowUseTheLocalZone:
    # 2026-10-04T02:00Z is still 2026-10-03 22:00 in Eastern. UTC and Eastern disagree on
    # which day it is, so a naive `received_at.date()` would be wrong for the owner.
    RECEIVED = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)

    def test_in_eastern_today_is_the_third(self) -> None:
        assert resolved_date("today", received_at=self.RECEIVED, tz=NYC) == date(2026, 10, 3)
        assert resolved_date("tomorrow", received_at=self.RECEIVED, tz=NYC) == date(2026, 10, 4)

    def test_in_utc_today_is_the_fourth(self) -> None:
        assert resolved_date("today", received_at=self.RECEIVED, tz=UTC_ZONE) == date(2026, 10, 4)

    def test_the_two_zones_disagree(self) -> None:
        eastern = resolved_date("today", received_at=self.RECEIVED, tz=NYC)
        utc = resolved_date("today", received_at=self.RECEIVED, tz=UTC_ZONE)
        assert eastern != utc

    def test_a_zone_behind_utc_moves_the_other_way(self) -> None:
        # 2026-10-04T05:00Z is 2026-10-03 22:00 in Los Angeles.
        received = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
        assert resolved_date(
            "today", received_at=received, tz=ZoneInfo("America/Los_Angeles")
        ) == date(2026, 10, 3)


class TestMonthAndDay:
    def test_this_years_occurrence_when_it_is_still_ahead(self) -> None:
        assert resolved_date("Oct 3") == date(2026, 10, 3)
        assert resolved_date("December 25") == date(2026, 12, 25)

    def test_rolls_to_next_year_when_this_years_occurrence_has_passed(self) -> None:
        assert resolved_date("Sep 1") == date(2027, 9, 1)
        assert resolved_date("January 1") == date(2027, 1, 1)

    def test_today_itself_counts(self) -> None:
        # §2.3.4 says "strictly after" for bare weekdays only; a month/day names a real day,
        # and a deadline today is still ahead of the owner until 11:59 PM.
        assert resolved_date("Sep 29") == TODAY
        assert resolved_date("9/29") == TODAY

    def test_ordinals_are_accepted_in_both_forms(self) -> None:
        assert resolved_date("October 3rd") == date(2026, 10, 3)
        assert resolved_date("October 3th") == date(2026, 10, 3)
        assert resolved_date("Oct 3rd") == date(2026, 10, 3)
        assert resolved_date("Oct 3") == date(2026, 10, 3)

    def test_abbreviated_month_with_a_period(self) -> None:
        assert resolved_date("Oct. 3") == date(2026, 10, 3)

    def test_month_names_are_case_insensitive(self) -> None:
        assert resolved_date("OCTOBER 3") == date(2026, 10, 3)
        assert resolved_date("october 3") == date(2026, 10, 3)

    def test_an_explicit_year_is_honoured(self) -> None:
        # The whole trap: "Oct 3, 2027" must not collapse back to 2026.
        assert resolved_date("October 3, 2027") == date(2027, 10, 3)
        assert resolved_date("Oct 3 2027") == date(2027, 10, 3)
        assert resolved_date("10/3/2027") == date(2027, 10, 3)

    def test_a_leap_day_is_found_in_the_next_leap_year(self) -> None:
        assert resolved_date("Feb 29") == date(2028, 2, 29)

    @pytest.mark.parametrize("text", ["Feb 30", "13/1", "0/3", "April 31", "Feb 29, 2027"])
    def test_impossible_dates_are_ambiguous(self, text: str) -> None:
        assert isinstance(resolve(text), Ambiguous)


class TestNumericDates:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("10/3", date(2026, 10, 3)),
            ("10/03", date(2026, 10, 3)),
            ("12/25", date(2026, 12, 25)),
            ("1/1", date(2027, 1, 1)),
            ("9/30", date(2026, 9, 30)),
        ],
    )
    def test_month_first(self, text: str, expected: date) -> None:
        assert resolved_date(text) == expected

    def test_two_digit_years_are_not_guessed(self) -> None:
        # "10/3/27" could be 2027 or 1927. Ask instead of picking one.
        assert isinstance(resolve("10/3/27"), Ambiguous)


class TestPastDates:
    def test_an_explicit_past_year_is_rejected(self) -> None:
        reason = ambiguous_reason("October 3, 2020")
        assert "past" in reason
        assert "2020" in reason

    def test_a_past_year_with_slashes_is_rejected(self) -> None:
        assert "past" in ambiguous_reason("10/3/2020")

    def test_a_yearless_phrase_never_resolves_into_the_past(self) -> None:
        result = resolve("Sep 1")
        assert isinstance(result, Resolved)
        assert result.date > TODAY

    def test_yesterday_is_not_in_the_grammar(self) -> None:
        assert isinstance(resolve("yesterday"), Ambiguous)


class TestMaxShiftDays:
    # 2026-09-01 -> 2026-10-31 is exactly 60 days; -> 2026-11-01 is 61.
    CURRENT_DUE = date(2026, 9, 1)

    def test_exactly_at_the_limit_is_allowed(self) -> None:
        assert resolved_date("Oct 31", current_due=self.CURRENT_DUE, max_shift_days=60) == (
            date(2026, 10, 31)
        )

    def test_one_day_past_the_limit_is_refused(self) -> None:
        reason = ambiguous_reason("Nov 1", current_due=self.CURRENT_DUE, max_shift_days=60)
        assert "big jump" in reason
        assert "confirm the exact date" in reason
        assert "60" in reason

    def test_the_guard_is_symmetric(self) -> None:
        # A jump backwards is just as suspicious.
        reason = ambiguous_reason("Oct 1", current_due=date(2026, 12, 1), max_shift_days=60)
        assert "big jump" in reason

    def test_no_guard_without_max_shift_days(self) -> None:
        assert resolved_date("Oct 31", current_due=self.CURRENT_DUE) == date(2026, 10, 31)

    def test_no_guard_without_a_current_due_date(self) -> None:
        assert resolved_date("Oct 31", max_shift_days=60) == date(2026, 10, 31)

    def test_a_small_shift_passes(self) -> None:
        assert resolved_date("Oct 3", current_due=self.CURRENT_DUE, max_shift_days=60) == (
            date(2026, 10, 3)
        )

    def test_a_past_date_is_rejected_before_the_jump_check(self) -> None:
        reason = ambiguous_reason(
            "October 3, 2020", current_due=self.CURRENT_DUE, max_shift_days=60
        )
        assert "past" in reason

    def test_the_jump_reason_names_both_dates(self) -> None:
        reason = ambiguous_reason("Nov 1", current_due=self.CURRENT_DUE, max_shift_days=60)
        assert "November 1, 2026" in reason
        assert "September 1, 2026" in reason


class TestUnrecognisedInput:
    @pytest.mark.parametrize("text", ["", "   ", "\t\n", '"  "', "  .  "])
    def test_empty_input_asks_for_a_date(self, text: str) -> None:
        assert "couldn't find a date" in ambiguous_reason(text)

    @pytest.mark.parametrize(
        "text",
        [
            "sometime soon",
            "the 3rd",
            "Friday Oct 3",
            "3/4/5",
            "someday",
            "10-3",
            "2026-10-03",
            "in 3 days",
            "two weeks from now",
            "before Thanksgiving",
        ],
    )
    def test_nothing_else_is_matched(self, text: str) -> None:
        assert isinstance(resolve(text), Ambiguous)

    def test_the_reason_quotes_what_the_owner_said(self) -> None:
        assert "sometime soon" in ambiguous_reason("sometime soon")


class TestContract:
    def test_naive_received_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            resolve_date("Friday", datetime(2026, 9, 29, 14, 0), NYC)

    def test_the_zone_is_a_parameter_not_a_constant(self) -> None:
        # 2026-10-04T02:00Z: Oct 4 in UTC, still Oct 3 in Eastern. A hardcoded zone could
        # never produce both answers.
        received = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
        assert resolved_date("today", received_at=received, tz=NYC) == date(2026, 10, 3)
        assert resolved_date("today", received_at=received, tz=UTC_ZONE) == date(2026, 10, 4)

    def test_a_resolved_date_carries_no_time(self) -> None:
        result = resolve("Oct 3")
        assert isinstance(result, Resolved)
        assert type(result.date) is date

    def test_resolved_is_frozen_and_slotted(self) -> None:
        result = Resolved(date=date(2026, 10, 3))
        with pytest.raises(FrozenInstanceError):
            result.date = date(2027, 1, 1)  # type: ignore[misc]
        assert not hasattr(result, "__dict__")
