"""Unit coverage for the alert vocabulary (FR-13, spec §2.3.4).

`AlertService`'s send-and-cooldown contract is already exercised against a real Postgres in
`tests/integration/test_scheduler_tick.py::TestAlertService` — that path needs
`system_state`, the mail port, and the clock together, so it does not belong here and is
not duplicated.

What this file pins is the part that needs no database: the **closed set of alert types**.
Every other workstream reaches `alert()` by name (a crashed job maps to a type, the send
path maps to a type, the inbound sweep maps to a type), and a name outside `ALERT_TYPES`
raises before anything is sent. So a rename or a dropped entry here does not fail loudly in
that caller — it turns a real failure into no alert at all, which is the exact silent mode
FR-13 exists to prevent.
"""

from __future__ import annotations

from typing import cast

import pytest

from app.container import AppContainer
from app.services.alert_service import ALERT_TYPES, AlertService, last_alert_key

# §2.3.4 / FR-13, in the tuple's own order (the order the types were introduced).
EXPECTED_ALERT_TYPES: tuple[str, ...] = (
    "notion_sync_failed",  # repeated sync failure
    "reminder_send_failed",  # send failures after max attempts
    "outbound_cap_exceeded",  # the outbound cap
    "gmail_poll_failed",  # the Gmail poll job crashed / history unusable
    "gmail_auth_failed",  # Gmail rejected our credentials
    "deepseek_failed",  # DeepSeek outage
    "inbound_stuck",  # a `processing` row past its deadline
)


class _UntouchedContainer:
    """Stands in for `AppContainer` only on paths that return before it is read."""


def test_the_alert_vocabulary_is_exactly_the_specs_failure_list() -> None:
    assert ALERT_TYPES == EXPECTED_ALERT_TYPES


def test_every_alert_type_is_unique_and_non_empty() -> None:
    assert len(set(ALERT_TYPES)) == len(ALERT_TYPES)
    assert all(alert_type for alert_type in ALERT_TYPES)


def test_alert_types_are_lowercase_snake_case() -> None:
    # `system_state` keys and audit payloads are read by humans and grepped in ops; the
    # spelling is a contract, not a matter of taste.
    for alert_type in ALERT_TYPES:
        assert alert_type == alert_type.lower()
        assert alert_type.replace("_", "").isalnum()


async def test_an_unknown_type_is_rejected_before_the_container_is_touched() -> None:
    """A typo must fail at the call site instead of writing an alert nobody defined.

    The stub container is the point: an unknown type raises on the vocabulary check, before
    the clock, the settings, the session factory, or the mail port is reached.
    """
    service = AlertService(cast(AppContainer, _UntouchedContainer()))

    with pytest.raises(ValueError, match="unknown alert type"):
        await service.alert("sync_failed", "subject", "body")


def test_the_cooldown_key_shape_matches_the_system_state_contract() -> None:
    assert last_alert_key("gmail_auth_failed") == "last_alert:gmail_auth_failed"
