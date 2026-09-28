"""`AlertService` — the owner's operational signal, rate-limited (FR-13, spec §2.3.4).

One rule shapes this module: **an alert type is not an audit event type.** The
`audit_log.event_type` vocabulary (§2.3.5) is closed, and it has exactly one entry for
this: `system_alert_sent`. The *kind* of alert travels in the payload
(`{"alert_type": ...}`) and in the subject line, never in `event_type`. Reusing
`event_type` for alert kinds would grow the vocabulary by every operational failure the
system can have — which is precisely how a closed vocabulary stops being one — and would
make "show me every alert" a query against an unbounded set of names.

Because of that, an unknown alert type is a `ValueError` and not a new vocabulary entry.
`ALERT_TYPES` below is the complete MVP set; a typo fails loudly at the call site instead of
writing a `system_alert_sent` row whose payload names an alert nobody defined.

Rate limiting (FR-13: at most one per failure type per `ALERT_COOLDOWN_HOURS`) lives in
`system_state` under `last_alert:{alert_type}` rather than in process memory: two
deployments overlap during a release, and an in-memory timestamp would let each of them
send its own copy of the same alert. The timestamp is written only when the mail actually
went out — a failed alert send does not start a cooldown, or a Gmail outage would silence
the very alert describing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from app.container import AppContainer
from app.db.repositories import audit as audit_repo
from app.db.repositories import state as state_repo
from app.integrations.gmail.compose import render_system_alert
from app.logging import get_logger

# The MVP's complete alert vocabulary (§2.3.4). Deliberately a tuple, not a set: it is
# small, it is read by a human, and its order is the order they were introduced.
ALERT_TYPES: Final[tuple[str, ...]] = (
    "notion_sync_failed",  # repeated Notion sync failure — the scheduler runs on local data
    "reminder_send_failed",  # a reminder spent its whole attempt budget without sending
    "outbound_cap_exceeded",  # MAX_OUTBOUND_EMAILS_PER_HOUR tripped; sending is paused
)

# `system_state` key prefix holding one alert type's last-send instant, as an ISO-8601 UTC
# string. Matches the `last_alert:{type}` shape in §2.3.5's `system_state` comment.
_LAST_ALERT_PREFIX = "last_alert:"


def last_alert_key(alert_type: str) -> str:
    """The `system_state` key holding when `alert_type` last sent. Public for tests/ops."""
    return f"{_LAST_ALERT_PREFIX}{alert_type}"


class AlertService:
    """Emails the owner about operational failures, at most once per type per cooldown.

    Constructed with the container like every other service: the mail port, settings, the
    injected clock, and the session factory all arrive from it, so a test swaps the world
    in one place.
    """

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._log = get_logger(__name__)

    async def alert(self, alert_type: str, subject: str, body: str) -> bool:
        """Send one rate-limited alert. True when it was actually sent.

        False means "suppressed by the cooldown", which is a successful outcome, not a
        failure — the caller must not retry it. A failure to *reach* Gmail propagates as an
        exception instead: the caller can then decide what to do, and nothing here pretends
        the message was delivered (the audit row and the cooldown stamp are written only
        after the send returns).
        """
        if alert_type not in ALERT_TYPES:
            known = ", ".join(ALERT_TYPES)
            raise ValueError(
                f"unknown alert type {alert_type!r}; the MVP alert vocabulary is: {known}. "
                "Adding one is a §2.3.4 change, not a call-site detail."
            )

        now = self._container.clock.now()
        if await self._in_cooldown(alert_type, now):
            self._log.info("alert_suppressed", alert_type=alert_type)
            return False

        settings = self._container.settings
        subject, body = render_system_alert(alert_type, subject, body)
        sent = await self._container.mail.send(
            to=settings.reminder_recipient,
            subject=subject,
            body=body,
            # An alert is its own conversation: it is not a reminder anyone should reply to
            # as if it were one, and there is no item behind it to map a reply to.
            thread_id=None,
            in_reply_to=None,
        )

        async with self._container.session_factory() as session:
            await state_repo.set_str(session, last_alert_key(alert_type), now.isoformat())
            await audit_repo.append(
                session,
                "system_alert_sent",
                provider_message_id=sent.provider_message_id,
                provider_thread_id=sent.provider_thread_id,
                payload={"alert_type": alert_type},
            )
            await session.commit()

        self._log.warning("alert_sent", alert_type=alert_type, subject=subject)
        return True

    async def _in_cooldown(self, alert_type: str, now: datetime) -> bool:
        """Whether this type sent an alert less than `ALERT_COOLDOWN_HOURS` ago.

        A stored value that cannot be read (wrong type, unparseable, naive) is treated as
        *no* previous alert. That direction is deliberate: a corrupt cooldown stamp must not
        silently swallow the owner's only signal that something is broken, and the cost of
        being wrong is one extra email.
        """
        async with self._container.session_factory() as session:
            stored = await state_repo.get_str(session, last_alert_key(alert_type))
        if stored is None:
            return False

        try:
            last_sent = datetime.fromisoformat(stored)
        except ValueError:
            self._log.warning("alert_cooldown_unreadable", alert_type=alert_type, value=stored)
            return False
        if last_sent.tzinfo is None:
            self._log.warning("alert_cooldown_naive", alert_type=alert_type, value=stored)
            return False

        cooldown = timedelta(hours=self._container.settings.alert_cooldown_hours)
        return now.astimezone(UTC) - last_sent.astimezone(UTC) < cooldown
