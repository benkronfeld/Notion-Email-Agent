"""`InboundService` — the reply pipeline (spec §2.3.2.E).

This is the one place in the system where an LLM is consulted, and the ordering below is
what keeps that safe. §2.3.2.E's numbered steps are load-bearing, so they are implemented in
exactly that order and each one is commented with its number:

1. **Dedupe first.** The `INSERT ... ON CONFLICT DO NOTHING` in `inbound_repo.claim` decides
   who processes a message *before* a sender check, before an item lookup, and long before
   any model call or write. A duplicate stops here and does nothing else.
2. **Then the sender.** Allowlist plus auto-reply headers, so a mailing list or a vacation
   responder can never be read as the owner's instruction (FR-11).
3. **Then the item, by stored provider ids only** — a Gmail `threadId`, or `In-Reply-To` /
   `References` against the RFC `Message-ID` we generated at send time. **Never by name.**
   No match fails safe: an audit event and a plain notice, no guessing.
4. Only then is the new reply text extracted, and 5. only that one item's context loaded.
6. The interpreter runs, and its output is treated as **untrusted**: 7. deterministic code
   resolves the date phrase and validates the intent, and anything unclear becomes a
   question instead of a write.
8-9. The write is live-fetched, patched once, and **read back**; success is claimed only when
   the read-back agrees, and a confirmation quotes what Notion actually holds.

Three behaviours here are easy to "improve" into a bug:

* **An unmappable reply gets no thread row.** `outbound_messages.thread_id` is `NOT NULL`
  and `email_threads.item_id` is too, and an unmapped reply has neither — so the notice is
  sent as a brand-new Gmail thread with no local record. That is deliberate: inventing a
  thread would mean inventing an item, which is the guessing step 3 forbids.
* **A DeepSeek transport failure is not answered with a question.** The interpreter
  propagates real API errors (only *invalid output* becomes a clarification), because
  replying "I couldn't understand that" during an outage hides the outage behind a question
  the owner cannot answer. This service catches it, alerts, and stays silent.
* **A stuck `processing` row is never retried** — see `flag_stuck`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.container import AppContainer
from app.db.models import EmailThread, Item
from app.db.repositories import audit as audit_repo
from app.db.repositories import inbound as inbound_repo
from app.db.repositories import items as items_repo
from app.db.repositories import reminders as reminders_repo
from app.db.repositories import state as state_repo
from app.db.repositories import threads as threads_repo
from app.db.repositories.threads import OutboundKind
from app.domain.date_resolver import Resolution, resolve_date
from app.domain.intents import ALLOWED_STATUSES
from app.domain.planner import PlannerPolicy
from app.domain.types import (
    AuditEvent,
    InboundMessage,
    InterpretationContext,
    NormalizedItem,
    NotionPage,
    SourceDb,
)
from app.domain.validator import ApplyChange, NeedsClarification, NoChangeNeeded, validate
from app.integrations.gmail import compose, parse
from app.integrations.notion.normalize import normalize_page
from app.integrations.notion.writer import NotionWriter, WriteOutcome
from app.logging import get_logger
from app.services.alert_service import AlertService

# `system_state` key holding the Gmail history cursor (§2.3.5 names it exactly this).
GMAIL_HISTORY_KEY = "gmail_history_id"

# A `processing` row older than this was interrupted between claim and finish. §2.3.2.E:
# flagged and alerted, never silently retried.
STUCK_AFTER = timedelta(minutes=10)

_PAGE_MISSING_REASON = "the item no longer exists in Notion, so nothing was changed"


class InboundService:
    """Polls for replies and runs §2.3.2.E's pipeline over each one."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._log = get_logger(__name__)
        self._alerts = AlertService(container)

    # ── Polling (step 0: get the messages) ──────────────────────────────────

    async def poll_once(self) -> int:
        """Poll Gmail once and process every new message. Returns how many were processed.

        The cursor is stored even when the poll returned nothing. A poll that found no
        messages still advanced Gmail's history, and a pipeline that only saved the cursor
        when it found something would rescan the same window forever.
        """
        async with self._container.session_factory() as session:
            cursor = await state_repo.get_str(session, GMAIL_HISTORY_KEY)

        result = await self._container.mail.poll_new(cursor)

        if result.history_id is not None and result.history_id != cursor:
            async with self._container.session_factory() as session:
                await state_repo.set_str(session, GMAIL_HISTORY_KEY, result.history_id)
                await session.commit()

        processed = 0
        for message in result.messages:
            # One message's failure must not strand the rest of the batch — the same rule the
            # scheduler tick follows. `_process` handles its own errors and records them.
            if await self._process(message):
                processed += 1

        if result.messages:
            self._log.info("inbound_poll", polled=len(result.messages), processed=processed)
        return processed

    async def flag_stuck(self) -> int:
        """Alert on rows left `processing` past `STUCK_AFTER`. Returns how many were found.

        **These rows are never returned to a claimable state.** A row left `processing` means
        a reply was picked up and never finished, so the work may have partly happened; a
        retry could send a second confirmation or make a second write. §2.3.2.E chooses a
        loud, human-visible stall over a silent double effect.
        """
        now = self._container.clock.now()
        async with self._container.session_factory() as session:
            stuck = await inbound_repo.list_stuck(session, now=now, stuck_after=STUCK_AFTER)

        if not stuck:
            return 0

        async with self._container.session_factory() as session:
            await audit_repo.append(
                session,
                "inbound_stuck",
                payload={
                    "count": len(stuck),
                    "provider_message_ids": [row.provider_message_id for row in stuck],
                    "stuck_after_minutes": int(STUCK_AFTER.total_seconds() // 60),
                },
                result="alerted",
            )
            await session.commit()

        await self._alerts.alert(
            "inbound_stuck",
            f"{len(stuck)} inbound message(s) are stuck",
            "These replies were picked up but never finished, so the app will NOT retry them "
            "automatically (a retry could send a duplicate confirmation or make a second "
            "change). Check the audit log and Notion:\n\n"
            + "\n".join(f"- {row.provider_message_id}" for row in stuck),
        )
        self._log.warning("inbound_stuck", count=len(stuck))
        return len(stuck)

    # ── The per-message pipeline (§2.3.2.E steps 1-10) ──────────────────────

    async def _process(self, message: InboundMessage) -> bool:
        """Run one message through the pipeline. True when it was handled to a terminal state."""
        now = self._container.clock.now()

        # ── Step 1: dedupe. Nothing else may happen before this. ────────────
        async with self._container.session_factory() as session:
            claimed = await inbound_repo.claim(
                session,
                provider_message_id=message.provider_message_id,
                provider_thread_id=message.provider_thread_id,
            )
            await session.commit()

        if claimed is None:
            # Already handled — a re-delivered history record, a second poller, or an
            # overlapping deploy. Not an error, and not a reason to do anything.
            self._log.info("inbound_duplicate", provider_message_id=message.provider_message_id)
            return False

        try:
            return await self._handle(claimed.id, message, now)
        except Exception as exc:
            # The row stays `failed`, never `processing`: a `processing` row is the signal for
            # a *crash*, and dressing an ordinary failure as one would make `flag_stuck` cry
            # wolf. The exception is audited rather than re-raised so one bad message does not
            # abort the whole poll.
            await self._finish(
                claimed.id,
                status="failed",
                item_id=None,
                result=f"unhandled error: {type(exc).__name__}",
                error=f"{type(exc).__name__}: {exc}",
                now=now,
            )
            self._log.error(
                "inbound_failed",
                provider_message_id=message.provider_message_id,
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return False

    async def _handle(self, message_id: UUID, message: InboundMessage, now: datetime) -> bool:
        # ── Step 2: the sender allowlist, and no auto-replies. ──────────────
        if parse.is_auto_reply(message) or not self._sender_allowed(message):
            await self._audit(
                "inbound_ignored",
                item_id=None,
                message=message,
                payload={
                    "from": parse.parse_from_address(message.from_address),
                    "reason": "auto_reply"
                    if parse.is_auto_reply(message)
                    else "sender_not_allowed",
                },
                result="ignored",
            )
            await self._finish(
                message_id, status="ignored", item_id=None, result="ignored", now=now
            )
            return True

        # ── Step 3: resolve to exactly one item, by stored ids only. ────────
        async with self._container.session_factory() as session:
            thread = await self._resolve_thread(session, message)

        if thread is None:
            # Fail safe. Deliberately `inbound_unmapped` and not `inbound_ignored`: a sender
            # problem and a thread-mapping problem must never be conflated in the audit trail.
            await self._audit(
                "inbound_unmapped",
                item_id=None,
                message=message,
                payload={
                    "in_reply_to": message.in_reply_to,
                    "references": list(message.references),
                },
                result="unmapped",
            )
            await self._notify_unmapped(message)
            await self._finish(
                message_id, status="ignored", item_id=None, result="unmapped", now=now
            )
            return True

        async with self._container.session_factory() as session:
            item = await items_repo.get_by_id(session, thread.item_id)
        if item is None:
            # The thread maps to an item that has since been deleted; the cascade removed the
            # thread, so this is only reachable in a race. Treat it as unmappable.
            await self._audit(
                "inbound_unmapped",
                item_id=None,
                message=message,
                payload={"reason": "item_missing"},
                result="unmapped",
            )
            await self._notify_unmapped(message)
            await self._finish(
                message_id, status="ignored", item_id=None, result="unmapped", now=now
            )
            return True

        # ── Step 4: only the new reply text. ────────────────────────────────
        reply_text = parse.extract_reply_text(message.body_text)

        # ── Steps 5-9. ──────────────────────────────────────────────────────
        await self._interpret_and_apply(message_id, message, thread, item, reply_text, now)
        return True

    async def _interpret_and_apply(
        self,
        message_id: UUID,
        message: InboundMessage,
        thread: EmailThread,
        item: Item,
        reply_text: str,
        now: datetime,
    ) -> None:
        # ── Step 5: that one item's context, and any pending question. ──────
        pending = thread.pending_clarification or {}
        question = pending.get("question") if isinstance(pending, dict) else None

        context = InterpretationContext(
            reply_text=reply_text,
            item_name=item.name,
            item_course=item.course,
            status=item.status,
            due_date=item.due_date,
            allowed_statuses=ALLOWED_STATUSES,
            pending_clarification=question if isinstance(question, str) else None,
        )

        # ── Step 6: interpret. Transport failures are an outage, not a question. ──
        try:
            intent = await self._container.interpreter.interpret(context)
        except Exception as exc:
            await self._audit(
                "reply_interpreted",
                item_id=item.id,
                message=message,
                thread=thread,
                payload={"result": "interpreter_error"},
                error=f"{type(exc).__name__}: {exc}",
                result="error",
            )
            await self._alerts.alert(
                "deepseek_failed",
                "The reply interpreter is failing",
                "An inbound reply could not be interpreted, so nothing was changed. The app "
                "did NOT reply with a clarification, because the problem is the model, not "
                f"the question: {type(exc).__name__}.",
            )
            await self._finish(
                message_id,
                status="failed",
                item_id=item.id,
                result="interpreter_error",
                error=f"{type(exc).__name__}: {exc}",
                now=now,
            )
            return

        await self._audit(
            "reply_interpreted",
            item_id=item.id,
            message=message,
            thread=thread,
            payload={
                "action": str(intent.action),
                "status": str(intent.status) if intent.status is not None else None,
                "due_date_text": intent.due_date_text,
                "user_message": reply_text,
            },
            result="interpreted",
        )

        # ── Step 7: resolve the phrase and validate, deterministically. ─────
        resolved = self._resolve(intent.due_date_text, message, item)
        outcome = validate(
            intent,
            current_status=item.status,
            current_due_date=item.due_date,
            resolved=resolved,
        )

        if isinstance(outcome, NeedsClarification):
            await self._clarify(message_id, message, thread, item, outcome, reply_text, now)
            return

        if isinstance(outcome, NoChangeNeeded):
            # FR-10's "already set" path: tell the owner, and write nothing.
            await self._reply(
                thread=thread,
                kind="notice",
                message=message,
                rendered=compose.render_notice(
                    subject=f"[No change] {item.name}",
                    body=(
                        f"Nothing to do: {item.name} is already "
                        f"{_describe(item.status, item.due_date)}. Nothing was changed.\n"
                    ),
                ),
            )
            await self._clear_clarification(thread, now)
            await self._finish(
                message_id, status="done", item_id=item.id, result="no_change", now=now
            )
            return

        await self._write(message_id, message, thread, item, outcome, now)

    # ── Steps 8-9: the write, and telling the truth about it ────────────────

    async def _write(
        self,
        message_id: UUID,
        message: InboundMessage,
        thread: EmailThread,
        item: Item,
        change: ApplyChange,
        now: datetime,
    ) -> None:
        await self._audit(
            "notion_update_attempted",
            item_id=item.id,
            message=message,
            thread=thread,
            payload={
                "previous_value": {"status": item.status, "due_date": _iso(item.due_date)},
                "requested_value": {
                    "status": change.status,
                    "due_date": _iso(change.due_date),
                },
            },
            result="attempted",
        )

        outcome: WriteOutcome = await NotionWriter(self._container).apply(
            page_id=item.notion_page_id,
            source_db=item.source_db,
            status=change.status,
            due_date=change.due_date,
        )

        if outcome.no_change:
            # The write was skipped because Notion already held those values. Say so; do not
            # claim a change that did not happen.
            await self._audit(
                "notion_update_succeeded",
                item_id=item.id,
                message=message,
                thread=thread,
                payload={"result": "no_change"},
                result="no_change",
            )
            await self._reply(
                thread=thread,
                kind="notice",
                message=message,
                rendered=compose.render_notice(
                    subject=f"[No change] {item.name}",
                    body=f"Nothing to do: Notion already had those values for {item.name}.\n",
                ),
            )
            await self._clear_clarification(thread, now)
            await self._finish(
                message_id, status="done", item_id=item.id, result="no_change", now=now
            )
            return

        if not outcome.succeeded:
            await self._audit(
                "notion_update_failed",
                item_id=item.id,
                message=message,
                thread=thread,
                payload={
                    "reason": outcome.reason,
                    "attempts": outcome.attempts,
                    "wrote_done": outcome.wrote_done,
                    "due_date_was_range": outcome.due_date_was_range,
                },
                result="failed",
                error=outcome.reason,
            )
            await self._reply(
                thread=thread,
                kind="failure",
                message=message,
                rendered=compose.render_failure(
                    item_name=item.name,
                    reason=outcome.reason or _PAGE_MISSING_REASON,
                ),
            )
            await self._finish(
                message_id, status="done", item_id=item.id, result="write_failed", now=now
            )
            return

        # Verified. The confirmation quotes the read-back, never the request.
        await self._audit(
            "notion_update_succeeded",
            item_id=item.id,
            message=message,
            thread=thread,
            payload={
                "previous_value": _values(outcome.before),
                "final_value": _values(outcome.after),
                "wrote_done": outcome.wrote_done,
                "due_date_was_range": outcome.due_date_was_range,
                "attempts": outcome.attempts,
            },
            result="verified",
        )
        await self._apply_locally(item, now)
        await self._reply(
            thread=thread,
            kind="confirmation",
            message=message,
            rendered=compose.render_confirmation(
                item_name=item.name,
                status_change=_status_change(outcome.before, outcome.after),
                due_change=_due_change(outcome.before, outcome.after),
            ),
        )
        await self._clear_clarification(thread, now)
        await self._finish(message_id, status="done", item_id=item.id, result="applied", now=now)

    async def _apply_locally(self, item: Item, now: datetime) -> None:
        """Bring the local row in line with what Notion now holds, then re-plan reminders.

        This is the step that makes a date change actually take effect on reminders: without
        it the local `due_at` would stay on the old date until the next hourly sync, and the
        new schedule would not exist. The page is re-fetched rather than reconstructed from
        the write outcome so the stored row matches Notion exactly.
        """
        page = await self._container.notion.get_page(item.notion_page_id)
        if page is None:
            # The write verified, so the page existed moments ago. Losing it now is rare; the
            # next sync will deactivate it. Nothing to reconcile against, so leave the local
            # row alone rather than upserting a guess.
            self._log.warning(
                "inbound_local_refresh_page_missing", notion_page_id=item.notion_page_id
            )
            return

        live = self._normalize(page, cast(SourceDb, item.source_db))
        # The write surface is Status/Done/Due Date, so the Course relation cannot have
        # changed; carrying the stored name keeps whatever the courses cache had resolved
        # (normalization alone cannot produce a display name — §2.3.2.A).
        if live.course_page_id == item.course_page_id:
            live = replace(live, course=item.course)

        async with self._container.session_factory() as session:
            stored = await items_repo.upsert(session, live)
            # `upsert` writes through a Core statement and re-selects; without this refresh a
            # row already in the identity map keeps its previous `due_at` and the planner
            # would build the new schedule from the old date. See `SyncService._upsert_page`.
            await session.refresh(stored)
            await reminders_repo.reconcile_reminders(session, stored, now, self._policy)
            await session.commit()

    # ── The clarification loop ──────────────────────────────────────────────

    async def _clarify(
        self,
        message_id: UUID,
        message: InboundMessage,
        thread: EmailThread,
        item: Item,
        outcome: NeedsClarification,
        reply_text: str,
        now: datetime,
    ) -> None:
        # A closed thread gets exactly one more thing: a pointer at Notion. No new question,
        # no write — the budget is spent (§2.3.2.E).
        if thread.state == "closed":
            await self._reply(
                thread=thread,
                kind="notice",
                message=message,
                rendered=compose.render_notice(
                    subject=f"[Closed] {item.name}",
                    body=(
                        f"This conversation is closed, so I did not change anything for "
                        f"{item.name}. Please change it in Notion directly.\n"
                        f"\n{_notion_link(item)}"
                    ),
                ),
            )
            await self._finish(
                message_id, status="done", item_id=item.id, result="closed_thread", now=now
            )
            return

        rendered = compose.render_clarification(item_name=item.name, question=outcome.question)
        await self._reply(
            thread=thread,
            kind="clarification",
            message=message,
            rendered=rendered,
        )

        await self._audit(
            "clarification_requested",
            item_id=item.id,
            message=message,
            thread=thread,
            payload={"question": outcome.question, "user_message": reply_text},
            result="asked",
        )

        async with self._container.session_factory() as session:
            rounds = await threads_repo.bump_clarification_rounds(session, thread.id, now=now)
            exhausted = rounds >= self._container.settings.max_clarification_rounds
            await threads_repo.set_state(
                session,
                thread.id,
                state="closed" if exhausted else "awaiting_clarification",
                now=now,
            )
            await threads_repo.set_pending_clarification(
                session,
                thread.id,
                pending={"question": outcome.question, "original_request": reply_text},
                now=now,
            )
            await session.commit()

        await self._finish(message_id, status="done", item_id=item.id, result="clarified", now=now)

    async def _clear_clarification(self, thread: EmailThread, now: datetime) -> None:
        """A reply that was understood answers whatever question was outstanding."""
        if thread.pending_clarification is None and thread.state != "awaiting_clarification":
            return
        async with self._container.session_factory() as session:
            await threads_repo.set_pending_clarification(session, thread.id, pending=None, now=now)
            if thread.state == "awaiting_clarification":
                await threads_repo.set_state(session, thread.id, state="open", now=now)
            await session.commit()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _sender_allowed(self, message: InboundMessage) -> bool:
        """Exact, case-insensitive membership of `ALLOWED_REPLY_SENDERS` (FR-11)."""
        return (
            parse.parse_from_address(message.from_address) in self._container.settings.reply_senders
        )

    async def _resolve_thread(
        self, session: AsyncSession, message: InboundMessage
    ) -> EmailThread | None:
        """Step 3's two-step resolution: thread id first, then the reply headers.

        Both paths are `UNIQUE` lookups against ids *we* stored, which is where "a reply maps
        to exactly one item, never by name" comes from.
        """
        if message.provider_thread_id:
            thread = await threads_repo.get_by_provider_thread_id(
                session, message.provider_thread_id
            )
            if thread is not None:
                return thread

        candidates = (*message.references, *((message.in_reply_to,) if message.in_reply_to else ()))
        for rfc_message_id in candidates:
            outbound = await threads_repo.get_outbound_by_rfc_message_id(session, rfc_message_id)
            if outbound is not None:
                return await threads_repo.get_thread_by_id(session, outbound.thread_id)
        return None

    def _resolve(
        self, phrase: str | None, message: InboundMessage, item: Item
    ) -> Resolution | None:
        """The date phrase, resolved deterministically against the email's own timestamp.

        `None` when the intent did not name a date — the validator treats that as "no date
        part", not as an unresolvable one.
        """
        if not phrase:
            return None
        settings = self._container.settings
        return resolve_date(
            phrase,
            message.received_at,
            settings.tz,
            current_due=item.due_date,
            max_shift_days=settings.max_date_shift_days,
        )

    async def _reply(
        self,
        *,
        thread: EmailThread,
        kind: OutboundKind,
        rendered: tuple[str, str],
        message: InboundMessage,
    ) -> None:
        """Send one message **inside** the existing conversation and record it.

        Staying in the thread is what keeps the next reply mappable. The `outbound_messages`
        row is what makes the `References` fallback work if the thread id is ever lost, so
        both halves are written together.
        """
        subject, body = rendered
        sent = await self._container.mail.send(
            to=self._container.settings.reminder_recipient,
            subject=subject,
            body=body,
            thread_id=thread.provider_thread_id,
            in_reply_to=message.in_reply_to,
        )
        async with self._container.session_factory() as session:
            await threads_repo.add_outbound(
                session,
                thread_id=thread.id,
                kind=kind,
                provider_message_id=sent.provider_message_id,
                rfc_message_id=sent.rfc_message_id,
            )
            await session.commit()

    async def _notify_unmapped(self, message: InboundMessage) -> None:
        """One plain notice for a reply that could not be mapped to an item (§2.3.2.E step 3).

        Sent as its own new thread with no `email_threads` row, because there is no item to
        attach one to. The alternative — guessing an item from the subject or the sender —
        is exactly what step 3 forbids.
        """
        subject, body = compose.render_notice(
            subject="I couldn't tell which item this refers to",
            body=(
                "I couldn't tell which item your reply refers to, so I did not change "
                "anything.\n\nReply directly to a reminder email and I'll know which "
                "deadline you mean.\n"
            ),
        )
        await self._container.mail.send(
            to=self._container.settings.reminder_recipient,
            subject=subject,
            body=body,
            thread_id=None,
            in_reply_to=message.in_reply_to,
        )

    async def _finish(
        self,
        message_id: UUID,
        *,
        status: inbound_repo.InboundStatus,
        item_id: UUID | None,
        result: str,
        now: datetime,
        error: str | None = None,
    ) -> None:
        async with self._container.session_factory() as session:
            await inbound_repo.finish(
                session, message_id, status=status, item_id=item_id, result=result, now=now
            )
            await session.commit()
        if error is not None:
            self._log.warning("inbound_message_failed", result=result, error=error)

    async def _audit(
        self,
        event_type: AuditEvent,
        *,
        item_id: UUID | None,
        message: InboundMessage,
        payload: dict[str, object],
        thread: EmailThread | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._container.session_factory() as session:
            await audit_repo.append(
                session,
                event_type,
                item_id=item_id,
                provider_message_id=message.provider_message_id,
                provider_thread_id=message.provider_thread_id,
                payload=payload,
                result=result,
                error=error,
            )
            await session.commit()

    def _normalize(self, page: NotionPage, source_db: SourceDb) -> NormalizedItem:
        settings = self._container.settings
        return normalize_page(
            page,
            source_db=source_db,
            tz=settings.tz,
            default_due_time=settings.default_due_time,
            prop_course=settings.notion_prop_course,
            prop_type=settings.notion_prop_type,
            prop_due=settings.notion_prop_due,
            prop_status=settings.notion_prop_status,
            prop_done=settings.notion_prop_done,
        )

    @property
    def _policy(self) -> PlannerPolicy:
        return PlannerPolicy(
            completed_status_value=self._container.settings.notion_status_completed
        )


# ── Small pure helpers ──────────────────────────────────────────────────────


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _values(values: object) -> dict[str, object] | None:
    """A `NotionValues` as a JSON-safe audit payload."""
    if values is None:
        return None
    return {
        "status": getattr(values, "status", None),
        "done": getattr(values, "done", None),
        "due_date": _iso(getattr(values, "due_date", None)),
    }


def _status_change(before: object, after: object) -> tuple[str, str] | None:
    """The `(old, new)` status pair, or None when the status did not change."""
    old = getattr(before, "status", None)
    new = getattr(after, "status", None)
    if old is None or new is None or old == new:
        return None
    return (str(old), str(new))


def _due_change(before: object, after: object) -> tuple[str, str] | None:
    """The `(old, new)` due-date pair, or None when the due date did not change."""
    old = _iso(getattr(before, "due_date", None))
    new = _iso(getattr(after, "due_date", None))
    if old is None and new is None:
        return None
    if old == new:
        return None
    return (old or "(none)", new or "(none)")


def _describe(status: str, due_date: object) -> str:
    due = _iso(due_date)
    return f"{status}" + (f", due {due}" if due else ", with no due date")


def _notion_link(item: Item) -> str:
    return item.notion_url or "(no link)"
