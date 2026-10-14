"""Export routes (plan §11.1, fixed-snapshot in B08).

Order of operations:

1. POST captures the snapshot SYNCHRONOUSLY (one read transaction) and
   writes it under ``jobs/<job>/exports/<export_id>/snapshot.json``;
2. the export row records the snapshot ref + result_revision;
3. the background task builds ONLY from that snapshot file — a review
   landing after (1) can never mix revisions;
4. artifacts live under the export's own directory (format/filter cannot
   collide on one revision).
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
from bio_pdf.application.result_snapshot import SnapshotService
from bio_pdf.errors import WorkflowError
from fastapi.responses import FileResponse

router = APIRouter(tags=["exports"])


class ExportCreateRequest(BaseModel):
    format: Literal["json", "csv", "fasta"]
    accepted_only: bool = True  # B08: default is the reviewed set
    include_candidates: bool = False  # explicit diagnostic marker


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
    """Build from the FIXED snapshot with its own connection (plan B03/B08)."""

    def _run() -> None:
        service = open_background_service(factory)
        try:
            export_svc = ExportService(service.repository, service.paths.artifact_root)
            snapshot_svc = SnapshotService(service.repository, service.paths.artifact_root)
            row = service.repository.get_export(export_id)
            snapshot_rel = row["snapshot_rel"] if row else None
            if not snapshot_rel:
                service.repository.update_export(
                    export_id, status="failed", error="snapshot missing for export"
                )
                return
            snapshot = snapshot_svc.read_snapshot(snapshot_rel)
            service.repository.update_export(export_id, status="running")
            rel, count = export_svc.build_from_snapshot(
                job_id=job_id,
                export_id=export_id,
                snapshot=snapshot,
                fmt=fmt,
                accepted_only=accepted_only,
            )
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
    accepted_only = request.accepted_only and not request.include_candidates

    # B08: the snapshot is captured NOW, in this request — the background
    # build reads only this file.
    snapshot_svc = SnapshotService(service.repository, service.paths.artifact_root)
    try:
        snapshot = snapshot_svc.capture(job_id, export_id=export_id)
    except WorkflowError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc

    service.repository.create_export(
        export_id=export_id,
        job_id=job_id,
        format=request.format,
        result_revision=int(snapshot.payload.get("result_revision", 0)),
        accepted_only=accepted_only,
        snapshot_rel=snapshot.rel_path,
        snapshot_sha=snapshot.sha256,
    )
    background.add_task(
        _make_background_runner(factory, export_id, job_id, request.format, accepted_only)
    )
    return {
        "api_version": "v1",
        "export_id": export_id,
        "job_id": job_id,
        "status": "queued",
        "format": request.format,
        "result_revision": snapshot.payload.get("result_revision", 0),
        "accepted_only": accepted_only,
        "include_candidates": request.include_candidates,
    }


@router.get("/exports/{export_id}")
def get_export(export_id: str, service: ServiceDep) -> dict:
    row = service.repository.get_export(export_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "export not found"})
    payload = _export_response(row)
    # Download metadata makes the baseline/view explicit (plan B08).
    payload["accepted_only"] = bool(row["accepted_only"])
    payload["snapshot_rel"] = row["snapshot_rel"] if "snapshot_rel" in row.keys() else None
    return payload


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
    # Filename carries baseline revision + filter so files on disk are not
    # mistaken for each other.
    filter_tag = "accepted" if row["accepted_only"] else "candidates"
    filename = f"{export_id}-r{row['result_revision']}-{filter_tag}{path.suffix}"
    return FileResponse(path, media_type=media, filename=filename)


__all__ = ["router", "FORMATS"]
