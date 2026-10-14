"""FastAPI dependency wiring (plan T03: per-request SQLite connections).

The app stores a service *factory*; every request (and every background
task) creates its own :class:`JobService` with its own connection and
closes it when done. Sharing one connection across requests/threads let
concurrent transactions join each other — ``check_same_thread=False`` does
not isolate BEGIN/COMMIT.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request

from bio_pdf.api.settings import ApiSettings
from bio_pdf.application.job_service import JobService
from bio_pdf.errors import WorkflowError


@lru_cache(maxsize=1)
def _get_settings() -> ApiSettings:
    return ApiSettings.from_env()


def get_settings(request: Request) -> ApiSettings:
    """Settings the app was created with (``create_app(settings)``).

    Falls back to environment settings only when no app settings exist
    (bare ``get_settings()`` calls outside an app). Resolving straight
    from the environment here previously made every request ignore the
    injected settings — tests and ``create_app`` callers got ``data/demo``
    while the app's own service factory used the real data dir.
    """

    settings: ApiSettings | None = getattr(request.app.state, "settings", None)
    if settings is not None:
        return settings
    return _get_settings()


def get_service_factory(request: Request) -> Callable[[], JobService]:
    factory: Callable[[], JobService] | None = getattr(
        request.app.state, "service_factory", None
    )
    if factory is None:
        raise HTTPException(status_code=503, detail="application service not ready")
    return factory


def get_job_service(
    factory: Annotated[Callable[[], JobService], Depends(get_service_factory)],
) -> Iterator[JobService]:
    """One JobService (one SQLite connection) per request."""

    service = factory()
    try:
        yield service
    finally:
        service.close()


def open_background_service(factory: Callable[[], JobService]) -> JobService:
    """Open a dedicated service for a background task (own connection)."""

    return factory()


ServiceDep = Annotated[JobService, Depends(get_job_service)]
SettingsDep = Annotated[ApiSettings, Depends(get_settings)]


def workflow_error_to_http(exc: WorkflowError, status_code: int = 400) -> HTTPException:
    """Map a domain error to an HTTPException carrying the stable code."""

    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


__all__ = [
    "get_settings",
    "get_service_factory",
    "get_job_service",
    "open_background_service",
    "ServiceDep",
    "SettingsDep",
    "workflow_error_to_http",
]
