"""FastAPI dependencies: container, DB session, API-key and device-key auth."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_db(container: Container = Depends(get_container)) -> Iterator[Session]:
    with container.session_factory() as s:
        yield s


def _matches(provided: str, *expected: str | None) -> bool:
    return any(e and secrets.compare_digest(provided.encode(), e.encode()) for e in expected)


def require_api_key(request: Request, container: Container = Depends(get_container)) -> None:
    key = container.settings.api_key
    if key and not _matches(request.headers.get("X-API-Key", ""), key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")


def require_device_key(request: Request, container: Container = Depends(get_container)) -> None:
    provided = request.headers.get("X-Device-Key") or request.headers.get("X-API-Key") or ""
    if not _matches(provided, container.settings.device_key, container.settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-Device-Key header")
