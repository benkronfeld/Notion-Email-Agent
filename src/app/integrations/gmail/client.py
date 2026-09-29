"""The `MailClient` port (spec §2.3.6).

**Frozen interface.** Mirrors §2.3.6, with the same async refinement as `NotionClient`.

The MVP uses `send` (phase 3) and `find_sent_by_token` (stale-claim recovery, phase 3).
`poll_new` is V1 (phase 4), implemented here against `parse.py` and `poller.py`.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any, Protocol
from uuid import uuid4

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import Settings
from app.domain.types import InboundMessage, PollResult, SentMessage
from app.integrations.gmail.parse import parse_message_payload
from app.integrations.gmail.poller import (
    history_message_ids,
    new_history_id,
    search_query_fallback,
)


class MailClient(Protocol):
    """Outbound mail, plus the V1 inbound poll."""

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None,
        in_reply_to: str | None,
    ) -> SentMessage:
        """Send one plain-text message and return its provider identifiers.

        `thread_id=None` starts a new Gmail thread, which is what every reminder does
        (one item per email, FR-6). The From address comes from configuration, not from
        an argument.
        """
        ...

    async def poll_new(self, history_id: str | None) -> PollResult:
        """Inbound mail since `history_id`, newest cursor included (V1, build phase 4).

        `history_id` is the cursor stored from the previous poll, or `None` on the first
        run. The returned `PollResult.history_id` is the cursor to store for next time and
        is always present, even when no messages came back — the history window has still
        advanced. Callers must store it; the sender allowlist, dedupe, and item mapping all
        happen downstream, never here.
        """
        ...

    async def find_sent_by_token(self, token: str) -> SentMessage | None:
        """Look up a previously sent message by the `ref:<token>` footer in its body.

        This is how stale-claim recovery answers "did the email actually go out before we
        crashed?" — a duplicate send is preferred over a silently lost reminder.
        """
        ...


# ── Implementation (build phase 3) ───────────────────────────────────────────
#
# Nothing here may be constructed or called by a test (CLAUDE.md constraint 5): every
# method below reaches the live Gmail API. Tests exercise `compose.py` and fake adapters.

GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)

TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

# How many Sent hits to inspect before giving up. The footer token is unique per reminder,
# so more than one hit means a duplicate send, and the first verified one is the answer.
_SENT_SEARCH_LIMIT = 10

# Page size for the 404 fallback search. The query itself is bounded (`in:inbox
# newer_than:2d`); this only controls how many pages that bounded result takes.
_SEARCH_PAGE_SIZE = 100


class GmailClient:
    """`MailClient` over the Gmail REST API, authenticated by a stored refresh token.

    The underlying `googleapiclient` calls are blocking, so each one is dispatched with
    `asyncio.to_thread` — the app runs on an async event loop and a synchronous HTTP call
    here would stall the scheduler tick.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Built on first use, so constructing the client performs no network I/O and
        # cannot fail because of a bad credential until something is actually sent.
        self._service: Any | None = None

    # ── MailClient ──────────────────────────────────────────────────────────

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None,
        in_reply_to: str | None,
    ) -> SentMessage:
        return await asyncio.to_thread(self._send_sync, to, subject, body, thread_id, in_reply_to)

    async def find_sent_by_token(self, token: str) -> SentMessage | None:
        return await asyncio.to_thread(self._find_sent_by_token_sync, token)

    async def poll_new(self, history_id: str | None) -> PollResult:
        """Inbound mail since `history_id` (V1, build phase 4); see the port docstring.

        Runs the blocking History API calls on a worker thread so a poll never stalls the
        event loop.
        """
        return await asyncio.to_thread(self._poll_new_sync, history_id)

    # ── Internals ───────────────────────────────────────────────────────────

    def _service_or_build(self) -> Any:
        if self._service is None:
            # google-auth ships `py.typed` but leaves `Credentials.__init__` unannotated,
            # so strict mypy flags the call. Narrow and deliberate: the arguments below
            # are all checked at runtime by google-auth itself.
            credentials = Credentials(  # type: ignore[no-untyped-call]
                token=None,  # forces a refresh on the first call
                refresh_token=self._settings.gmail_refresh_token,
                token_uri=TOKEN_URI,
                client_id=self._settings.gmail_client_id,
                client_secret=self._settings.gmail_client_secret,
                scopes=list(GMAIL_SCOPES),
            )
            self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None,
    ) -> tuple[EmailMessage, str]:
        """A plain-text MIME message plus the RFC `Message-ID` we generated for it.

        The `Message-ID` is kept so the V1 reply resolver can match `In-Reply-To` against
        `outbound_messages.rfc_message_id` (§2.3.2.E step 3b). It is derived from a UUID
        rather than `email.utils.make_msgid` so nothing here reads the system clock.
        """
        sender = self._settings.gmail_sender_address
        domain = sender.rpartition("@")[2] or "localhost"
        rfc_message_id = f"<{uuid4().hex}@{domain}>"

        message = EmailMessage()
        message["To"] = to
        message["From"] = sender
        message["Subject"] = subject
        message["Message-ID"] = rfc_message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)  # no subtype -> text/plain, UTF-8
        return message, rfc_message_id

    def _send_sync(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None,
        in_reply_to: str | None,
    ) -> SentMessage:
        message, rfc_message_id = self._build_message(to, subject, body, in_reply_to)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        request: dict[str, Any] = {"raw": raw}
        if thread_id is not None:
            # Passing threadId is what keeps a reply in the same Gmail thread. Every
            # reminder passes None, which starts a new thread (one item per email, FR-6).
            request["threadId"] = thread_id

        sent = self._service_or_build().users().messages().send(userId="me", body=request).execute()
        return SentMessage(
            provider_message_id=str(sent["id"]),
            provider_thread_id=str(sent.get("threadId", "")),
            rfc_message_id=rfc_message_id,
        )

    def _find_sent_by_token_sync(self, token: str) -> SentMessage | None:
        marker = f"ref: {token}"
        service = self._service_or_build()
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=f'in:sent "{token}"', maxResults=_SENT_SEARCH_LIMIT)
            .execute()
        )
        for entry in listing.get("messages") or []:
            message_id = str(entry["id"])
            message = (
                service.users().messages().get(userId="me", id=message_id, format="full").execute()
            )
            payload = message.get("payload") or {}
            # The search query is only a hint (Gmail tokenises it); the body is the proof.
            if marker not in _plain_text_body(payload):
                continue
            return SentMessage(
                provider_message_id=str(message.get("id", message_id)),
                provider_thread_id=str(message.get("threadId", "")),
                rfc_message_id=_header(payload, "Message-ID"),
            )
        return None

    # ── Inbound poll (V1, build phase 4) ─────────────────────────────────────

    def _poll_new_sync(self, history_id: str | None) -> PollResult:
        service = self._service_or_build()
        if history_id is None:
            message_ids, cursor = self._fallback_sync(service)
        else:
            try:
                response = self._history_response_sync(service, history_id)
            except HttpError as exc:
                if _status_code(exc) != 404:
                    # Anything else is a real failure. Swallowing it would make a broken
                    # poll look like "nothing new" — the silent-failure mode this project
                    # exists to avoid — so it propagates.
                    raise
                # The stored id has aged out of Gmail's history window; the search below is
                # bounded, and `processed_inbound_messages` makes its re-deliveries safe.
                message_ids, cursor = self._fallback_sync(service)
            else:
                message_ids = history_message_ids(response)
                # The cursor advances even when no messages came back: the history window
                # has still moved, and keeping the old id would re-scan it forever.
                cursor = new_history_id(response) or history_id

        messages = tuple(
            self._fetch_inbound_sync(service, message_id)
            for message_id in dict.fromkeys(message_ids)  # history/search can repeat an id
        )
        return PollResult(messages=messages, history_id=cursor)

    def _fallback_sync(self, service: Any) -> tuple[list[str], str | None]:
        """The bounded-search path, plus the cursor to resume history from.

        The profile is read *before* the search: a message that arrives between the two is
        caught by the search, and one that arrives after it is caught by the next history
        poll from this cursor.
        """
        cursor = self._profile_history_id_sync(service)
        return self._search_message_ids_sync(service), cursor

    def _history_response_sync(self, service: Any, history_id: str) -> Mapping[str, Any]:
        """Every `history.list` page merged into one response dict.

        Merging keeps the pagination loop out of `poller.py`: its pure helpers see the same
        shape whether the window fit on one page or ten.
        """
        merged: dict[str, Any] = {"history": []}
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "userId": "me",
                "startHistoryId": history_id,
                "historyTypes": ["messageAdded"],
            }
            if page_token is not None:
                params["pageToken"] = page_token
            response = service.users().history().list(**params).execute()
            merged["history"].extend(response.get("history") or [])
            if response.get("historyId") is not None:
                merged["historyId"] = response["historyId"]
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return merged

    def _search_message_ids_sync(self, service: Any) -> list[str]:
        """Every message id the bounded fallback query returns, across all pages."""
        message_ids: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "userId": "me",
                "q": search_query_fallback(),
                "maxResults": _SEARCH_PAGE_SIZE,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            response = service.users().messages().list(**params).execute()
            for entry in response.get("messages") or []:
                if entry.get("id") is not None:
                    message_ids.append(str(entry["id"]))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return message_ids

    def _profile_history_id_sync(self, service: Any) -> str | None:
        """The mailbox's current `historyId`, used to seed or reset the poll cursor."""
        profile = service.users().getProfile(userId="me").execute()
        value = profile.get("historyId")
        return str(value) if value is not None else None

    def _fetch_inbound_sync(self, service: Any, message_id: str) -> InboundMessage:
        message = (
            service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        return parse_message_payload(message)


def _status_code(exc: HttpError) -> int | None:
    """The HTTP status carried by a `googleapiclient` `HttpError`, or `None` if unreadable.

    `HttpError.resp` is the `httplib2` response; its `.status` is what distinguishes an
    expired history cursor (404) from a failure that must propagate.
    """
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def _header(payload: Any, name: str) -> str:
    """One header's value from a `format="full"` payload, or `""`."""
    wanted = name.lower()
    for header in payload.get("headers") or []:
        if str(header.get("name", "")).lower() == wanted:
            return str(header.get("value", ""))
    return ""


def _plain_text_body(payload: Any) -> str:
    """The decoded `text/plain` body of a message payload, best effort.

    Walks the MIME tree for a `text/plain` part and falls back to the first part that
    decodes to anything, so a message Gmail stored as multipart/alternative is still
    searchable for the footer.
    """
    data = (payload.get("body") or {}).get("data")
    if data:
        return _decode_base64url(str(data))
    parts = payload.get("parts") or []
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = _plain_text_body(part)
            if text:
                return text
    for part in parts:
        text = _plain_text_body(part)
        if text:
            return text
    return ""


def _decode_base64url(data: str) -> str:
    padding = "=" * (-len(data) % 4)  # Gmail omits base64 padding
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
