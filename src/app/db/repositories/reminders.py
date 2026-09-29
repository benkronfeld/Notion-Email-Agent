"""`reminders` repository — the SQL half of `reconcile_reminders` (spec §2.3.2.B, §2.3.2.C).

`app.domain.planner` decides *what* should change; this module is the only place that turns
those decisions into SQL. It holds no policy of its own: `reconcile_reminders` loads the
rows, converts them to planner DTOs, calls `plan_reminders`, and hands the result to
`apply_plan`. Putting a rule here instead of in `planner.py` would move decision logic into
the one layer that cannot be unit-tested without a database.

Idempotence — the property the whole design rests on — comes from three places, and none of
them is this file's good intentions:

* ``UNIQUE(item_id, reminder_type, due_at_snapshot)`` — a repeated `InsertReminder` is
  absorbed by ``ON CONFLICT DO NOTHING``. The `reminder_created` audit row is written only
  when the ``RETURNING`` clause proves the INSERT actually happened, so a second reconcile
  is silent in the audit trail as well as in the table.
* ``UNIQUE(idempotency_key)`` — the same fact expressed a second way, so a row inserted by
  any other path cannot be duplicated either.
* A status guard on every UPDATE: a cancel only touches `pending`/`claimed` rows, a
  resurrection only touches a `skipped` or `superseded` one. An action replayed against a
  row that already reached its target state changes nothing, which is what makes
  `apply_plan` return 0 the second time.

Reads use ``populate_existing()`` because the state transitions here are Core UPDATEs: an
entity already in the session's identity map would otherwise keep handing back its
pre-update attribute values, and a second reconcile would plan against stale rows.

## Why `FlipToPending` writes no audit event

§2.3.5's ``event_type`` list is a closed vocabulary and it has no resurrection event. The
choices were `reminder_created`, `reminder_skipped`, or nothing, and this module writes
nothing:

* `reminder_created` would be a false statement. No row is created — the row has existed
  since its original insert (`created_at` proves it) and it is still that same row under
  the same ``UNIQUE(item_id, reminder_type, due_at_snapshot)`` key. An auditor joining
  `audit_log` to `reminders` on "created" would find an event with no creation behind it,
  and the log is only worth having if every row in it is true.
* Nothing is lost. A resurrection only happens while the reminder is still in the future
  and unsent (the planner requires ``target_at > now``), so it can only ever *prevent* a
  gap in sending, never cause a wrong send. The transition is recorded on the row itself
  (`status='pending'`, `skip_reason IS NULL`, `updated_at`), and the pause it reverses was
  already audited by the `reminder_skipped` event that put the row into `skipped` in the
  first place. `reminder_sent` remains the authoritative answer to "was this item ever
  emailed, and when?".
* `reminder_skipped` would be worse than either: the row is being *un*-skipped, so the
  event would say the opposite of what happened.

Inventing an event, or reusing one whose meaning is wrong, would corrupt the single record
this project treats as append-only truth. Writing nothing is the honest choice, and this
comment is the deliberate record that it was a choice.
"""

from __future__ import annotations

import secrets
import string
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item, Reminder
from app.db.repositories import audit
from app.domain.planner import (
    IN_FLIGHT,
    FlipToPending,
    InsertReminder,
    MarkSkipped,
    MarkSuperseded,
    PlannerPolicy,
    ReminderAction,
    iso_utc,
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

# Short and alphanumeric: the token is printed in an email footer as `ref: <token>`, found
# again by substring search, and passed to Gmail's `q=` (which tokenises on punctuation).
# 12 characters of [A-Za-z0-9] is ~71 bits — collision is not a practical concern, which is
# why the unique-constraint collision path below is a single retry and not a loop.
REF_TOKEN_LENGTH = 12
_REF_TOKEN_ALPHABET = string.ascii_letters + string.digits

# The planner's `IN_FLIGHT`, materialised as a sorted tuple for a deterministic `IN (...)`.
# Imported rather than retyped so the two layers cannot disagree about which rows are still
# cancellable.
_IN_FLIGHT_STATUSES: tuple[str, ...] = tuple(sorted(IN_FLIGHT))


def generate_ref_token() -> str:
    """A short alphanumeric token for the email footer (§2.3.5, §2.3.2.C).

    `secrets`, not `random`: the token is the only thing tying a sent email back to its
    reminder row during stale-claim recovery, and guessing one is not a capability worth
    leaving available.
    """
    return "".join(secrets.choice(_REF_TOKEN_ALPHABET) for _ in range(REF_TOKEN_LENGTH))


# ── ORM -> planner DTOs ─────────────────────────────────────────────────────


def to_planner_item(item: Item) -> PlannerItem:
    """The item fields `plan_reminders` needs.

    The `cast`s are the boundary between the database's ``text`` columns and the closed
    vocabularies of §2.3.5; the CHECK constraints on `items` are what make them true.
    """
    return PlannerItem(
        item_id=item.id,
        notion_page_id=item.notion_page_id,
        item_kind=cast(ItemKind, item.item_kind),
        status=item.status,
        done=item.done,
        due_at=item.due_at,
        is_active=item.is_active,
    )


def to_planner_reminder(row: Reminder) -> PlannerReminder:
    """An existing reminder row as the planner sees it."""
    return PlannerReminder(
        reminder_id=row.id,
        reminder_type=cast(ReminderType, row.reminder_type),
        due_at_snapshot=row.due_at_snapshot,
        target_at=row.target_at,
        status=cast(ReminderStatus, row.status),
        skip_reason=cast(SkipReason | None, row.skip_reason),
    )


# ── Reads ───────────────────────────────────────────────────────────────────


async def load_for_item(session: AsyncSession, item_id: UUID) -> list[Reminder]:
    """Every reminder row for one item, earliest target first.

    All statuses are returned, not just the live ones: the planner needs to see `sent`
    and `superseded` rows to know a given (type, due_at_snapshot) already exists and must
    not be duplicated.
    """
    result = await session.execute(
        select(Reminder)
        .where(Reminder.item_id == item_id)
        # The state transitions in this module are Core UPDATEs, which do not refresh
        # entities already in the identity map. Without this, a second reconcile in the
        # same session would plan against the pre-update rows.
        .execution_options(populate_existing=True)
        .order_by(Reminder.target_at, Reminder.reminder_type)
    )
    rows: list[Reminder] = list(result.scalars().all())
    return rows


async def get_by_id(session: AsyncSession, reminder_id: UUID) -> Reminder | None:
    """The reminder with this id, or None."""
    result = await session.execute(
        select(Reminder).where(Reminder.id == reminder_id).execution_options(populate_existing=True)
    )
    row: Reminder | None = result.scalar_one_or_none()
    return row


async def list_stale_claimed(
    session: AsyncSession, *, now: datetime, stale_after: timedelta
) -> list[Reminder]:
    """Claimed rows older than `stale_after` — the crash-between-claim-and-send queue.

    §2.3.2.C recovers each of these by searching Gmail Sent for its `ref_token`; the
    caller decides, this only finds them. Ordered by `claimed_at` so the longest-stuck
    reminder is looked at first.
    """
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.status == "claimed",
            Reminder.claimed_at.is_not(None),
            Reminder.claimed_at <= now - stale_after,
        )
        .execution_options(populate_existing=True)
        .order_by(Reminder.claimed_at)
    )
    rows: list[Reminder] = list(result.scalars().all())
    return rows


async def has_later_claimable(
    session: AsyncSession,
    *,
    reminder_id: UUID,
    item_id: UUID,
    due_at_snapshot: datetime,
    target_at: datetime,
    now: datetime,
) -> bool:
    """Whether another, strictly later reminder for the same item *and due date* is ready.

    §2.3.2.C uses this to avoid two emails for one item arriving within seconds of each
    other after downtime: if the 48h and 24h reminders both came due while the process was
    down, the 48h one is skipped(`superseded_by_later`) and only the 24h email goes out.

    Same `due_at_snapshot` deliberately, not just same item: after a due-date change the
    old schedule is `superseded`, and a reminder for the *new* date must never suppress a
    live one for the old.
    """
    result = await session.execute(
        select(Reminder.id)
        .where(
            Reminder.id != reminder_id,
            Reminder.item_id == item_id,
            Reminder.due_at_snapshot == due_at_snapshot,
            Reminder.target_at > target_at,
            Reminder.target_at <= now,
            Reminder.status == "pending",
            or_(Reminder.next_attempt_at.is_(None), Reminder.next_attempt_at <= now),
        )
        .limit(1)
    )
    return result.first() is not None


# ── reconcile + apply ───────────────────────────────────────────────────────


async def reconcile_reminders(
    session: AsyncSession, item: Item, now: datetime, policy: PlannerPolicy
) -> list[ReminderAction]:
    """Bring an item's reminders in line with the item (spec §2.3.2.B).

    The composition, deliberately: read the rows, convert them, let the pure planner
    decide, apply the decisions. Every rule lives in `app.domain.planner`; nothing is
    re-implemented or second-guessed here.

    Idempotent — a second call with the same item state returns ``[]`` because the first
    call left the rows exactly as the planner wants them. The caller owns the transaction:
    this does not commit, so a sync that fails afterwards rolls the reminders back with it.
    """
    existing = await load_for_item(session, item.id)
    actions = plan_reminders(
        to_planner_item(item),
        [to_planner_reminder(row) for row in existing],
        now,
        policy,
    )
    if actions:
        await apply_plan(session, item_id=item.id, actions=actions, now=now)
    return actions


async def apply_plan(
    session: AsyncSession,
    *,
    item_id: UUID,
    actions: Sequence[ReminderAction],
    now: datetime,
) -> int:
    """Turn planner actions into SQL, and audit what actually changed.

    Returns the number of rows actually changed — an action that was already satisfied
    (its row already in the target state, or its insert absorbed by the unique key) counts
    zero and writes no audit row.

    Every audit row is written in the caller's transaction, alongside the change it
    describes: a reminder state change and the record of it either both commit or neither
    does. The audit log is never UPDATEd or DELETEd from here (or anywhere).
    """
    changed = 0

    for action in actions:
        if isinstance(action, InsertReminder):
            new_id = await _insert_if_absent(session, item_id=item_id, action=action)
            if new_id is None:
                # ON CONFLICT DO NOTHING: the row was already there, so nothing changed and
                # nothing is audited. This is what makes a repeated reconcile silent.
                continue
            changed += 1
            # For a `skipped(missed_window)` insert this is still `reminder_created`: the
            # row really was created, and the payload's `skip_reason` says why it will
            # never fire (FR-4).
            await audit.append(
                session,
                "reminder_created",
                item_id=item_id,
                payload=_insert_payload(new_id, action),
            )

        elif isinstance(action, MarkSuperseded):
            # No skip_reason: a supersede only ever means the due date moved (§2.3.2.B
            # step 1), and `superseded_by_later` belongs to the scheduler's decision, not
            # the planner's.
            superseded = await _cancel(
                session,
                action.reminder_id,
                status="superseded",
                skip_reason=None,
                now=now,
            )
            if superseded:
                changed += 1
                await audit.append(
                    session,
                    "reminder_superseded",
                    item_id=item_id,
                    payload={"reminder_id": str(action.reminder_id)},
                )

        elif isinstance(action, MarkSkipped):
            if await _cancel(
                session,
                action.reminder_id,
                status="skipped",
                skip_reason=action.skip_reason,
                now=now,
            ):
                changed += 1
                await audit.append(
                    session,
                    "reminder_skipped",
                    item_id=item_id,
                    payload={
                        "reminder_id": str(action.reminder_id),
                        "skip_reason": action.skip_reason,
                    },
                )

        elif isinstance(action, FlipToPending):
            # Deliberately no audit event — see the module docstring. The resurrection is
            # recorded on the row itself (`status`, `skip_reason`, `updated_at`).
            if await _flip_to_pending(session, action.reminder_id, now=now):
                changed += 1

    return changed


def _insert_payload(new_id: UUID, action: InsertReminder) -> dict[str, Any]:
    """The audit payload for one created reminder. Datetimes as ISO-8601 UTC strings."""
    return {
        "reminder_id": str(new_id),
        "reminder_type": action.reminder_type,
        "due_at_snapshot": iso_utc(action.due_at_snapshot),
        "target_at": iso_utc(action.target_at),
        "status": action.status,
        "skip_reason": action.skip_reason,
    }


async def _insert_if_absent(
    session: AsyncSession, *, item_id: UUID, action: InsertReminder
) -> UUID | None:
    """INSERT one reminder, or None when the unique key says it already exists.

    The `(item_id, reminder_type, due_at_snapshot)` conflict is the ordinary case and is
    absorbed by ``ON CONFLICT DO NOTHING``. The `ref_token` collision is not: it is a
    *different* row, and Postgres will not skip one conflict while raising the other — so
    it is the one error caught here, and it is retried exactly once with a fresh token.
    A second collision propagates: it is impossible in practice, and swallowing it into a
    third attempt would be pretending to know something we do not.

    Every other integrity error is a real bug and propagates on the first throw. Nothing
    in this function turns a constraint violation into a silent success.
    """
    try:
        return await _insert_with_fresh_token(session, item_id=item_id, action=action)
    except IntegrityError as exc:
        if "ref_token" not in str(exc.orig):
            raise
    return await _insert_with_fresh_token(session, item_id=item_id, action=action)


async def _insert_with_fresh_token(
    session: AsyncSession, *, item_id: UUID, action: InsertReminder
) -> UUID | None:
    """One attempt at the INSERT, with a brand-new `ref_token`."""
    stmt = (
        pg_insert(Reminder)
        .values(
            item_id=item_id,
            reminder_type=action.reminder_type,
            due_at_snapshot=action.due_at_snapshot,
            target_at=action.target_at,
            status=action.status,
            skip_reason=action.skip_reason,
            idempotency_key=action.idempotency_key,
            ref_token=generate_ref_token(),
        )
        .on_conflict_do_nothing(
            index_elements=[
                Reminder.item_id,
                Reminder.reminder_type,
                Reminder.due_at_snapshot,
            ]
        )
        .returning(Reminder.id)
    )
    # A savepoint, not a bare execute: after a statement fails, the surrounding PostgreSQL
    # transaction is aborted and every later statement in it fails with "current
    # transaction is aborted". Without the savepoint there would be no working transaction
    # left to retry in.
    async with session.begin_nested():
        result = await session.execute(stmt)
        row = result.first()
    if row is None:
        return None
    new_id: UUID = row[0]
    return new_id


async def _cancel(
    session: AsyncSession,
    reminder_id: UUID,
    *,
    status: ReminderStatus,
    skip_reason: SkipReason | None,
    now: datetime,
) -> bool:
    """Move an in-flight reminder to a cancelled state. True only if a row changed.

    The `pending`/`claimed` guard is not decoration. It makes a replayed action a no-op,
    and it is a genuine safety rail against a race with the scheduler: a row the scheduler
    sent in the microseconds since the planner read it is `sent`, and this cannot un-send
    it.
    """
    result = await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id, Reminder.status.in_(_IN_FLIGHT_STATUSES))
        .values(status=status, skip_reason=skip_reason, updated_at=now)
        .returning(Reminder.id)
        .execution_options(synchronize_session="evaluate")
    )
    return result.first() is not None


async def _flip_to_pending(session: AsyncSession, reminder_id: UUID, *, now: datetime) -> bool:
    """Resurrect a cancelled reminder — `skipped` or `superseded`. True if a row changed.

    Both statuses are accepted because the planner revives both (see
    `planner._is_revivable`): a `skipped` row when the item becomes active and incomplete
    again, and a `superseded` row when its due date comes back. `sent` and `failed` rows
    are still untouchable — the two terminal outcomes are not revivals.

    Everything the cancellation set is cleared, not just the status: a resurrected row must
    be indistinguishable from a freshly inserted `pending` one, or the scheduler would claim
    it carrying a stale backoff. `claimed_at` is cleared for the same reason — the row is
    not in flight yet.
    """
    result = await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id, Reminder.status.in_(("skipped", "superseded")))
        .values(
            status="pending",
            skip_reason=None,
            next_attempt_at=None,
            claimed_at=None,
            updated_at=now,
        )
        .returning(Reminder.id)
        .execution_options(synchronize_session="evaluate")
    )
    return result.first() is not None


# ── Scheduler: claim (spec §2.3.2.C) ────────────────────────────────────────


async def claim_next(session: AsyncSession, now: datetime) -> Reminder | None:
    """Claim the single most-due pending reminder, or None when nothing is ready.

    A transliteration of §2.3.2.C's statement::

        UPDATE reminders SET status='claimed', claimed_at=:now, attempt_count=attempt_count+1
         WHERE id = (SELECT id FROM reminders
                     WHERE status='pending' AND target_at <= :now
                       AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
                     ORDER BY target_at FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING *

    `FOR UPDATE SKIP LOCKED` inside the subquery is what lets two workers run this at the
    same instant: the second skips the row the first has locked and claims the next one,
    instead of blocking or claiming the same row twice. `attempt_count` is incremented
    here, on the claim, deliberately — the 5-attempt budget counts claims, so a crash
    between claim and send still spends one.
    """
    due = (
        select(Reminder.id)
        .where(
            Reminder.status == "pending",
            Reminder.target_at <= now,
            or_(Reminder.next_attempt_at.is_(None), Reminder.next_attempt_at <= now),
        )
        .order_by(Reminder.target_at)
        .with_for_update(skip_locked=True)
        .limit(1)
        .scalar_subquery()
    )
    result = await session.execute(
        update(Reminder)
        .where(Reminder.id == due)
        .values(
            status="claimed",
            claimed_at=now,
            attempt_count=Reminder.attempt_count + 1,
            updated_at=now,
        )
        .returning(Reminder)
        # `attempt_count=attempt_count + 1` cannot be evaluated in Python, so the ORM
        # cannot synchronise the identity map from the statement. `populate_existing` does
        # it from the RETURNING row instead, and `False` keeps the ORM from silently
        # emitting a second SELECT to fetch what the UPDATE already returned.
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    claimed: Reminder | None = result.scalars().first()
    return claimed


# ── Scheduler: outcomes ─────────────────────────────────────────────────────


async def mark_sent(
    session: AsyncSession,
    reminder_id: UUID,
    *,
    now: datetime,
    provider_message_id: str,
    provider_thread_id: str,
) -> None:
    """Record a successful send.

    `claimed_at` and `last_error` are left alone on purpose: the gap between them is the
    only evidence of how long the claim was held and whether a retry was needed, and it
    costs nothing to keep. Not guarded on `status='claimed'` — if the mail really went out,
    the row must say so, and a silent lost reminder is the one outcome the design refuses.
    """
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(
            status="sent",
            sent_at=now,
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            updated_at=now,
        )
        .execution_options(synchronize_session="evaluate")
    )


async def mark_failed(session: AsyncSession, reminder_id: UUID, *, error: str) -> None:
    """Terminal failure: the attempt budget is spent. An alert accompanies this (§2.3.2.C).

    No `now` parameter, so `updated_at` is stamped by the database. That is not the app
    reading a clock: nothing is ever decided from `updated_at`, it exists for humans
    reading the table, and the alternative — inventing a `now` at the call site — would be
    the same value the caller already had. Every instant the system *reasons* about still
    arrives through the injected Clock.
    """
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(status="failed", last_error=error, updated_at=func.now())
        .execution_options(synchronize_session="evaluate")
    )


async def mark_skipped(
    session: AsyncSession, reminder_id: UUID, *, skip_reason: SkipReason, now: datetime
) -> None:
    """Scheduler-side skip: completed, inactive, past due, or superseded by a later one."""
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(status="skipped", skip_reason=skip_reason, updated_at=now)
        .execution_options(synchronize_session="evaluate")
    )


async def release_with_backoff(
    session: AsyncSession, reminder_id: UUID, *, next_attempt_at: datetime, error: str
) -> None:
    """Put a failed-but-retryable send back in the queue, no sooner than `next_attempt_at`.

    `claimed_at` is cleared: the row is no longer in flight, and leaving it set would make
    a stale-claim sweep look at a row that is already correctly waiting out its backoff.
    """
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(
            status="pending",
            next_attempt_at=next_attempt_at,
            claimed_at=None,
            last_error=error,
            updated_at=func.now(),
        )
        .execution_options(synchronize_session="evaluate")
    )


async def mark_superseded(session: AsyncSession, reminder_id: UUID) -> None:
    """Withdraw a row whose due date moved (FR-8 Option A).

    `skip_reason` is cleared, not set: supersession is not a skip reason. The row is not
    simply dead, either — the planner revives it to `pending` if the due date moves back to
    this snapshot (§2.3.2.B step 2), and `_flip_to_pending` clears the field again on the way.
    A reason here would therefore imply a meaning that neither state has.
    """
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(status="superseded", skip_reason=None, updated_at=func.now())
        .execution_options(synchronize_session="evaluate")
    )
