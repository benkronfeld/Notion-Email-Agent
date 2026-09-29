# syntax=docker/dockerfile:1
#
# The app image (spec §2.3.3, deployed to Railway as one service + one Postgres).
#
# No secret is ever baked into this image: `.env` is not copied and must not be, since
# production configuration arrives as platform environment variables (§2.2, constraint 2).
#
#   docker build -t notion-email-agent .
#   docker run --rm -p 8000:8000 --env-file .env notion-email-agent

FROM python:3.12-slim

# `uv` — the pinned package manager (§2.2). Copied from the official distroless image; the
# tag matches the version the lockfile was generated with, so the lock is reproduced
# exactly. Keep it in step with `uv --version` locally.
COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Dependencies live in a project-local venv; put it on PATH so `uvicorn` resolves.
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    # The package is not installed (pyproject sets `package = false`), so `src/` is the
    # import root: `app.main:app` resolves through it.
    PYTHONPATH=/app/src

WORKDIR /app

# Non-root for everything the app itself does.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

# Dependency layer — only the manifest and the lock are inputs, so editing application code
# reuses this layer instead of reinstalling every dependency.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application code. Alembic's `alembic.ini`/`migrations/` are not copied yet: migrations
# are applied as a separate step, and COPY of a path that does not exist fails the build.
COPY --chown=appuser:appuser src/ ./src/

USER appuser

EXPOSE 8000

# Railway injects $PORT; a plain `docker run` without it falls back to 8000. `exec` keeps
# uvicorn as PID 1 so it receives SIGTERM directly and shuts down gracefully.
#
# `app.asgi:app`, not `app.main:app`: `main` exposes `create_app` and deliberately has no
# module-level app, because the one that reads `.env` must not be a module the test suite
# imports. See `src/app/asgi.py`.
CMD ["sh", "-c", "exec uvicorn app.asgi:app --host 0.0.0.0 --port ${PORT:-8000}"]
