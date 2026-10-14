"""Export routes (design §11.1): queue an export, poll, download.

POST returns 202 after the queued row is committed; the actual build runs
as a background task against one atomic snapshot with a fixed
``result_revision``.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from bio_pdf.api.dependencies import (
    ServiceDep,
    get_service_factory,
    open_background_service,
)
from bio_pdf.application.export_service import FORMATS, ExportService
from bio_pdf.errors import WorkflowError
from fastapi.responses import FileResponse

router = APIRouter(tags=["exports"])


class ExportCreateRequest(BaseModel):
    format: Literal["json", "csv", "fasta"]
    accepted_only: bool = False


def _export_response(row) -> dict:
    return {
        "api_version": "v1",
        "export_id": row["export_id"],
        "job_id": row["job_id"],
        "format": row["format"],
        "status": row["status"],
        "result_revision": row["result_revision"],
        "record_count": row["record_count"],
        "error": row["error"],
    }


def _make_background_runner(factory, export_id: str, job_id: str, fmt: str, accepted_only: bool):
    """Build a closure that runs with its OWN connection (plan T03)."""

    def _run() -> None:
        service = open_background_service(factory)
        try:
            export_svc = ExportService(service.repository, service.paths.artifact_root)
            service.repository.update_export(export_id, status="running")
            rel, count = export_svc.build(job_id, fmt, accepted_only=accepted_only)
            service.repository.update_export(
                export_id, status="completed", artifact_rel=rel, record_count=count
            )
        except WorkflowError as exc:
            service.repository.update_export(export_id, status="failed", error=exc.message)
        except Exception as exc:  # noqa: BLE001 - export failure is recorded, not raised
            service.repository.update_export(export_id, status="failed", error=str(exc))
        finally:
            service.close()

    return _run


@router.post("/jobs/{job_id}/exports", status_code=202)
def create_export(
    job_id: str,
    request: ExportCreateRequest,
    background: BackgroundTasks,
    service: ServiceDep,
    factory=Depends(get_service_factory),
) -> dict:
    if service.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})

    export_id = f"exp-{uuid.uuid4().hex[:12]}"
    # Current revision is captured at queue time; the snapshot read happens
    # atomically when the build runs.
    job = service.get_job(job_id)
    service.repository.create_export(
        export_id=export_id,
        job_id=job_id,
        format=request.format,
        result_revision=job.result_revision,
        accepted_only=request.accepted_only,
    )
    background.add_task(
        _make_background_runner(factory, export_id, job_id, request.format, request.accepted_only)
    )
    return {
        "api_version": "v1",
        "export_id": export_id,
        "job_id": job_id,
        "status": "queued",
        "format": request.format,
    }


@router.get("/exports/{export_id}")
def get_export(export_id: str, service: ServiceDep) -> dict:
    row = service.repository.get_export(export_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "export not found"})
    return _export_response(row)


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, service: ServiceDep) -> FileResponse:
    row = service.repository.get_export(export_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "export not found"})
    if row["status"] != "completed" or not row["artifact_rel"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXPORT_NOT_READY",
                "message": f"export status is {row['status']}",
                "details": {"error": row["error"] or ""},
            },
        )
    path = service.paths.artifact_root / row["artifact_rel"]
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "ARTIFACT_MISSING", "message": "export file missing"})
    media = {
        ".json": "application/json",
        ".csv": "text/csv",
        ".fasta": "text/plain",
    }.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


__all__ = ["router", "FORMATS"]
