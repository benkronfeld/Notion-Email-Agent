"""The `IntentInterpreter` port (spec §2.3.6) — V1, declared only.

The MVP never calls this. It exists so the container's shape is stable and so the DeepSeek
implementation (build phase 5) has a fixed target to fill in.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.types import Intent, InterpretationContext


class IntentInterpreter(Protocol):
    """Turns reply text into a structured intent. The only LLM use in the system."""

    async def interpret(self, ctx: InterpretationContext) -> Intent:
        """Interpret one reply against one item's context.

        Never schedules, decides eligibility, deduplicates, or computes dates — the app
        does all of that deterministically.
        """
        ...
