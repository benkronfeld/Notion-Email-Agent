"""Operator CLI — `python -m app.cli`.

One-off tools a human runs at a terminal, never application logic. Currently:

- `gmail-auth`   — the one-time installed-app OAuth consent flow (build phase 3).
- `--check-config` — report which required settings are present, **by name only**.

Output goes through `sys.stdout.write` rather than `print`, so ruff's `T20` rule stays
enabled for all of `src/` and `pyproject.toml` needs no per-file ignore.

This module never reads `.env` directly and never prints a stored value (CLAUDE.md
constraint 2). `--check-config` builds `Settings`, which loads `.env` for the app the same
way the app itself does, and reports presence/absence of names only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from app.config import Settings
from app.integrations.gmail.client import AUTH_URI, GMAIL_SCOPES, TOKEN_URI

# The settings with no default. A missing one of these stops the app at startup.
REQUIRED_SETTINGS: tuple[str, ...] = (
    "database_url",
    "reminder_recipient",
    "admin_api_token",
    "notion_token",
    "notion_db_assignments_readings",
    "notion_db_exams_projects",
    "gmail_sender_address",
    "gmail_client_id",
    "gmail_client_secret",
    "gmail_refresh_token",
)


def _write(text: str) -> None:
    sys.stdout.write(text)


def _check_config() -> int:
    """Print `present`/`MISSING` per required setting name. Never prints a value."""
    invalid: dict[str, str] = {}
    try:
        Settings()
    except ValidationError as exc:
        for error in exc.errors():
            loc = error.get("loc") or ("",)
            invalid[str(loc[0]).lower()] = str(error.get("type", "invalid"))

    _write("Required settings (names only — no values are printed):\n")
    for name in REQUIRED_SETTINGS:
        if name in invalid:
            _write(f"  MISSING  {name}  ({invalid[name]})\n")
        else:
            _write(f"  present  {name}\n")

    unexpected = sorted(name for name in invalid if name not in REQUIRED_SETTINGS)
    for name in unexpected:
        _write(f"  INVALID  {name}  ({invalid[name]})\n")

    if invalid:
        _write("\nAt least one required setting is missing or invalid.\n")
        return 1
    _write("\nAll required settings are present.\n")
    return 0


def _gmail_auth() -> int:
    """Run the OAuth consent flow and print a refresh token for GMAIL_REFRESH_TOKEN.

    The consent flow needs only the OAuth client id/secret, so those are checked first;
    the refresh token being absent is expected (obtaining it is the point).
    """
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = sorted({str((error.get("loc") or ("",))[0]).lower() for error in exc.errors()})
        _write(
            "Cannot start the OAuth flow: settings are incomplete.\n"
            f"Missing or invalid: {', '.join(missing)}\n"
            "Run `python -m app.cli --check-config` for the full list.\n"
        )
        return 1

    # Imported lazily: `google_auth_oauthlib` is only needed for this one operator tool.
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=list(GMAIL_SCOPES))

    _write(
        "Opening a browser for the Google consent screen.\n"
        f"Sign in as {settings.gmail_sender_address} and grant: {', '.join(GMAIL_SCOPES)}\n"
        "The consent screen must be in 'In production' status, or the refresh token will\n"
        "expire in about 7 days and reminders will stop silently (Appendix D).\n\n"
    )

    # Real wall-clock time is used here on purpose: this is a one-off interactive operator
    # tool, not application logic. The OAuth token lifetime is part of the handshake and
    # cannot be injected, and nothing in the scheduler ever calls this path, so the
    # injected-Clock rule (CLAUDE.md constraint 4) does not apply.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    refresh_token = credentials.refresh_token
    if not refresh_token:
        _write(
            "No refresh token was returned. Revoke the app's access at\n"
            "https://myaccount.google.com/permissions and run this command again.\n"
        )
        return 1

    # Deliberate: this prints a credential the operator just minted, so they can store it
    # in .env. It is never a value read back out of .env or out of Gmail.
    _write("\nStore this line in .env (it is shown once):\n\n")
    _write(f"GMAIL_REFRESH_TOKEN={refresh_token}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Operator tools for notion-email-agent. Nothing here runs in the app.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Report which required settings are present, by name only (never prints values).",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser(
        "gmail-auth",
        help="Run the one-time OAuth consent flow and print a GMAIL_REFRESH_TOKEN.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_config:
        return _check_config()
    if args.command == "gmail-auth":
        return _gmail_auth()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
