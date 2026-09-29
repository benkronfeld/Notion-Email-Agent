"""The ASGI entrypoint uvicorn serves: `uvicorn app.asgi:app`.

This is the **only** module that reads the process environment at import time, and it is
deliberately kept out of `main.py`. The test harness imports `create_app` from `app.main`
in a fixture; if the module-level `app` lived there, every integration test would call
`get_settings()`, load the real `.env`, and pull the production `DATABASE_URL` and
`REMINDER_RECIPIENT` into a test process. `CLAUDE.md` constraints 1 and 2 exist to make that
impossible, so the environment read is isolated here and no test imports this module.

The container is built once, at import, because that is what the ASGI server expects.
Nothing in it connects: engines connect on first checkout and each adapter builds its
client lazily, so importing this module is safe on a machine with no database and no valid
credentials.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI

from app.bootstrap import build_container
from app.config import get_settings
from app.main import create_app

_settings: Final = get_settings()

# `get_settings` is `lru_cache`d, so the settings object the app runs with is the same one
# every importer sees — there is one configuration per process, not one per call site.
app: Final[FastAPI] = create_app(_settings, build_container(_settings))
