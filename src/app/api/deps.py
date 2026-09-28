"""Admin API dependencies: bearer-token auth and access to the container's session.

The API is ops-only (spec §2.3.6). Everything except ``/healthz`` requires
``Authorization: Bearer $ADMIN_API_TOKEN``. The token is compared with
``secrets.compare_digest`` so a wrong token cannot be recovered by timing, and it is
never logged or echoed (CLAUDE.md constraint 2).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import AppContainer

_BEARER = "bearer"


def get_container(request: Request) -> AppContainer:
    """The process-wide `AppContainer` riding on `app.state`."""
    container: AppContainer = request.app.state.container
    return container


def _unauthorized() -> HTTPException:
    """A 401 that does not reveal whether the token was missing, malformed, or wrong."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing admin token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin_token(request: Request) -> None:
    """Enforce the bearer token. Raised as 401 on anything but an exact match.

    Bytes comparison rather than str: `secrets.compare_digest` rejects non-ASCII str
    arguments with a TypeError, and a hand-crafted header could contain any Unicode.
    """
    expected = get_container(request).settings.admin_api_token
    header = request.headers.get("Authorization", "")
    scheme, _, provided = header.partition(" ")
    if scheme.lower() != _BEARER or not provided:
        raise _unauthorized()
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise _unauthorized()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A session from the container's factory, closed at the end of the request."""
    container = get_container(request)
    async with container.session_factory() as session:
        yield session


ContainerDep = Annotated[AppContainer, Depends(get_container)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
