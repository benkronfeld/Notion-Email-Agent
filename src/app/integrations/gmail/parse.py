"""Inbound Gmail parsing (spec §2.3.2.E steps 2 and 4).

Pure functions over plain strings and dicts: nothing here touches the network, the clock,
or config, so every rule below is testable from a literal Gmail response. The adapter
(`client.GmailClient.poll_new`) only moves bytes; deciding what a `From:` header means, or
where the new reply text starts, lives here where a test can pin it down.

Two of these functions are security-relevant, not cosmetic:

- `parse_from_address` is what the sender allowlist compares against (FR-11), so it
  lowercases: `Ben@Example.COM` and `ben@example.com` must be the same sender.
- `is_auto_reply` is what keeps a vacation responder or a mailing list from being read as
  the owner's instruction (§2.3.2.E step 2).

The MIME-tree walk is duplicated from `client.py` rather than imported: `client` imports
`parse_message_payload` from this module, so importing `client`'s private helpers here would
close a circular import. The duplication is three short functions whose contract
(`text/plain`, base64url, UTF-8 with replacement) is fixed by what Gmail returns.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.domain.types import InboundMessage

# A `From:` value is either `Display Name <addr@host>` or a bare address. The angle-bracket
# form wins when present, because a display name may itself contain an `@`.
_ANGLE_ADDRESS_RE = re.compile(r"<([^<>]+)>")

# RFC 5322 `References`/`In-Reply-To` are one or more `<id@host>` tokens, possibly folded
# across lines. Capture the inside and re-wrap, so incidental whitespace is stripped.
_MESSAGE_ID_RE = re.compile(r"<([^<>]+)>")

# Gmail's attribution line: `On Mon, Sep 28, 2026 at 3:00 PM Alice <a@b.com> wrote:`. Anchor
# on " wrote:" at end of line so a body sentence mentioning "wrote" is not a false cut.
_ATTRIBUTION_RE = re.compile(r"^\s*On\s.+\swrote:\s*$", re.IGNORECASE)
# Outlook's separator between a reply and the quoted message.
_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
# The RFC 3676 signature delimiter: exactly `--` or `-- ` on its own line.
_SIGNATURE_RE = re.compile(r"^--\s*$")

# `Precedence` values that mark bulk/automated mail (RFC 3834 and its predecessors).
_AUTO_REPLY_PRECEDENCE: frozenset[str] = frozenset({"bulk", "auto_reply", "junk"})


def extract_reply_text(body: str) -> str:
    """The owner's new text only, with quoted history and the signature removed (§2.3.2.E step 4).

    Only the new text may reach the interpreter: a quoted reminder body is the system's own
    words, and letting the model see them invites it to "resolve" a date phrase that the
    owner never typed.

    Conservative by construction:

    - `>`-prefixed lines are dropped wherever they appear, so an inline reply keeps its own
      text even when it sits between quoted paragraphs.
    - The first `On ... wrote:` / `-----Original Message-----` / `-- ` line cuts everything
      after it (signature delimiter, quoting boundary).
    - If nothing would remain — a reply that was *only* a quote, or an empty body — the
      original text is returned. An empty string must never be what reaches the interpreter;
      a clarification prompt is the correct outcome, and the pipeline can only produce one
      from real text.
    """
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        if _is_cut_marker(line):
            break
        kept.append(line)

    kept = [line for line in kept if not line.lstrip().startswith(">")]
    text = _trim_blank_lines([line.rstrip() for line in kept])
    if text:
        return text
    return body.strip()


def parse_from_address(raw: str) -> str:
    """The lowercased email address in a `From:` header value.

    Handles both `Name <a@b.com>` and bare `a@b.com`. The result is what the sender
    allowlist compares (FR-11, §2.3.2.E step 2), and mail clients vary the case, so the
    comparison is done on the normalised form rather than the raw header.
    """
    match = _ANGLE_ADDRESS_RE.search(raw)
    address = match.group(1) if match is not None else raw
    return address.strip().lower()


def is_auto_reply(message: InboundMessage) -> bool:
    """True when the message is automated and must not be read as the owner's instruction.

    `Auto-Submitted` marks a reply when present and not `no` (RFC 3834); a `Precedence` of
    `bulk`/`auto_reply`/`junk` marks a mailing list or an autoresponder. Both are checked
    case-insensitively. An absent or empty header is not a marker.
    """
    auto_submitted = (message.auto_submitted or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    precedence = (message.precedence or "").strip().lower()
    return precedence in _AUTO_REPLY_PRECEDENCE


def parse_rfc_message_ids(raw: str | None) -> tuple[str, ...]:
    """Split a `References` (or `In-Reply-To`) header into its individual `<...>` ids.

    Order is preserved — the resolver walks the chain newest-first — and whitespace inside
    the brackets is stripped so a folded header still yields clean ids. `None`/empty -> `()`.
    """
    if not raw:
        return ()
    return tuple(f"<{inner.strip()}>" for inner in _MESSAGE_ID_RE.findall(raw))


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """A header value by name, case-insensitively; `None` when the header is absent."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def parse_message_payload(message: Mapping[str, Any]) -> InboundMessage:
    """One `users.messages.get(format="full")` response as an `InboundMessage`.

    `internalDate` is Gmail's own epoch **milliseconds**, so it is the only authoritative
    timestamp for the message; it is decoded to an aware UTC datetime and never read from
    the system clock (CLAUDE.md constraint 4). `body_text` is the *whole* decoded
    `text/plain` body — stripping quoted history stays in `extract_reply_text`, deliberately,
    so the stripping rules are testable without synthesizing a Gmail payload.
    """
    payload = message.get("payload") or {}
    headers = {
        str(header.get("name", "")): str(header.get("value", ""))
        for header in payload.get("headers") or []
    }

    thread_id = message.get("threadId")
    return InboundMessage(
        provider_message_id=str(message["id"]),
        provider_thread_id=str(thread_id) if thread_id is not None else None,
        from_address=parse_from_address(header_value(headers, "From") or ""),
        subject=header_value(headers, "Subject") or "",
        in_reply_to=_optional(header_value(headers, "In-Reply-To")),
        references=parse_rfc_message_ids(header_value(headers, "References")),
        body_text=_plain_text_body(payload),
        received_at=_internal_date(message["internalDate"]),
        auto_submitted=_optional(header_value(headers, "Auto-Submitted")),
        precedence=_optional(header_value(headers, "Precedence")),
    )


# ── Internals ───────────────────────────────────────────────────────────────


def _optional(value: str | None) -> str | None:
    """An absent or whitespace-only header is `None`, never an empty string."""
    if value is None:
        return None
    return value.strip() or None


def _internal_date(raw: Any) -> datetime:
    """Gmail's `internalDate` (epoch milliseconds, as a string) -> aware UTC datetime."""
    return datetime.fromtimestamp(int(str(raw)) / 1000, tz=UTC)


def _is_cut_marker(line: str) -> bool:
    """A line where the owner's new text ends and quoted history/signature begins."""
    return bool(
        _ATTRIBUTION_RE.match(line) or _ORIGINAL_MESSAGE_RE.match(line) or _SIGNATURE_RE.match(line)
    )


def _trim_blank_lines(lines: list[str]) -> str:
    """Join lines with LF, dropping leading and trailing blank lines but keeping inner ones."""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _plain_text_body(payload: Mapping[str, Any]) -> str:
    """The decoded `text/plain` body of a message payload, best effort.

    Mirrors `client._plain_text_body`: a non-multipart part carries its data directly, a
    `multipart/alternative` is searched for its `text/plain` child first, and anything else
    falls back to the first child that decodes to text.
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
    """Gmail's URL-safe base64 without padding, decoded as UTF-8 with replacement."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
