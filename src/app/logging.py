"""Structured logging — JSON to stdout (spec §2.2), with correlation IDs per job run."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit one JSON object per line on stdout."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


@contextmanager
def job_context(job_name: str, correlation_id: str) -> Iterator[None]:
    """Bind a correlation ID for the duration of one job run, then clear it."""
    structlog.contextvars.bind_contextvars(job=job_name, correlation_id=correlation_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("job", "correlation_id")
