"""FastAPI application factory.

The API serves ``/api/v1/*`` and, when a built frontend is configured
(``BIO_PDF_WEB_DIST``), the static SPA with an HTML5 fallback. The fallback
only matches non-API page paths — an unknown ``/api`` route stays a JSON
404, never the SPA shell.
"""

from __future__ import annotations

import mimetypes
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bio_pdf.api.contracts import API_VERSION, CapabilitiesResponse, EngineCapability
from bio_pdf.api.routes import comparisons, documents, exports, jobs, queue, results, reviews
from bio_pdf.api.settings import ApiSettings
from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.errors import WorkflowError

# Windows registry mimetypes often serve .mjs as octet-stream, which makes
# the browser reject PDF.js's module worker. Register it explicitly.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: ApiSettings = app.state.settings
    # T03: the app keeps a *factory*, not a shared connection. Each request
    # (and each background task) opens its own JobService/SQLite connection.
    def service_factory() -> JobService:
        return JobService(JobPaths.under(settings.data_dir))

    app.state.service_factory = service_factory

    # E2/S05 + F04/S03: comparison experiment recovery. A process that
    # died mid-run leaves sides 'running' with leases that expire; the
    # sweeper (boot pass + periodic loop) claims exactly the queued sides
    # and the lease-expired running ones, so a fast restart whose old
    # leases are still valid recovers as soon as they lapse — no retry
    # click. 'failed' is left for the explicit retry action, and a live
    # execution's heartbeat keeps its lease fresh so it is never stolen.
    def _comparison_recover_once() -> None:
        try:
            from bio_pdf.application.comparison_service import ComparisonService

            svc = service_factory()
            try:
                ComparisonService.from_service(svc).recover_orphans()
            finally:
                svc.close()
        except Exception:  # noqa: BLE001 — recovery is best-effort
            pass

    stop_sweep = threading.Event()

    def _comparison_sweeper() -> None:
        _comparison_recover_once()
        interval = max(1.0, float(settings.comparison_sweep_seconds))
        while not stop_sweep.wait(interval):
            _comparison_recover_once()

    threading.Thread(target=_comparison_sweeper, daemon=True, name="cmp-sweeper").start()
    try:
        yield
    finally:
        stop_sweep.set()


def _capabilities(settings: ApiSettings) -> CapabilitiesResponse:
    from bio_pdf.parsers.docling_adapter import docling_is_available
    from bio_pdf.parsers.ocr import ocr_availability
    from bio_pdf.validation.acceptance import POLICY_VERSION

    engines = [
        EngineCapability(
            name="native",
            available=True,
            supports_tables=False,
            supports_ocr=False,
            supports_page_range=True,
            sends_bytes_offsite=False,
        ),
        EngineCapability(
            name="docling_sdk",
            available=docling_is_available(),
            supports_tables=True,
            supports_ocr=True,
            supports_page_range=True,
            sends_bytes_offsite=False,
            ocr_available=any(ocr_availability().values()),
            ocr_engines=ocr_availability(),
        ),
        EngineCapability(
            name="mineru",
            available=False,  # comparison experiments only; requires token
            supports_tables=True,
            supports_ocr=True,
            supports_page_range=False,
            sends_bytes_offsite=True,
        ),
    ]
    return CapabilitiesResponse(
        engines=engines,
        schema_version="v1",
        rule_version=POLICY_VERSION,
        max_upload_bytes=settings.max_upload_bytes,
        samples_available=settings.samples_dir.is_dir(),
    )


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_env()

    app = FastAPI(
        title="Bio PDF Workbench API",
        version=API_VERSION,
        lifespan=_lifespan,
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Idempotency-Key", "Range", "If-None-Match"],
        )

    api_prefix = "/api/v1"

    @app.get(f"{api_prefix}/capabilities", response_model=CapabilitiesResponse)
    def get_capabilities() -> CapabilitiesResponse:
        return _capabilities(settings)

    app.include_router(documents.router, prefix=api_prefix)
    app.include_router(jobs.router, prefix=api_prefix)
    app.include_router(queue.router, prefix=api_prefix)
    app.include_router(results.router, prefix=api_prefix)
    app.include_router(reviews.router, prefix=api_prefix)
    app.include_router(exports.router, prefix=api_prefix)
    app.include_router(comparisons.router, prefix=api_prefix)

    @app.exception_handler(WorkflowError)
    async def _workflow_error_handler(_: Request, exc: WorkflowError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "api_version": API_VERSION,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": uuid.uuid4().hex[:12],
                },
            },
        )

    # The starlette base class covers router-level misses (unknown routes)
    # as well as FastAPI's own HTTPException subclass.
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        body = {
            "api_version": API_VERSION,
            "error": {
                "code": str(detail.get("code", "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR")),
                "message": str(detail.get("message", "")),
                "details": detail.get("details", {}),
                "request_id": uuid.uuid4().hex[:12],
            },
        }
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    # ---- static SPA (only when a built frontend exists) -------------------
    if settings.web_dist_dir is not None and settings.web_dist_dir.is_dir():
        assets_dir = settings.web_dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        dist_dir = settings.web_dist_dir

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "NOT_FOUND", "message": "unknown API route"},
                )
            candidate = dist_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist_dir / "index.html")

    return app


__all__ = ["create_app", "ApiSettings", "JobService"]
