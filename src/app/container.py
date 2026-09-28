"""`AppContainer` — the dependency-injection seam.

Every external dependency is a port held on this container so the whole set can be
swapped for fakes. Tests construct a container of fakes and boot the real app around it;
no test ever constructs a real Notion or Gmail adapter (CLAUDE.md constraint 5).

The field list is a frozen interface: it is fixed before parallel work begins so that no
two workstreams both add and consume a field.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.config import Settings
from app.integrations.gmail.client import MailClient
from app.integrations.llm.interpreter import IntentInterpreter
from app.integrations.notion.client import NotionClient


@dataclass(frozen=True, slots=True)
class AppContainer:
    settings: Settings
    clock: Clock
    session_factory: async_sessionmaker[AsyncSession]
    notion: NotionClient
    mail: MailClient
    interpreter: IntentInterpreter
