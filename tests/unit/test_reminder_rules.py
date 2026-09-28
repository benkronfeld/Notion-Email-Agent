"""FR-2: which reminders an item kind gets, and that offsets are absolute durations."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.due_time import compute_due_at
from app.domain.reminder_rules import RULES, offsets_for, relative_phrase, targets

NYC = ZoneInfo("America/New_York")


class TestRules:
    def test_assignment_reading_gets_48h_and_24h(self) -> None:
        assert dict(offsets_for("assignment_reading")) == {
            "assignment_48h": timedelta(hours=48),
            "assignment_24h": timedelta(hours=24),
        }

    def test_exam_project_gets_120h_and_48h(self) -> None:
        assert dict(offsets_for("exam_project")) == {
            "exam_120h": timedelta(hours=120),
            "exam_48h": timedelta(hours=48),
        }

    def test_every_kind_is_covered(self) -> None:
        assert set(RULES) == {"assignment_reading", "exam_project"}


class TestTargets:
    def test_assignment_targets(self) -> None:
        due = compute_due_at(date(2026, 10, 3), NYC)
        result = targets("assignment_reading", due)
        assert [(t.reminder_type, t.target_at) for t in result] == [
            ("assignment_48h", datetime(2026, 10, 2, 3, 59, tzinfo=UTC)),
            ("assignment_24h", datetime(2026, 10, 3, 3, 59, tzinfo=UTC)),
        ]

    def test_exam_targets_explicit(self) -> None:
        # Due 2026-10-04 03:59 UTC; 120h before is 2026-09-29 03:59 UTC, 48h is 2026-10-02.
        due = compute_due_at(date(2026, 10, 3), NYC)
        result = targets("exam_project", due)
        assert [(t.reminder_type, t.target_at) for t in result] == [
            ("exam_120h", datetime(2026, 9, 29, 3, 59, tzinfo=UTC)),
            ("exam_48h", datetime(2026, 10, 2, 3, 59, tzinfo=UTC)),
        ]

    def test_offsets_are_absolute_durations_across_dst(self) -> None:
        # FR-2 accepts that a 48h offset lands at a different *local* wall-clock time
        # across a DST change: 48 real hours before a Nov 2 EST due date is 00:59 EDT,
        # not 23:59. Asserted explicitly so nobody "fixes" it into calendar arithmetic.
        due = compute_due_at(date(2026, 11, 2), NYC)  # 2026-11-03 04:59 UTC
        found = targets("assignment_reading", due)
        target_48h = next(t for t in found if t.reminder_type == "assignment_48h")
        assert target_48h.target_at == due - timedelta(hours=48)
        assert target_48h.target_at.astimezone(NYC).strftime("%H:%M") == "00:59"

    def test_targets_are_aware_utc(self) -> None:
        due = compute_due_at(date(2026, 10, 3), NYC)
        for target in targets("exam_project", due):
            assert target.target_at.tzinfo is not None
            assert target.target_at.utcoffset() == timedelta(0)

    def test_earliest_target_is_first(self) -> None:
        due = compute_due_at(date(2026, 10, 3), NYC)
        result = targets("exam_project", due)
        assert [t.target_at for t in result] == sorted(t.target_at for t in result)

    def test_rejects_naive_due_at(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            targets("assignment_reading", datetime(2026, 10, 3, 23, 59))


class TestRelativePhrase:
    @pytest.mark.parametrize(
        ("reminder_type", "expected"),
        [
            ("assignment_48h", "in 48 hours"),
            ("assignment_24h", "in 24 hours"),
            ("exam_120h", "in 5 days"),
            ("exam_48h", "in 48 hours"),
        ],
    )
    def test_phrases(self, reminder_type: str, expected: str) -> None:
        assert relative_phrase(reminder_type) == expected  # type: ignore[arg-type]

    def test_every_reminder_type_has_a_phrase(self) -> None:
        for _, pairs in RULES.items():
            for reminder_type, _offset in pairs:
                assert relative_phrase(reminder_type)


def test_default_due_time_from_config_shape() -> None:
    # Guard the coupling between the schema default (23:59) and a plain date.
    due = compute_due_at(date(2026, 10, 3), NYC, time(23, 59))
    assert due.astimezone(NYC).strftime("%H:%M") == "23:59"
