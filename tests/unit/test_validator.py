"""The validator's three outcomes, and the boundary it must not cross (spec §2.3.4).

The load-bearing assertion here is structural: `ApplyChange` carries `status` and
`due_date` and nothing else. `Done` is derived inside `NotionWriter`; if the validator ever
grew a `done` field the same decision would live in two places.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date

from app.domain.date_resolver import Ambiguous, Resolved
from app.domain.intents import ALLOWED_STATUSES, Intent, IntentAction, StatusValue
from app.domain.validator import (
    ApplyChange,
    NeedsClarification,
    NoChangeNeeded,
    ValidationOutcome,
    validate,
)

REFERENCE_DUE = date(2026, 10, 3)
RESOLVED = Resolved(date=date(2026, 10, 10))


def no_action() -> Intent:
    return Intent(action=IntentAction.NO_ACTION)


def change_status(status: StatusValue, *, unsure: str | None = None) -> Intent:
    return Intent(
        action=IntentAction.CHANGE_STATUS,
        status=status,
        needs_clarification=unsure is not None,
        clarification_question=unsure,
    )


def change_due(text: str) -> Intent:
    return Intent(action=IntentAction.CHANGE_DUE_DATE, due_date_text=text)


def change_both(status: StatusValue, text: str) -> Intent:
    return Intent(
        action=IntentAction.CHANGE_STATUS_AND_DUE_DATE,
        status=status,
        due_date_text=text,
    )


def ask(question: str) -> Intent:
    return Intent(
        action=IntentAction.ASK_CLARIFICATION,
        needs_clarification=True,
        clarification_question=question,
    )


def check(
    intent: Intent,
    *,
    current_status: str = "Not started",
    current_due_date: date | None = REFERENCE_DUE,
    resolved: Resolved | Ambiguous | None = None,
) -> ValidationOutcome:
    return validate(
        intent,
        current_status=current_status,
        current_due_date=current_due_date,
        resolved=resolved,
    )


class TestNoAction:
    def test_no_action_changes_nothing(self) -> None:
        # Nothing was asked for, so nothing changes; whether that silence deserves a reply
        # is the caller's call, not the validator's.
        assert check(no_action()) == NoChangeNeeded()


class TestAskClarification:
    def test_the_models_question_is_passed_through_verbatim(self) -> None:
        question = "Do you want me to mark it complete, or move the date?"
        result = check(ask(question))
        assert isinstance(result, NeedsClarification)
        assert result.question == question

    def test_the_uncertainty_flag_alone_asks(self) -> None:
        # If the model says it is unsure, the system asks rather than guessing (FR-10).
        result = check(change_status(StatusValue.COMPLETED, unsure="The essay or the reading?"))
        assert isinstance(result, NeedsClarification)
        assert result.question == "The essay or the reading?"


class TestStatusChanges:
    def test_a_new_status_is_applied(self) -> None:
        assert check(change_status(StatusValue.COMPLETED)) == ApplyChange(
            status="Completed", due_date=None
        )

    def test_the_same_status_needs_no_change(self) -> None:
        result = check(change_status(StatusValue.IN_PROGRESS), current_status="In progress")
        assert result == NoChangeNeeded()

    def test_the_applied_status_is_a_plain_string(self) -> None:
        result = check(change_status(StatusValue.IN_PROGRESS))
        assert isinstance(result, ApplyChange)
        assert type(result.status) is str

    def test_an_unrecognised_status_is_refused(self) -> None:
        # `model_construct` bypasses the intent schema's own validation, which is the only
        # way a bad status can arrive — and the reason the guard exists.
        broken = Intent.model_construct(
            action=IntentAction.CHANGE_STATUS,
            status="Done",  # type: ignore[arg-type]  # deliberately not a StatusValue
        )
        result = check(broken)
        assert isinstance(result, NeedsClarification)
        for allowed in ALLOWED_STATUSES:
            assert allowed in result.question

    def test_a_missing_status_is_refused(self) -> None:
        broken = Intent.model_construct(action=IntentAction.CHANGE_STATUS, status=None)
        assert isinstance(check(broken), NeedsClarification)

    def test_every_allowed_status_is_accepted(self) -> None:
        for status in StatusValue:
            result = check(change_status(status), current_status="Something else")
            assert isinstance(result, ApplyChange)
            assert result.status == status.value


class TestDueDateChanges:
    def test_a_resolved_date_is_applied(self) -> None:
        result = check(change_due("Oct 10"), resolved=RESOLVED)
        assert result == ApplyChange(status=None, due_date=date(2026, 10, 10))

    def test_the_same_date_needs_no_change(self) -> None:
        result = check(change_due("Oct 3"), resolved=Resolved(date=REFERENCE_DUE))
        assert result == NoChangeNeeded()

    def test_a_missing_resolution_asks(self) -> None:
        result = check(change_due("Friday"))
        assert isinstance(result, NeedsClarification)
        assert "date" in result.question

    def test_an_ambiguous_resolution_passes_the_reason_through(self) -> None:
        ambiguous = Ambiguous(reason='"next Friday" is ambiguous — give the exact date.')
        result = check(change_due("next Friday"), resolved=ambiguous)
        assert isinstance(result, NeedsClarification)
        assert result.question == ambiguous.reason

    def test_an_item_with_no_due_date_gets_one(self) -> None:
        result = check(change_due("Oct 10"), current_due_date=None, resolved=RESOLVED)
        assert result == ApplyChange(status=None, due_date=date(2026, 10, 10))


class TestCombinedChanges:
    def test_both_parts_change(self) -> None:
        result = check(
            change_both(StatusValue.COMPLETED, "Oct 10"),
            current_status="In progress",
            resolved=RESOLVED,
        )
        assert result == ApplyChange(status="Completed", due_date=date(2026, 10, 10))

    def test_only_the_status_differs(self) -> None:
        result = check(
            change_both(StatusValue.COMPLETED, "Oct 3"),
            current_status="Not started",
            resolved=Resolved(date=REFERENCE_DUE),
        )
        assert result == ApplyChange(status="Completed", due_date=None)

    def test_only_the_due_date_differs(self) -> None:
        result = check(
            change_both(StatusValue.IN_PROGRESS, "Oct 10"),
            current_status="In progress",
            resolved=RESOLVED,
        )
        assert result == ApplyChange(status=None, due_date=date(2026, 10, 10))

    def test_neither_part_differs(self) -> None:
        result = check(
            change_both(StatusValue.COMPLETED, "Oct 3"),
            current_status="Completed",
            resolved=Resolved(date=REFERENCE_DUE),
        )
        assert result == NoChangeNeeded()

    def test_an_ambiguous_date_stops_a_status_change_too(self) -> None:
        # Half a combined change is not a change: ask, write nothing.
        result = check(
            change_both(StatusValue.COMPLETED, "next Friday"),
            resolved=Ambiguous(reason="Ambiguous: which Friday?"),
        )
        assert isinstance(result, NeedsClarification)
        assert result.question == "Ambiguous: which Friday?"

    def test_an_invalid_status_wins_over_an_ambiguous_date(self) -> None:
        # Both parts are wrong; the status question is the one the owner can answer without
        # any date reasoning, so it is the one asked.
        broken = Intent.model_construct(
            action=IntentAction.CHANGE_STATUS_AND_DUE_DATE,
            status="Done",  # type: ignore[arg-type]  # deliberately not a StatusValue
            due_date_text="next Friday",
        )
        result = check(broken, resolved=Ambiguous(reason="Ambiguous: which Friday?"))
        assert isinstance(result, NeedsClarification)
        assert "Not started" in result.question

    def test_a_stray_date_phrase_is_ignored_by_a_status_only_action(self) -> None:
        # The action decides which parts are considered, so nothing extra reaches the write.
        result = check(
            Intent(
                action=IntentAction.CHANGE_STATUS,
                status=StatusValue.COMPLETED,
                due_date_text="Oct 10",
            )
        )
        assert result == ApplyChange(status="Completed", due_date=None)


class TestApplyChangeShape:
    def test_apply_change_has_exactly_two_fields(self) -> None:
        assert [f.name for f in fields(ApplyChange)] == ["status", "due_date"]

    def test_apply_change_has_no_done_attribute(self) -> None:
        # The validator has no opinion on Done; NotionWriter derives it from `status`
        # (spec §2.3.4, CLAUDE.md constraint 6).
        result = check(change_status(StatusValue.COMPLETED))
        assert isinstance(result, ApplyChange)
        assert not hasattr(result, "done")
        assert not hasattr(result, "checkbox")

    def test_apply_change_is_never_returned_with_nothing_to_change(self) -> None:
        cases: list[ValidationOutcome] = [
            check(change_status(StatusValue.COMPLETED)),
            check(change_status(StatusValue.COMPLETED), current_status="Completed"),
            check(change_due("Oct 10"), resolved=RESOLVED),
            check(no_action()),
        ]
        for outcome in cases:
            if isinstance(outcome, ApplyChange):
                assert outcome.status is not None or outcome.due_date is not None

    def test_the_three_outcomes_are_distinguishable(self) -> None:
        assert isinstance(check(no_action()), NoChangeNeeded)
        assert isinstance(check(ask("Which one?")), NeedsClarification)
        assert isinstance(check(change_status(StatusValue.COMPLETED)), ApplyChange)
