"""The `IntentInterpreter` port (spec §2.3.6) — V1, build phase 5.

The MVP never calls this. `app.integrations.llm.deepseek.DeepSeekInterpreter` is the live
implementation; a fake stands in for it in tests. Keeping the port here, separate from the
implementation, is what lets everything above it be written and tested against the shape
alone.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.intents import Intent
from app.domain.types import InterpretationContext


class IntentInterpreter(Protocol):
    """Turns reply text into a structured intent. The only LLM use in the system."""

    async def interpret(self, ctx: InterpretationContext) -> Intent:
        """Interpret one reply against one item's context.

        Never schedules, decides eligibility, deduplicates, or computes dates — the app
        does all of that deterministically.
        """
        ...
