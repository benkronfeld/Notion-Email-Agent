"""`DeepSeekInterpreter` — the one LLM call in the system (spec §2.3.4, build phase 5).

The whole design rests on keeping this narrow. The model turns reply text into a validated
`Intent`; it never schedules, decides eligibility, deduplicates, identifies an item, or
computes a date. Everything it returns is re-checked by `parse_intent`, and every parse
failure resolves to "ask, do not guess".

Call shape, verified against DeepSeek's API (§2.3.4):

- `temperature=0` — this is extraction, not generation.
- `response_format={"type": "json_object"}` — **generic JSON mode only.** There is no
  JSON-schema `response_format` here, which is why the prompt states the field names and
  types itself and why validating the response is mandatory rather than a fallback.
- `extra_body={"thinking": {"type": "disabled"}}` — thinking is on by default at high
  effort and only adds latency to a task this small.

Failure handling. An invalid or empty response is a *parse* failure, not an exception, so
one retry is re-issued inline and a second failure returns `clarification_intent()` — no
write, no guessing (§2.3.4). A transport or API error is deliberately **not** swallowed: the
service layer models a DeepSeek outage as an alert-worthy failure, and returning a
clarification there would hide an outage behind a question the owner cannot answer.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.domain.intents import Intent, clarification_intent
from app.domain.types import InterpretationContext
from app.integrations.llm.interpreter import IntentInterpreter
from app.integrations.llm.prompts import (
    SYSTEM_PROMPT,
    InvalidIntentOutput,
    build_user_prompt,
    parse_intent,
)

# The fixed call settings, named rather than scattered inline. The model name is not here:
# it is configuration (`DEEPSEEK_MODEL`), read from `Settings` at call time.
_TEMPERATURE: float = 0.0
_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
_THINKING_DISABLED: dict[str, dict[str, str]] = {"thinking": {"type": "disabled"}}
# The initial request plus the one retry §2.3.4 allows before the safe fallback.
_MAX_ATTEMPTS: int = 2


class DeepSeekInterpreter(IntentInterpreter):
    """`IntentInterpreter` over the OpenAI-compatible DeepSeek client.

    `client` is injectable, matching how `NotionRestClient` takes an `http_client` — that is
    what lets a contract test drive the adapter with a recorded response and no network
    (CLAUDE.md constraint 5). When it is omitted the real `AsyncOpenAI` is built lazily on
    first use, so constructing this adapter performs no I/O and cannot fail on missing
    configuration until something is actually asked of it.
    """

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client: Any | None = client

    async def interpret(self, ctx: InterpretationContext) -> Intent:
        """Turn one reply into one validated `Intent`; never raise on a bad response."""
        messages = _messages(ctx)
        for _ in range(_MAX_ATTEMPTS):
            response = await self._client_or_build().chat.completions.create(
                model=self._settings.deepseek_model,
                messages=messages,
                temperature=_TEMPERATURE,
                response_format=_RESPONSE_FORMAT,
                extra_body=_THINKING_DISABLED,
            )
            try:
                return parse_intent(_content_of(response))
            except InvalidIntentOutput:
                continue
        # Both attempts produced unusable output: ask, do not guess (§2.3.4, Appendix B 10).
        return clarification_intent()

    # ── Internals ───────────────────────────────────────────────────────────

    def _client_or_build(self) -> Any:
        """The injected client, or a real one built on first use.

        Deferred exactly as `GmailClient._service_or_build` is: construction must not be a
        place where a missing key or an unreachable host can fail, and it must perform no
        network I/O.
        """
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self._settings.deepseek_base_url,
                api_key=self._settings.deepseek_api_key,
            )
        return self._client


def _messages(ctx: InterpretationContext) -> list[dict[str, str]]:
    """The two chat messages: the fixed rules, then this reply's fenced payload."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(ctx)},
    ]


def _content_of(response: Any) -> str | None:
    """The message content of a completion, or `None` when the response is unusable.

    A JSON-mode response can come back with no choices or with empty content (§2.3.4); both
    are invalid output, so this hands back `None` for `parse_intent` to reject rather than
    raising here.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    content = getattr(getattr(choices[0], "message", None), "content", None)
    return content if isinstance(content, str) else None
