"""`plan_reminders` — the crux (spec §2.3.2.B).

Every case from Appendix B that touches reminder state, plus the idempotence property
the whole design rests on.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.domain import planner as planner_module
from app.domain.planner import (
    FlipToPending,
    InsertReminder,
    MarkSkipped,
    MarkSuperseded,
    PlannerPolicy,
    ReminderAction,
    idempotency_key,
    plan_reminders,
)
from app.domain.types import (
    ItemKind,
    PlannerItem,
    PlannerReminder,
    ReminderStatus,
    ReminderType,
    SkipReason,
)

POLICY = PlannerPolicy(completed_status_value="Completed")
PAGE_ID = "page-abc"

ITEM_ID = UUID("00000000-0000-0000-0000-000000000001")
R48 = UUID("00000000-0000-0000-0000-0000000000a1")
R24 = UUID("00000000-0000-0000-0000-0000000000a2")

# Due 2026-10-03 23:59 Eastern (EDT) = 2026-10-04 03:59 UTC.
DUE = datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
T48 = DUE - timedelta(hours=48)  # 2026-10-02 03:59 UTC
T24 = DUE - timedelta(hours=24)  # 2026-10-03 03:59 UTC
T120 = DUE - timedelta(hours=120)  # 2026-09-29 03:59 UTC

# Comfortably before every target, so "future" is unambiguous.
EARLY = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def make_item(
    *,
    due_at: datetime | None = DUE,
    status: str = "Not started",
    done: bool = False,
    is_active: bool = True,
    kind: ItemKind = "assignment_reading",
) -> PlannerItem:
    return PlannerItem(
        item_id=ITEM_ID,
        notion_page_id=PAGE_ID,
        item_kind=kind,
        status=status,
        done=done,
        due_at=due_at,
        is_active=is_active,
    )


def make_reminder(
    reminder_type: ReminderType,
    *,
    target_at: datetime,
    reminder_id: UUID = R48,
    due_at_snapshot: datetime = DUE,
    status: ReminderStatus = "pending",
    skip_reason: SkipReason | None = None,
) -> PlannerReminder:
    return PlannerReminder(
        reminder_id=reminder_id,
        reminder_type=reminder_type,
        due_at_snapshot=due_at_snapshot,
        target_at=target_at,
        status=status,
        skip_reason=skip_reason,
    )


def pending_pair() -> list[PlannerReminder]:
    """Both reminders for the due date, never sent."""
    return [
        make_reminder("assignment_48h", target_at=T48, reminder_id=R48),
        make_reminder("assignment_24h", target_at=T24, reminder_id=R24),
    ]


def skipped_pair(skip_reason: SkipReason) -> list[PlannerReminder]:
    """Both reminders for the due date, skipped for the same reason."""
    return [
        make_reminder(
            "assignment_48h",
            target_at=T48,
            reminder_id=R48,
            status="skipped",
            skip_reason=skip_reason,
        ),
        make_reminder(
            "assignment_24h",
            target_at=T24,
            reminder_id=R24,
            status="skipped",
            skip_reason=skip_reason,
        ),
    ]


def apply_actions(
    actions: list[ReminderAction], rows: list[PlannerReminder]
) -> list[PlannerReminder]:
    """A stand-in for `apply_plan`, so idempotence can be tested for real.

    Persistence does this in SQL; here it is done in memory so the planner can actually
    be shown to settle after one pass rather than having that asserted by hand.
    """
    by_id = {row.reminder_id: row for row in rows}
    fresh = 0
    for action in actions:
        if isinstance(action, MarkSuperseded):
            by_id[action.reminder_id] = _replace(
                by_id[action.reminder_id], status="superseded", skip_reason=None
            )
        elif isinstance(action, MarkSkipped):
            by_id[action.reminder_id] = _replace(
                by_id[action.reminder_id], status="skipped", skip_reason=action.skip_reason
            )
        elif isinstance(action, FlipToPending):
            by_id[action.reminder_id] = _replace(
                by_id[action.reminder_id], status="pending", skip_reason=None
            )
        else:
            fresh += 1
            new_id = UUID(int=9000 + fresh)
            by_id[new_id] = PlannerReminder(
                reminder_id=new_id,
                reminder_type=action.reminder_type,
                due_at_snapshot=action.due_at_snapshot,
                target_at=action.target_at,
                status=action.status,
                skip_reason=action.skip_reason,
            )
    return list(by_id.values())


def _replace(
    row: PlannerReminder,
    *,
    status: ReminderStatus,
    skip_reason: SkipReason | None,
) -> PlannerReminder:
    return PlannerReminder(
        reminder_id=row.reminder_id,
        reminder_type=row.reminder_type,
        due_at_snapshot=row.due_at_snapshot,
        target_at=row.target_at,
        status=status,
        skip_reason=skip_reason,
    )


class TestFreshItem:
    def test_new_item_before_every_target_inserts_two_pending(self) -> None:
        actions = plan_reminders(make_item(), [], EARLY, POLICY)
        assert actions == [
            InsertReminder(
                reminder_type="assignment_48h",
                due_at_snapshot=DUE,
                target_at=T48,
                status="pending",
                skip_reason=None,
                idempotency_key=f"{PAGE_ID}:assignment_48h:2026-10-04T03:59:00Z",
            ),
            InsertReminder(
                reminder_type="assignment_24h",
                due_at_snapshot=DUE,
                target_at=T24,
                status="pending",
                skip_reason=None,
                idempotency_key=f"{PAGE_ID}:assignment_24h:2026-10-04T03:59:00Z",
            ),
        ]

    def test_exam_project_gets_its_own_schedule(self) -> None:
        actions = plan_reminders(make_item(kind="exam_project"), [], EARLY, POLICY)
        inserted = [a for a in actions if isinstance(a, InsertReminder)]
        assert [a.reminder_type for a in inserted] == ["exam_120h", "exam_48h"]
        assert inserted[0].target_at == T120

    def test_item_with_no_due_date_gets_nothing(self) -> None:
        assert plan_reminders(make_item(due_at=None), [], EARLY, POLICY) == []


class TestMissedWindows:
    """FR-4 / Appendix B cases 1 and 2: never send retroactively."""

    def test_added_between_the_two_targets(self) -> None:
        now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)  # after T48, before T24
        actions = plan_reminders(make_item(), [], now, POLICY)
        inserted = [a for a in actions if isinstance(a, InsertReminder)]
        assert [(a.reminder_type, a.status, a.skip_reason) for a in inserted] == [
            ("assignment_48h", "skipped", "missed_window"),
            ("assignment_24h", "pending", None),
        ]

    def test_added_after_every_target(self) -> None:
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        actions = plan_reminders(make_item(), [], now, POLICY)
        inserted = [a for a in actions if isinstance(a, InsertReminder)]
        assert [(a.status, a.skip_reason) for a in inserted] == [
            ("skipped", "missed_window"),
            ("skipped", "missed_window"),
        ]

    def test_a_target_exactly_now_is_missed_not_sent(self) -> None:
        # The boundary is `target_at > now`, so a target equal to now is already gone.
        actions = plan_reminders(make_item(), [], T48, POLICY)
        first = actions[0]
        assert isinstance(first, InsertReminder)
        assert (first.status, first.skip_reason) == ("skipped", "missed_window")


class TestCompletionAndActivity:
    """FR-3 / FR-9: completed and archived items send nothing."""

    def test_completed_item_skips_pending(self) -> None:
        # `skipped`, not `superseded`: only a skipped row can be revived later.
        actions = plan_reminders(make_item(status="Completed"), pending_pair(), EARLY, POLICY)
        assert actions == [MarkSkipped(R48, "item_completed"), MarkSkipped(R24, "item_completed")]

    def test_done_true_with_status_not_started_is_complete(self) -> None:
        # Rows predating this system may have Done=true with Status stuck at Not started.
        item = make_item(status="Not started", done=True)
        actions = plan_reminders(item, pending_pair(), EARLY, POLICY)
        assert actions == [MarkSkipped(R48, "item_completed"), MarkSkipped(R24, "item_completed")]

    def test_status_completed_with_done_false_is_complete(self) -> None:
        item = make_item(status="Completed", done=False)
        assert item.status == POLICY.completed_status_value
        assert plan_reminders(item, [], EARLY, POLICY) == []

    def test_unknown_status_is_treated_as_active(self) -> None:
        item = make_item(status="Blocked", done=False)
        actions = plan_reminders(item, [], EARLY, POLICY)
        assert len([a for a in actions if isinstance(a, InsertReminder)]) == 2

    def test_inactive_item_skips_pending(self) -> None:
        actions = plan_reminders(make_item(is_active=False), pending_pair(), EARLY, POLICY)
        assert actions == [MarkSkipped(R48, "item_inactive"), MarkSkipped(R24, "item_inactive")]

    def test_inactive_wins_over_completed_as_the_reason(self) -> None:
        item = make_item(is_active=False, status="Completed")
        actions = plan_reminders(item, pending_pair(), EARLY, POLICY)
        assert actions == [MarkSkipped(R48, "item_inactive"), MarkSkipped(R24, "item_inactive")]

    def test_claimed_row_is_skipped_when_the_item_completes_mid_flight(self) -> None:
        existing = [
            make_reminder("assignment_48h", target_at=T48, reminder_id=R48, status="claimed")
        ]
        actions = plan_reminders(make_item(done=True), existing, EARLY, POLICY)
        assert actions == [MarkSkipped(R48, "item_completed")]


class TestDueDateChange:
    """FR-8 Option A: a new due date gets a fresh schedule; sent history is kept."""

    NEW_DUE = DUE + timedelta(days=7)

    def test_old_pending_is_superseded_and_a_fresh_schedule_inserted(self) -> None:
        existing = [make_reminder("assignment_24h", target_at=T24, reminder_id=R24)]
        actions = plan_reminders(make_item(due_at=self.NEW_DUE), existing, EARLY, POLICY)
        assert actions[0] == MarkSuperseded(R24)
        inserted = [a for a in actions if isinstance(a, InsertReminder)]
        assert [a.reminder_type for a in inserted] == ["assignment_48h", "assignment_24h"]
        assert {a.due_at_snapshot for a in inserted} == {self.NEW_DUE}

    def test_a_due_date_change_supersedes_rather_than_skips(self) -> None:
        # The distinction still matters: a skip is revived by the item changing (step 2's
        # symmetric transition), whereas a supersede is only revived by its own date coming
        # back. The two states must not be collapsed into one.
        existing = [make_reminder("assignment_24h", target_at=T24, reminder_id=R24)]
        actions = plan_reminders(make_item(due_at=self.NEW_DUE), existing, EARLY, POLICY)
        assert not any(isinstance(a, MarkSkipped) for a in actions)

    def test_an_already_sent_type_is_scheduled_again_for_the_new_date(self) -> None:
        # The 48h reminder went out for the old date; the new date still needs one.
        existing = [make_reminder("assignment_48h", target_at=T48, reminder_id=R48, status="sent")]
        actions = plan_reminders(make_item(due_at=self.NEW_DUE), existing, EARLY, POLICY)
        assert MarkSuperseded(R48) not in actions  # sent history is kept
        inserted = [a for a in actions if isinstance(a, InsertReminder)]
        assert "assignment_48h" in [a.reminder_type for a in inserted]

    def test_clearing_the_due_date_supersedes_everything_pending(self) -> None:
        actions = plan_reminders(make_item(due_at=None), pending_pair(), EARLY, POLICY)
        assert actions == [MarkSuperseded(R48), MarkSuperseded(R24)]


class TestResurrection:
    """The symmetric skip -> pending transition (§2.3.2.B step 2)."""

    def test_uncompleting_resurrects_a_still_future_reminder(self) -> None:
        actions = plan_reminders(make_item(), skipped_pair("item_completed"), EARLY, POLICY)
        assert actions == [FlipToPending(R48), FlipToPending(R24)]

    def test_unarchiving_resurrects_a_still_future_reminder(self) -> None:
        actions = plan_reminders(make_item(), skipped_pair("item_inactive"), EARLY, POLICY)
        assert actions == [FlipToPending(R48), FlipToPending(R24)]

    def test_only_the_reminder_matching_its_skip_reason_is_revived(self) -> None:
        # The 48h row was skipped for completion, the 24h row missed its window. Only the
        # first is revivable, so the item gets exactly one reminder back.
        existing = [
            make_reminder(
                "assignment_48h",
                target_at=T48,
                reminder_id=R48,
                status="skipped",
                skip_reason="item_completed",
            ),
            make_reminder(
                "assignment_24h",
                target_at=T24,
                reminder_id=R24,
                status="skipped",
                skip_reason="missed_window",
            ),
        ]
        actions = plan_reminders(make_item(), existing, EARLY, POLICY)
        assert actions == [FlipToPending(R48)]

    def test_no_resurrection_once_the_target_has_passed(self) -> None:
        now = T24 + timedelta(minutes=1)  # both targets are behind us
        actions = plan_reminders(make_item(), skipped_pair("item_completed"), now, POLICY)
        assert actions == []

    def test_no_resurrection_while_the_item_is_still_complete(self) -> None:
        item = make_item(status="Completed")
        actions = plan_reminders(item, skipped_pair("item_completed"), EARLY, POLICY)
        assert actions == []

    def test_no_resurrection_while_the_item_is_still_inactive(self) -> None:
        item = make_item(is_active=False)
        actions = plan_reminders(item, skipped_pair("item_inactive"), EARLY, POLICY)
        assert actions == []

    def test_a_missed_window_never_comes_back(self) -> None:
        actions = plan_reminders(make_item(), skipped_pair("missed_window"), EARLY, POLICY)
        assert actions == []

    def test_the_full_complete_then_uncomplete_round_trip(self) -> None:
        """The end-to-end symmetric transition, through really-applied actions.

        This is the case a `superseded`-instead-of-`skipped` bug would break: the rows
        have to come back from the dead, not merely look right in isolation.
        """
        rows = pending_pair()

        # 1. Completing the item skips both pending reminders.
        completed = make_item(status="Completed")
        first = plan_reminders(completed, rows, EARLY, POLICY)
        assert first == [MarkSkipped(R48, "item_completed"), MarkSkipped(R24, "item_completed")]
        rows = apply_actions(first, rows)
        assert {row.status for row in rows} == {"skipped"}

        # 2. A second pass changes nothing.
        assert plan_reminders(completed, rows, EARLY, POLICY) == []

        # 3. Un-completing revives both still-future reminders.
        revived = make_item(status="In progress")
        second = plan_reminders(revived, rows, EARLY, POLICY)
        assert second == [FlipToPending(R48), FlipToPending(R24)]
        rows = apply_actions(second, rows)
        assert {row.status for row in rows} == {"pending"}
        assert {row.skip_reason for row in rows} == {None}

        # 4. And it settles again.
        assert plan_reminders(revived, rows, EARLY, POLICY) == []


class TestRowsThatMustNotBeTouched:
    def test_sent_rows_are_never_touched(self) -> None:
        existing = [
            make_reminder("assignment_48h", target_at=T48, reminder_id=R48, status="sent"),
            make_reminder("assignment_24h", target_at=T24, reminder_id=R24),
        ]
        assert plan_reminders(make_item(), existing, EARLY, POLICY) == []

    def test_a_failed_row_is_not_in_flight(self) -> None:
        # `failed` is terminal for the planner; a reconcile does not revive it.
        existing = [
            make_reminder("assignment_48h", target_at=T48, reminder_id=R48, status="failed"),
            make_reminder("assignment_24h", target_at=T24, reminder_id=R24, status="failed"),
        ]
        assert plan_reminders(make_item(), existing, EARLY, POLICY) == []


class TestIdempotence:
    def test_a_second_pass_produces_nothing(self) -> None:
        item = make_item()
        rows = apply_actions(plan_reminders(item, [], EARLY, POLICY), [])
        assert len(rows) == 2
        assert plan_reminders(item, rows, EARLY, POLICY) == []

    def test_a_second_pass_produces_nothing_after_a_due_date_change(self) -> None:
        new_due = DUE + timedelta(days=7)
        existing = [make_reminder("assignment_24h", target_at=T24, reminder_id=R24)]
        item = make_item(due_at=new_due)
        rows = apply_actions(plan_reminders(item, existing, EARLY, POLICY), existing)
        assert plan_reminders(item, rows, EARLY, POLICY) == []

    def test_a_second_pass_produces_nothing_for_a_completed_item(self) -> None:
        item = make_item(done=True)
        rows = apply_actions(plan_reminders(item, pending_pair(), EARLY, POLICY), pending_pair())
        assert plan_reminders(item, rows, EARLY, POLICY) == []

    def test_planning_is_deterministic(self) -> None:
        item = make_item()
        assert plan_reminders(item, [], EARLY, POLICY) == plan_reminders(item, [], EARLY, POLICY)


class TestRevertedDueDate:
    """A due date that moves away and comes back keeps its reminders.

    §2.3.2.B step 2 revives a `skipped` row but says nothing about a `superseded` one, so
    the literal algorithm leaves the original rows holding the unique key
    `(item_id, reminder_type, due_at_snapshot)` and the restored date receives no reminder
    at all — a silently lost reminder, the exact outcome CLAUDE.md constraint 7 exists to
    prevent. The planner deliberately closes that gap: a `superseded` row is flipped back
    to `pending` when its own snapshot *is* the item's current due date, on exactly the
    same terms as a revived skip (target still in the future, item active and incomplete).
    FR-4 is not weakened by this — a target behind us is never revived.
    """

    def test_a_reverted_due_date_revives_the_superseded_rows(self) -> None:
        existing = [
            make_reminder("assignment_48h", target_at=T48, reminder_id=R48, status="superseded"),
            make_reminder("assignment_24h", target_at=T24, reminder_id=R24, status="superseded"),
        ]
        # The due date is back to DUE, which is exactly what these rows describe.
        assert plan_reminders(make_item(due_at=DUE), existing, EARLY, POLICY) == [
            FlipToPending(R48),
            FlipToPending(R24),
        ]

    def test_a_superseded_row_whose_target_has_passed_stays_dead(self) -> None:
        # The revival does not weaken FR-4: a window that is already gone is never sent.
        now = T24 + timedelta(minutes=1)
        existing = [
            make_reminder("assignment_48h", target_at=T48, reminder_id=R48, status="superseded"),
            make_reminder("assignment_24h", target_at=T24, reminder_id=R24, status="superseded"),
        ]
        assert plan_reminders(make_item(due_at=DUE), existing, now, POLICY) == []

    def test_a_superseded_row_for_a_different_date_is_left_alone(self) -> None:
        # Only a row under the *desired* date is a date that came back. This one describes a
        # date the item no longer has, so the restored date is scheduled fresh instead.
        existing = [
            make_reminder(
                "assignment_48h",
                target_at=T48,
                reminder_id=R48,
                status="superseded",
                due_at_snapshot=DUE + timedelta(days=7),
            )
        ]
        actions = plan_reminders(make_item(due_at=DUE), existing, EARLY, POLICY)
        assert not any(isinstance(a, FlipToPending) for a in actions)
        inserted = [a for a in actions if isinstance(a, InsertReminder)]
        assert [a.reminder_type for a in inserted] == ["assignment_48h", "assignment_24h"]

    def test_the_full_round_trip_revives_the_original_rows_without_duplicating_them(self) -> None:
        """date A -> date B -> date A, through really-applied actions.

        The claim is about the rows, not the plan: the restored date must end up with the
        same two rows it started with, `pending` (not superseded, and not a second copy).
        """
        rows = pending_pair()
        original_ids = {row.reminder_id for row in rows}
        new_due = DUE + timedelta(days=7)

        # 1. The date moves away. The old rows are superseded and a fresh schedule appears.
        first = plan_reminders(make_item(due_at=new_due), rows, EARLY, POLICY)
        assert [type(action) for action in first] == [
            MarkSuperseded,
            MarkSuperseded,
            InsertReminder,
            InsertReminder,
        ]
        rows = apply_actions(first, rows)
        assert len(rows) == 4
        assert {row.status for row in rows if row.due_at_snapshot == DUE} == {"superseded"}

        # 2. The date comes back. The original rows are revived, and — the whole point —
        #    nothing is inserted, because the unique key they hold is already correct.
        second = plan_reminders(make_item(due_at=DUE), rows, EARLY, POLICY)
        assert [a for a in second if isinstance(a, FlipToPending)] == [
            FlipToPending(R48),
            FlipToPending(R24),
        ]
        assert not any(isinstance(a, InsertReminder) for a in second)
        rows = apply_actions(second, rows)
        assert len(rows) == 4  # two rows per date, no duplicates

        restored = [row for row in rows if row.due_at_snapshot == DUE]
        assert {row.reminder_id for row in restored} == original_ids
        assert {row.status for row in restored} == {"pending"}
        assert {row.skip_reason for row in restored} == {None}

        # 3. And the round trip settles.
        assert plan_reminders(make_item(due_at=DUE), rows, EARLY, POLICY) == []


class TestIdempotencyKey:
    def test_exact_format(self) -> None:
        assert idempotency_key("abc", "assignment_48h", DUE) == (
            "abc:assignment_48h:2026-10-04T03:59:00Z"
        )

    def test_a_different_due_date_is_a_different_key(self) -> None:
        other = DUE + timedelta(days=1)
        assert idempotency_key("abc", "assignment_48h", DUE) != idempotency_key(
            "abc", "assignment_48h", other
        )

    def test_the_key_contains_no_naive_time(self) -> None:
        assert "+00:00" not in idempotency_key("abc", "assignment_48h", DUE)

    def test_a_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            idempotency_key("abc", "assignment_48h", datetime(2026, 10, 4, 3, 59))


class TestPurity:
    def test_plan_reminders_rejects_a_naive_now(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            plan_reminders(make_item(), [], datetime(2026, 10, 1), POLICY)

    def test_the_whole_domain_package_imports_no_io_libraries(self) -> None:
        # Mechanical enforcement of "domain/ is pure, no I/O".
        forbidden = {
            "sqlalchemy",
            "httpx",
            "google",
            "googleapiclient",
            "psycopg",
            "alembic",
            "fastapi",
        }
        domain_dir = Path(inspect.getfile(planner_module)).parent
        offenders: dict[str, set[str]] = {}
        for path in sorted(domain_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            bad = imported & forbidden
            if bad:
                offenders[path.name] = bad
        assert offenders == {}

    def test_the_planner_reads_no_configuration(self) -> None:
        source = inspect.getsource(planner_module)
        assert "app.config" not in source
        assert "Settings" not in source
