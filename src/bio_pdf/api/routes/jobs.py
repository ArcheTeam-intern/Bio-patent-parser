"""Job routes: create (202), list, detail, stages and resume."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from bio_pdf.api.contracts import (
    JobCreateRequest,
    JobResponse,
    StageRunView,
)
from bio_pdf.api.dependencies import ServiceDep, workflow_error_to_http
from bio_pdf.errors import JobRunningError, WorkflowError

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: Stable API parser names → engine persisted in the job config (B09 fix:
#: "docling_sdk" must pass through — the old mapping to the legacy name
#: "docling" made every UI "结构增强" job fail config validation).
_PARSERS = {
    "native": "native",
    "docling_sdk": "docling_sdk",
    "docling": "docling_sdk",  # legacy alias
    "docling_serve": "docling_serve",
    "mineru_http": "mineru_http",
}


def _job_response(service, job) -> JobResponse:
    stages = [
        StageRunView(
            stage_name=s.stage_name,
            attempt=s.attempt,
            status=s.status,
            started_at=s.started_at.isoformat() if s.started_at else None,
            ended_at=s.ended_at.isoformat() if s.ended_at else None,
            error={str(k): str(v) for k, v in s.error_json.items()},
        )
        for s in service.repository.list_stages(job.job_id)
    ]
    export_id = _latest_export_artifact(service, job.job_id)
    config = service.repository.get_job_config(job.job_id)
    engine = str((config.get("effective_config") or {}).get("parser") or "")
    return JobResponse(
        job_id=job.job_id,
        document_id=job.document_id,
        status=job.status,
        current_stage=job.current_stage,
        result_revision=job.result_revision,
        open_reviews=service.open_review_count(job.job_id),
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        stages=stages,
        export_artifact_id=export_id,
        config_hash=job.config_hash,
        engine=engine,
    )


def _latest_export_artifact(service, job_id: str) -> str | None:
    row = service.repository.connection.execute(
        """
        SELECT artifact_id FROM artifacts
        WHERE relative_path = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (f"jobs/{job_id}/export/result.json",),
    ).fetchone()
    return row["artifact_id"] if row else None


@router.post("", response_model=JobResponse, status_code=202)
def create_job(service: ServiceDep, request: JobCreateRequest) -> JobResponse:
    engine = _PARSERS.get(request.parser)
    if engine is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ENGINE_UNAVAILABLE",
                "message": f"unknown parser {request.parser!r}",
                "details": {"allowed": sorted(_PARSERS)},
            },
        )

    row = service.repository.connection.execute(
        "SELECT source_path FROM documents WHERE document_id = ?",
        (request.document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "document not found; upload it first"},
        )

    hint = None if request.document_type_hint == "auto" else request.document_type_hint
    try:
        job, _page_count = service.create_job(
            _resolved_source_path(row["source_path"]),
            engine=engine,
            page_numbers=tuple(request.page_numbers or ()) or (),
            targets=tuple(request.targets),
            document_type_hint=hint,
        )
    except WorkflowError as exc:
        raise workflow_error_to_http(exc, status_code=422) from exc

    return _job_response(service, service.get_job(job.job_id))


@router.get("", response_model=list[JobResponse])
def list_jobs(
    service: ServiceDep,
    document_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[JobResponse]:
    limit = max(1, min(limit, 200))
    jobs = service.list_jobs()
    out = []
    for job in jobs:
        if document_id and job.document_id != document_id:
            continue
        if status and job.status != status:
            continue
        out.append(_job_response(service, job))
        if len(out) >= limit:
            break
    return out


@router.get("/{job_id}", response_model=JobResponse)
def get_job(service: ServiceDep, job_id: str) -> JobResponse:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    return _job_response(service, job)


@router.get("/{job_id}/stages", response_model=list[StageRunView])
def get_stages(service: ServiceDep, job_id: str) -> list[StageRunView]:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    return [
        StageRunView(
            stage_name=s.stage_name,
            attempt=s.attempt,
            status=s.status,
            started_at=s.started_at.isoformat() if s.started_at else None,
            ended_at=s.ended_at.isoformat() if s.ended_at else None,
            error={str(k): str(v) for k, v in s.error_json.items()},
        )
        for s in service.repository.list_stages(job_id)
    ]


@router.post("/{job_id}/resume", status_code=202)
def resume_job(service: ServiceDep, job_id: str) -> dict:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    if job.status not in ("failed", "running", "queued"):
        # A fully succeeded job simply returns its current state.
        return _job_response(service, job)
    try:
        # T03: a running job with a valid lease refuses (409 JOB_RUNNING);
        # otherwise the resume intent is persisted and the job requeued.
        service.requeue_resume(job_id)
    except JobRunningError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    except WorkflowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    response = _job_response(service, service.get_job(job_id))
    return {**response.model_dump(mode="json"), "execution_mode": "resume"}


def _resolved_source_path(raw: str) -> object:
    from pathlib import Path

    return Path(raw)


__all__ = ["router"]
