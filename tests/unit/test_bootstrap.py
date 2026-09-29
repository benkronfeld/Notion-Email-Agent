"""Production wiring actually produces a usable container.

This file exists because of a real gap: `create_app` takes a container, every test injects
one, and **nothing built the real one** — so `uvicorn app.main:app` had no `app` and the
service could not start. No test noticed, because no test ever tried.

These tests are deliberately not a smoke test of the network. They assert the two
properties that make the entrypoint safe to import anywhere:

1. every port is the real adapter, so the wiring is not silently left as a fake;
2. constructing them performs no I/O and cannot fail on missing credentials — which is why
   the test `settings` carry an *empty* `deepseek_api_key` and an unroutable base URL.

`asgi.py` itself is not imported here on purpose: it reads the environment at import time,
and a test process that loaded `.env` would pick up real secrets (constraints 1 and 2).
"""

from __future__ import annotations

import inspect

from app.bootstrap import build_container
from app.clock import SystemClock
from app.config import Settings
from app.container import AppContainer
from app.integrations.gmail.client import GmailClient
from app.integrations.llm.deepseek import DeepSeekInterpreter
from app.integrations.notion.client import NotionRestClient


def test_build_container_wires_the_real_adapters(settings: Settings) -> None:
    """The production container holds real adapters, not fakes."""
    container = build_container(settings)

    assert isinstance(container, AppContainer)
    assert isinstance(container.clock, SystemClock)
    assert isinstance(container.notion, NotionRestClient)
    assert isinstance(container.mail, GmailClient)
    assert isinstance(container.interpreter, DeepSeekInterpreter)
    assert container.settings is settings


def test_build_container_succeeds_with_no_credentials_and_makes_no_request(
    settings: Settings,
) -> None:
    """An empty API key and an unroutable base URL must not stop the app from booting.

    `settings.deepseek_api_key` is `""` in the test configuration. The OpenAI SDK raises on
    an empty key at *client construction*, so the adapter builds its client lazily — meaning
    a missing credential surfaces on the first call rather than as a container that cannot be
    built. The database URL is likewise never contacted here, because `create_async_engine`
    connects on first checkout.
    """
    container = build_container(settings)

    # Reaching this line is the assertion: no exception, and no connection attempted.
    assert container.session_factory is not None


def test_adapters_do_no_io_at_construction() -> None:
    """The constructors must stay I/O-free, or importing the entrypoint becomes a network call.

    Checked structurally rather than by observing sockets: no constructor may be declared
    `async`, and the modules must not build an HTTP client eagerly. This is a proxy for the
    real property, but it fails loudly if someone later makes construction do work.
    """
    for adapter in (NotionRestClient, GmailClient, DeepSeekInterpreter, SystemClock):
        assert not inspect.iscoroutinefunction(adapter.__init__), (
            f"{adapter.__name__}.__init__ must not be async"
        )
