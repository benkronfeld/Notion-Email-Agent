"""Production wiring: turn `Settings` into a real `AppContainer` (spec §2.3.1).

`create_app` has always taken a container rather than building one, which is what lets the
whole test suite boot the real application over fake ports. The consequence — discovered
while starting V1 — was that **nothing constructed the real one**, so the documented
`uvicorn app.main:app` had no `app` to serve and the service could not start at all. This
module is the missing half of that seam, and `asgi.py` is the entrypoint that calls it.

**It takes settings as an argument and never reads the environment itself.** That is
deliberate: `.env` holds real secrets (constraint 2) and, more sharply, a test process that
imported a module calling `get_settings()` would load the production `DATABASE_URL` and
`REMINDER_RECIPIENT` into memory — the exact accident constraints 1 and 2 exist to prevent.
Reading the environment happens in exactly one place, `asgi.py`, which no test imports.

Every adapter here is constructed but performs no network I/O until first use:
`GmailClient` builds its service lazily, `DeepSeekInterpreter` builds its client lazily, and
`create_async_engine` connects on first checkout. So building a container at import time
cannot fail because a credential is wrong, and cannot make a request.
"""

from __future__ import annotations

from app.clock import SystemClock
from app.config import Settings
from app.container import AppContainer
from app.db.session import create_engine, create_session_factory
from app.integrations.gmail.client import GmailClient
from app.integrations.llm.deepseek import DeepSeekInterpreter
from app.integrations.notion.client import NotionRestClient


def build_container(settings: Settings) -> AppContainer:
    """The real adapter set, wired once at startup and shared by every service and route.

    Ports are swappable by construction: a test calls this same function's fake counterpart
    (`fixtures.fakes.make_container`), so the container is the only thing that differs
    between production and the suite.
    """
    return AppContainer(
        settings=settings,
        clock=SystemClock(settings.timezone),
        session_factory=create_session_factory(create_engine(settings)),
        notion=NotionRestClient(settings),
        mail=GmailClient(settings),
        interpreter=DeepSeekInterpreter(settings),
    )
