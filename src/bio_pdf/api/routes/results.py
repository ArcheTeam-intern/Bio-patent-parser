"""Result routes: records, page projection, evidence and artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from bio_pdf.api.contracts import (
    ArtifactInfo,
    ArtifactsResponse,
    PageResponse,
    RecordsResponse,
)
from bio_pdf.api.dependencies import ServiceDep, SettingsDep
from bio_pdf.contracts import NormalizedDocument

router = APIRouter(prefix="/jobs/{job_id}", tags=["results"])


def _job_or_404(service, job_id: str):
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    return job


def _result_base(service, settings, job_id: str) -> Path:
    return service.paths.artifact_root / "jobs" / job_id


def _load_json(path: Path) -> dict:
    if not path.is_file():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RESULT_NOT_READY",
                "message": "job has no result artifacts yet",
                "details": {"path": str(path.name)},
            },
        )
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/records", response_model=RecordsResponse)
def get_records(
    service: ServiceDep,
    settings: SettingsDep,
    job_id: str,
    kind: str | None = None,
    q: str | None = None,
) -> RecordsResponse:
    _job_or_404(service, job_id)
    data = _load_json(_result_base(service, settings, job_id) / "validate" / "validation.json")
    sequences = data.get("sequences", [])
    measurements = data.get("measurements", [])

    # Overlay human-reviewed record heads so the workbench shows the current
    # version, not a stale baseline.
    heads = {row["record_id"]: row for row in service.repository.list_record_heads(job_id)}
    overlaid_seqs = []
    for seq in sequences:
        head = heads.get(seq["sequence_id"])
        if head is not None:
            version = service.repository.get_record_version(
                job_id, seq["sequence_id"], int(head["revision"])
            )
            if version is not None:
                seq = json.loads(version["record_json"])
        overlaid_seqs.append(seq)
    overlaid_meas = []
    for meas in measurements:
        head = heads.get(meas["measurement_id"])
        if head is not None:
            version = service.repository.get_record_version(
                job_id, meas["measurement_id"], int(head["revision"])
            )
            if version is not None:
                meas = json.loads(version["record_json"])
        overlaid_meas.append(meas)
    sequences, measurements = overlaid_seqs, overlaid_meas

    if kind == "sequence":
        measurements = []
    elif kind == "measurement":
        sequences = []
    if q:
        needle = q.lower()
        sequences = [
            s for s in sequences
            if needle in str(s.get("sequence_id", "")).lower()
            or needle in str(s.get("normalized_sequence", "")).lower()
            or needle in str(s.get("seq_id_no", "")).lower()
        ]
        measurements = [
            m for m in measurements
            if needle in str(m.get("measurement_id", "")).lower()
            or needle in str(m.get("metric", "")).lower()
            or needle in str(m.get("target_raw", "") or "").lower()
        ]
    job = service.get_job(job_id)
    return RecordsResponse(
        job_id=job_id,
        revision=job.result_revision if job else 0,
        sequences=sequences,
        measurements=measurements,
    )


@router.get("/pages/{page_number}", response_model=PageResponse)
def get_page(
    service: ServiceDep,
    settings: SettingsDep,
    job_id: str,
    page_number: int,
) -> PageResponse:
    _job_or_404(service, job_id)
    document = NormalizedDocument.model_validate(
        _load_json(_result_base(service, settings, job_id) / "normalized.json")
    )
    page = next((p for p in document.pages if p.page_number == page_number), None)
    if page is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": f"page {page_number} not in result"},
        )
    return PageResponse(
        job_id=job_id,
        page_number=page.page_number,
        width_pt=page.width_pt,
        height_pt=page.height_pt,
        route=page.route,
        blocks=[b.model_dump(mode="json") for b in page.blocks],
        tables=[t.model_dump(mode="json") for t in page.tables],
    )


@router.get("/evidence/{evidence_id}")
def get_evidence(
    service: ServiceDep,
    settings: SettingsDep,
    job_id: str,
    evidence_id: str,
) -> dict:
    """Return one evidence span (page, bbox, raw text) from the extract index."""

    _job_or_404(service, job_id)
    data = _load_json(_result_base(service, settings, job_id) / "extract" / "extract.json")
    for span in data.get("evidence_spans", []):
        if span.get("evidence_id") == evidence_id:
            return {"api_version": "v1", **span}
    raise HTTPException(
        status_code=404,
        detail={"code": "EVIDENCE_MISSING", "message": f"evidence {evidence_id} not found"},
    )


@router.get("/artifacts", response_model=ArtifactsResponse)
def list_artifacts(service: ServiceDep, settings: SettingsDep, job_id: str) -> ArtifactsResponse:
    _job_or_404(service, job_id)
    rows = service.repository.connection.execute(
        "SELECT artifact_id, relative_path, media_type FROM artifacts WHERE relative_path LIKE ? ORDER BY created_at",
        (f"jobs/{job_id}/%",),
    ).fetchall()
    root = service.paths.artifact_root
    infos = []
    for row in rows:
        path = root / row["relative_path"]
        infos.append(
            ArtifactInfo(
                artifact_id=row["artifact_id"],
                relative_path=row["relative_path"],
                media_type=row["media_type"],
                bytes=path.stat().st_size if path.is_file() else None,
            )
        )
    return ArtifactsResponse(job_id=job_id, artifacts=infos)


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(
    service: ServiceDep,
    settings: SettingsDep,
    job_id: str,
    artifact_id: str,
) -> Response:
    """Serve an artifact by ID — the path always comes from the database."""

    _job_or_404(service, job_id)
    ref = service.repository.get_artifact(artifact_id)
    if ref is None or not ref.relative_path.startswith(f"jobs/{job_id}/"):
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "artifact not found for this job"},
        )
    path = service.paths.artifact_root / ref.relative_path
    if not path.is_file():
        raise HTTPException(
            status_code=410,
            detail={"code": "ARTIFACT_MISSING", "message": "artifact file missing"},
        )
    media = "text/csv" if path.suffix == ".csv" else "application/json"
    return FileResponse(path, media_type=media, headers={"ETag": f'"{ref.sha256}"'})


__all__ = ["router"]
