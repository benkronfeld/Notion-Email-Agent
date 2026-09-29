"""`plan_reminders` — the crux of the system (spec §2.3.2.B).

This is the pure half of `reconcile_reminders`: given an item, its existing reminder
rows, and the current time, it decides what every reminder row's state should be and
returns those decisions as a list of typed actions. Persistence (`apply_plan`) turns the
actions into SQL. The split is what makes §2.3.4's "pure functions, unit-tested" true —
this module touches no database, no clock, and no network.

It is safe to run any number of times: the unique constraint
`(item_id, reminder_type, due_at_snapshot)` plus the idempotency key mean a repeated run
produces no actions at all. Option A (a new due date gets a fresh schedule) falls out for
free, because a new due date is simply a new key.

The algorithm follows §2.3.2.B, with one deliberate addition to step 2 that the spec's
prose omits (and that §2.3.2.B's "known gap" note asked to be closed): a `superseded` row
also goes back to `pending` when its due date returns. Without it, moving a due date away
and then back leaves the original rows holding the unique key and the restored date with
no reminder at all — a silently lost reminder, the one outcome CLAUDE.md constraint 7
exists to prevent. Two states can therefore be revived, and both are pinned by tests: a
`skipped(item_completed|item_inactive)` row whose item became active and incomplete again,
and a `superseded` row whose own date came back. Everything a `sent` row records, and
every `missed_window`, stays exactly where it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.domain.reminder_rules import targets
from app.domain.types import (
    PlannerItem,
    PlannerReminder,
    ReminderType,
    SkipReason,
)

# Rows in these states are "in flight": not yet sent, so still cancellable.
IN_FLIGHT: frozenset[str] = frozenset({"pending", "claimed"})

# Resurrectable skip reasons. A row skipped because the item was complete or inactive is
# revived when the item becomes active and incomplete again (§2.3.2.B step 2). A
# `missed_window` skip is NOT revived — the window is gone for good (FR-4).
#
# These are reasons a *skipped* row can be revived for. A `superseded` row carries no
# reason at all (`skip_reason IS NULL`), so it cannot be expressed here; `_is_revivable`
# is where both cases are decided.
RESURRECTABLE_SKIPS: frozenset[str] = frozenset({"item_completed", "item_inactive"})


@dataclass(frozen=True, slots=True)
class PlannerPolicy:
    """The one piece of configuration the planner needs.

    Passed in rather than read from config so this module stays free of configuration
    and the caller decides what "completed" means for a given database.
    """

    completed_status_value: str


# ── Actions ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarkSuperseded:
    """An in-flight reminder whose due date moved. Replaced by a fresh schedule.

    Only ever a due-date change (FR-8, Option A). A `sent` row is never superseded and
    never revived, so sent history and old schedules stay distinct — but a superseded row
    whose date later comes back *is* revived by step 2, because otherwise the restored
    date would silently get no reminder at all.
    """

    reminder_id: UUID


@dataclass(frozen=True, slots=True)
class MarkSkipped:
    """An in-flight reminder that must not be sent, because the item is done or gone.

    Revivable by a later reconcile if the item becomes active and incomplete again and
    the target is still in the future.
    """

    reminder_id: UUID
    skip_reason: SkipReason


@dataclass(frozen=True, slots=True)
class FlipToPending:
    """A previously skipped or superseded reminder that applies again.

    Two ways in, and both are reversals of a state this planner itself wrote: un-completing
    or un-archiving the item reverses `skipped`, and a due date returning reverses
    `superseded`. See `_is_revivable`.
    """

    reminder_id: UUID


@dataclass(frozen=True, slots=True)
class InsertReminder:
    """A reminder row that should exist."""

    reminder_type: ReminderType
    due_at_snapshot: datetime
    target_at: datetime
    status: Literal["pending", "skipped"]
    skip_reason: SkipReason | None
    idempotency_key: str


ReminderAction = MarkSuperseded | MarkSkipped | FlipToPending | InsertReminder


def iso_utc(moment: datetime) -> str:
    """`2026-10-04T03:59:00Z`. Seconds precision, always UTC."""
    if moment.tzinfo is None:
        raise ValueError("cannot format a naive datetime; every instant is timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def idempotency_key(
    notion_page_id: str, reminder_type: ReminderType, due_at_snapshot: datetime
) -> str:
    """`{notion_page_id}:{reminder_type}:{due_at_snapshot ISO UTC}` (spec §2.3.5)."""
    return f"{notion_page_id}:{reminder_type}:{iso_utc(due_at_snapshot)}"


def plan_reminders(
    item: PlannerItem,
    existing: Sequence[PlannerReminder],
    now: datetime,
    policy: PlannerPolicy,
) -> list[ReminderAction]:
    """Decide the state of every reminder row for one item.

    `now` and every due instant must be timezone-aware. Returns the actions needed to
    bring the stored rows in line — an empty list means the rows are already correct,
    which is what makes a repeated reconcile silent.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    is_complete = item.done or item.status == policy.completed_status_value
    desired_due = item.due_at if (item.is_active and item.due_at is not None) else None
    desired = (
        targets(item.item_kind, desired_due)
        if (not is_complete and desired_due is not None)
        else []
    )

    actions: list[ReminderAction] = []

    # Step 1 — cancel anything still in flight that no longer applies.
    #
    # The three cases produce two DIFFERENT states, and the distinction is still worth
    # keeping even though step 2 can revive both:
    #   * item inactive / complete -> `skipped(reason)`, revivable when the item comes back
    #   * the due date moved       -> `superseded`, revivable when *this* date comes back
    # Collapsing them into `superseded` would silently break the symmetric transition:
    # un-completing an item could never bring a still-future reminder back, because the
    # flip would be gated on a due date that never moved.
    # A completed or deactivated item wins over a due-date change, so its reason is
    # reported as completion rather than as a moved date.
    for row in existing:
        if row.status not in IN_FLIGHT:
            continue
        if not item.is_active:
            actions.append(MarkSkipped(row.reminder_id, "item_inactive"))
        elif is_complete:
            actions.append(MarkSkipped(row.reminder_id, "item_completed"))
        elif desired_due is None or row.due_at_snapshot != desired_due:
            actions.append(MarkSuperseded(row.reminder_id))

    # Step 2 — make sure every wanted reminder exists, exactly once.
    # A snapshot is only meaningful alongside a due date, and `desired` is non-empty only
    # when one is set, so the loop is gated on it.
    index = {(row.reminder_type, row.due_at_snapshot): row for row in existing}
    if desired_due is not None:
        for target in desired:
            snapshot = desired_due
            stored = index.get((target.reminder_type, snapshot))
            if stored is None:
                # FR-4: a target that already passed is recorded, never sent.
                already_passed = target.target_at <= now
                actions.append(
                    InsertReminder(
                        reminder_type=target.reminder_type,
                        due_at_snapshot=snapshot,
                        target_at=target.target_at,
                        status="skipped" if already_passed else "pending",
                        skip_reason="missed_window" if already_passed else None,
                        idempotency_key=idempotency_key(
                            item.notion_page_id, target.reminder_type, snapshot
                        ),
                    )
                )
            elif (
                _is_revivable(stored)
                and target.target_at > now
                and item.is_active
                and not is_complete
            ):
                actions.append(FlipToPending(stored.reminder_id))
            # Otherwise leave the row alone: sent stays sent, and a missed window stays
            # missed. The unique key makes re-inserting impossible anyway.

    return actions


def _is_revivable(stored: PlannerReminder) -> bool:
    """Whether a stored row that is not already `pending` should go back to `pending`.

    Two states qualify, for different reasons — and only ever for the *desired* due date,
    because the caller looks a row up by `(reminder_type, due_at_snapshot)` and so can only
    reach one whose snapshot is the date the item has right now:

    * ``skipped(item_completed | item_inactive)`` — the *item* changed, not the date. Step
      2's symmetric transition (§2.3.2.B): un-completing or un-archiving an item revives a
      still-future reminder. `RESURRECTABLE_SKIPS` is the closed list.
    * ``superseded`` — the *date* changed and came back (FR-8 Option A). The row still
      occupies the unique key for the restored date, so without this it would sit there
      forever and the restored date would receive no reminder: a silently lost reminder,
      which CLAUDE.md constraint 7 forbids outright. This is the addition to step 2 the
      spec's prose does not spell out.

    A `sent` row is never revivable, so sent history and old schedules stay distinct. A
    `missed_window` skip is never revivable either: the window is gone for good (FR-4). The
    caller separately requires `target_at > now` and an active, incomplete item, so this
    predicate only has to answer "is this state a reversal of one we wrote?".
    """
    if stored.status == "superseded":
        return True
    return stored.status == "skipped" and stored.skip_reason in RESURRECTABLE_SKIPS
