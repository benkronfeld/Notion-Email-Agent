"""The intent schema — the LLM's only output shape (spec §2.3.4).

This is the boundary of the entire design. The model's single job is turning reply text
into one of these objects; everything else (scheduling, eligibility, deduplication, item
identification, date arithmetic) is deterministic code. That is why this module is pure
and why nothing here knows what a Notion page or a Gmail thread is.

**Pydantic, not a dataclass.** §2.2 selects Pydantic for LLM output and §2.3.4 makes
validation on the response mandatory rather than a fallback, because DeepSeek's API
supports generic JSON mode only — there is no JSON-schema `response_format` to lean on.
`extra="forbid"` is the load-bearing part: a model that invents a field produces a
validation error the adapter can see, instead of a silently ignored key.

**`due_date_text` is a phrase, never a date.** The model returns what the user *said*
("Friday", "Oct 3"); `date_resolver` turns that into a date. Keeping the split here is what
stops an LLM from doing arithmetic, which Appendix A and §2.3.4 both require.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class StatusValue(StrEnum):
    """The three statuses as spoken by the owner (§1.2, FR-10).

    Exactly these three, and they are the *intent* vocabulary — not the Notion property
    vocabulary. `NOTION_STATUS_COMPLETED` config names which Notion value means complete;
    this enum is what the owner is allowed to ask for.
    """

    NOT_STARTED = "Not started"
    IN_PROGRESS = "In progress"
    COMPLETED = "Completed"


class IntentAction(StrEnum):
    """What the reply is asking for (§2.3.4)."""

    CHANGE_STATUS = "change_status"
    CHANGE_DUE_DATE = "change_due_date"
    CHANGE_STATUS_AND_DUE_DATE = "change_status_and_due_date"
    ASK_CLARIFICATION = "ask_clarification"
    NO_ACTION = "no_action"


# The statuses the interpreter may return, in a stable order — handed to the model as the
# allowed set and reused by the validator. Derived from the enum so the two cannot drift.
ALLOWED_STATUSES: tuple[str, ...] = tuple(status.value for status in StatusValue)

_CLARIFICATION_FALLBACK_QUESTION = (
    "I couldn't tell what you'd like me to change. Reply with a status "
    "(Not started / In progress / Completed) or the exact date you want it due."
)


class Intent(BaseModel):
    """One reply, as a structured intent (§2.3.4).

    Frozen and `extra="forbid"` so an adapter cannot quietly attach state the rest of the
    pipeline never validated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: IntentAction
    status: StatusValue | None = None
    due_date_text: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Intent:
        """Enforce the per-action requirements from §2.3.4.

        These are consistency checks on the model's own output, not business rules: a
        `change_status` with no status is an incoherent intent, and failing here is what
        lets the adapter treat it as invalid output (one retry, then safe fallback) rather
        than letting a half-formed intent reach the validator.
        """
        if self.action is IntentAction.CHANGE_STATUS and self.status is None:
            raise ValueError("change_status requires `status`")
        if self.action is IntentAction.CHANGE_DUE_DATE and not self.due_date_text:
            raise ValueError("change_due_date requires `due_date_text`")
        if self.action is IntentAction.CHANGE_STATUS_AND_DUE_DATE and (
            self.status is None or not self.due_date_text
        ):
            raise ValueError(
                "change_status_and_due_date requires both `status` and `due_date_text`"
            )
        if self.action is IntentAction.ASK_CLARIFICATION and not self.clarification_question:
            raise ValueError("ask_clarification requires `clarification_question`")
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("`needs_clarification` requires a `clarification_question`")
        return self


def clarification_intent(question: str = _CLARIFICATION_FALLBACK_QUESTION) -> Intent:
    """The safe fallback: ask, never guess (§2.3.4, Appendix B case 10).

    Used when the model's output is invalid or empty even after its one retry. Returning an
    intent rather than raising is deliberate — the pipeline's answer to "I don't know" is a
    question, which is a legitimate outcome, not an error.
    """
    return Intent(
        action=IntentAction.ASK_CLARIFICATION,
        needs_clarification=True,
        clarification_question=question,
    )
