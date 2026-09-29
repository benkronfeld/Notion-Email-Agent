"""The `users.history.list` logic behind one inbound poll (spec §2.3.2.E).

Pure functions over a Gmail `history.list` response. `client.GmailClient.poll_new` does the
network call and hands the response here, so the two decisions that actually matter — which
message ids are new, and what cursor to store next — are testable from a literal dict.

**Gmail repeats itself, and the fallback repeats it more.** A single `history.list` response
can mention the same message in several records, and the 404 fallback re-runs a plain search
that happily re-delivers anything still matching `in:inbox newer_than:2d`. Nothing here is
what makes that safe: correctness rests on `processed_inbound_messages`'s
`UNIQUE(provider_message_id)` (§2.3.2.E step 1). A re-delivered message is deduplicated at
insert time, so this module can afford to be generous — it de-duplicates within a single
response, but it does not, and cannot, guarantee cross-poll uniqueness.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

# The bounded search used when Gmail has expired the stored history id (§2.3.2.E). Bounded is
# the point: a 404 must not become "scan the whole mailbox", and two days comfortably covers
# a reminder sent within the last day or so.
_FALLBACK_QUERY = "in:inbox newer_than:2d"


def history_message_ids(
    history_response: Mapping[str, Any],
    *,
    already_known: set[str] | None = None,
) -> list[str]:
    """The new message ids in a `history.list` response, de-duplicated and order-preserving.

    Reads both shapes Gmail uses for the same message — the flat `messages[]` list and
    `messagesAdded[].message` — across every history record, because which one is populated
    depends on the `historyTypes` filter and the Gmail API version. `already_known` (the ids
    this poll has already collected or processed) is excluded before de-duplication.
    """
    known = already_known or set()
    found: list[str] = []
    seen: set[str] = set()
    for record in history_response.get("history") or []:
        for message_id in _record_message_ids(record):
            if message_id in known or message_id in seen:
                continue
            seen.add(message_id)
            found.append(message_id)
    return found


def new_history_id(history_response: Mapping[str, Any]) -> str | None:
    """The `historyId` to store as the next cursor, or `None` when the response omits it."""
    value = history_response.get("historyId")
    return str(value) if value is not None else None


def search_query_fallback() -> str:
    """The query for the 404 fallback: `in:inbox newer_than:2d` (§2.3.2.E).

    A search has no cursor, so it can return messages that were already handled. That is
    safe only because `processed_inbound_messages` deduplicates on `provider_message_id`.
    """
    return _FALLBACK_QUERY


def _record_message_ids(record: Mapping[str, Any]) -> Iterator[str]:
    """Every message id one history record mentions, in the order it lists them."""
    for entry in record.get("messages") or []:
        if isinstance(entry, Mapping) and entry.get("id") is not None:
            yield str(entry["id"])
    for entry in record.get("messagesAdded") or []:
        message = entry.get("message") if isinstance(entry, Mapping) else None
        if isinstance(message, Mapping) and message.get("id") is not None:
            yield str(message["id"])
