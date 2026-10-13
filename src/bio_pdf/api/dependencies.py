"""FastAPI dependency wiring.

Every request that needs the application service gets a process-local
:class:`JobService` (one SQLite connection per process — never handed to
another process). The service is created lazily and closed on shutdown.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from bio_pdf.api.settings import ApiSettings
from bio_pdf.application.job_service import JobService
from bio_pdf.errors import WorkflowError


@lru_cache(maxsize=1)
def _get_settings() -> ApiSettings:
    return ApiSettings.from_env()


def get_settings() -> ApiSettings:
    return _get_settings()


def get_job_service(request: Request) -> JobService:
    """Return the process-local application service stored on app.state."""

    service: JobService | None = getattr(request.app.state, "job_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="application service not ready")
    return service


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
    "get_job_service",
    "ServiceDep",
    "SettingsDep",
    "workflow_error_to_http",
]
