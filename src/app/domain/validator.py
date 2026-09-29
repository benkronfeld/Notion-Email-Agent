"""The validator (spec §2.3.4, "Validator").

The last pure step before anything is written. It takes the model's intent and the item it
is about and answers one of exactly three ways: apply a change, do nothing because there
is nothing to do, or ask a question. Nothing here talks to Notion, to Gmail, or to a
clock — the item's current values arrive as arguments.

**`ApplyChange` carries `status` and `due_date` and nothing else.** There is deliberately
no `done` field. `Done` is derived purely inside `NotionWriter`, from whatever `status`
comes out of here, so the write pipeline as a whole touches only `Status`, `Done`, and
`Due Date`. Giving the validator an opinion on `Done` would put the same decision in two
places and let them disagree, which is precisely what FR-3's dual `is_complete` check
exists to survive.

Two judgment calls are written down because the spec leaves them open:

* An unrecognised status is checked *before* the date. A `change_status_and_due_date` that
  is wrong about both is answered with the status question, because that is the one the
  owner can fix without any date reasoning.
* `Intent.needs_clarification` is honoured even when the action asks for a change. The
  model sets it when it is unsure, and §2.3.4's whole posture is that uncertainty means
  asking rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.domain.date_resolver import Ambiguous, Resolution
from app.domain.intents import ALLOWED_STATUSES, Intent, IntentAction


@dataclass(frozen=True, slots=True)
class ApplyChange:
    """A change that should be written.

    Only the parts that actually differ from the item's current values are set — a `None`
    means "leave that property alone". Both fields are never `None` at once: that case is
    `NoChangeNeeded`.
    """

    status: str | None
    due_date: date | None


@dataclass(frozen=True, slots=True)
class NoChangeNeeded:
    """Nothing would change — the reply asked for a value the item already has.

    This is FR-10's "already set" path. The caller sends the "already set" reply and writes
    nothing. It is also the answer to `no_action`, where there is nothing to change *and*
    nothing to say; the caller decides whether that silence deserves a reply.
    """


@dataclass(frozen=True, slots=True)
class NeedsClarification:
    """The change cannot be made as asked; ask a question instead of guessing."""

    question: str


ValidationOutcome = ApplyChange | NoChangeNeeded | NeedsClarification


_UNCLEAR_QUESTION = (
    "I couldn't tell what you'd like me to change. Reply with a status "
    "(Not started / In progress / Completed) or the exact date you want it due."
)

_STATUS_QUESTION = (
    "I don't recognise that status. Reply with one of: " + " / ".join(ALLOWED_STATUSES) + "."
)

_DATE_MISSING_QUESTION = (
    "I couldn't find a date in your reply. Which exact date should it be due? "
    'For example, reply "Oct 3".'
)


def validate(
    intent: Intent,
    *,
    current_status: str,
    current_due_date: date | None,
    resolved: Resolution | None = None,
) -> ValidationOutcome:
    """Intent + item -> the one thing to do about it (spec §2.3.4).

    `resolved` is the output of `date_resolver.resolve_date` for the intent's
    `due_date_text`; it is `None` when no date phrase was asked for, and the caller passes
    the resolver's own `Ambiguous` through untouched so its owner-facing reason reaches the
    clarification email.
    """
    if intent.action is IntentAction.NO_ACTION:
        return NoChangeNeeded()

    if intent.action is IntentAction.ASK_CLARIFICATION or intent.needs_clarification:
        return NeedsClarification(intent.clarification_question or _UNCLEAR_QUESTION)

    wants_status = intent.action in (
        IntentAction.CHANGE_STATUS,
        IntentAction.CHANGE_STATUS_AND_DUE_DATE,
    )
    wants_due_date = intent.action in (
        IntentAction.CHANGE_DUE_DATE,
        IntentAction.CHANGE_STATUS_AND_DUE_DATE,
    )

    new_status: str | None = None
    if wants_status:
        # Never write a status the app does not recognise. The intent schema already
        # constrains this, so reaching here means the intent was built some other way —
        # which is exactly when a guard is worth having.
        if intent.status is None or intent.status not in ALLOWED_STATUSES:
            return NeedsClarification(_STATUS_QUESTION)
        if intent.status != current_status:
            new_status = str(intent.status)

    new_due_date: date | None = None
    if wants_due_date:
        if resolved is None:
            return NeedsClarification(_DATE_MISSING_QUESTION)
        if isinstance(resolved, Ambiguous):
            return NeedsClarification(resolved.reason)
        if resolved.date != current_due_date:
            new_due_date = resolved.date

    # Every requested part already holds. Nothing to write, and the caller's cue to send
    # the "already set" reply.
    if new_status is None and new_due_date is None:
        return NoChangeNeeded()

    return ApplyChange(status=new_status, due_date=new_due_date)
