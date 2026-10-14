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
from bio_pdf.storage.result_reader import ResultReader
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
    data = ResultReader(service.paths.artifact_root).read_json(job_id, "validation")
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
        ResultReader(service.paths.artifact_root).read_json(job_id, "normalized")
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
    """Return one evidence span (page, bbox, raw text) from the extract index.

    The response carries ``baseline_id`` (the published baseline the span
    was read from) so the frontend can bind navigation to a version and
    detect staleness (plan B01). ``precisions`` splits what is known: text
    offset vs geometry — the UI must not claim character-precise highlight
    from a block bbox.
    """

    job = _job_or_404(service, job_id)
    data = ResultReader(service.paths.artifact_root).read_json(job_id, "extract")
    baseline_id = _baseline_id(service, job_id)
    for span in data.get("evidence_spans", []):
        if span.get("evidence_id") == evidence_id:
            return {
                "api_version": "v1",
                "job_id": job_id,
                "baseline_id": baseline_id,
                "result_revision": job.result_revision,
                "text_precision": span.get("text_precision")
                or _legacy_text_precision(span),
                "geometry_precision": span.get("geometry_precision")
                or _legacy_geometry_precision(span),
                **span,
            }
    raise HTTPException(
        status_code=404,
        detail={"code": "EVIDENCE_MISSING", "message": f"evidence {evidence_id} not found"},
    )


def _baseline_id(service, job_id: str) -> str | None:
    from bio_pdf.storage.result_reader import ResultReader

    manifest = ResultReader(service.paths.artifact_root).read_published_manifest(job_id)
    if manifest is None:
        return None
    return manifest.get("published_at") or None


def _legacy_text_precision(span: dict) -> str:
    """Derive text precision for pre-B02 spans (offset present or not)."""

    if span.get("char_start") is not None or span.get("char_end") is not None:
        return "offset"
    return "block" if span.get("raw_text") else "none"


def _legacy_geometry_precision(span: dict) -> str:
    """Derive geometry precision from what actually exists.

    A block-level bbox is NOT a character box — the previous
    ``location_precision="character"`` claim with a paragraph-sized bbox
    was the review's "假字符精度" finding.
    """

    has_bbox = span.get("bbox") is not None
    fragments = span.get("fragments") or []
    cell_anchored = bool(
        fragments and any(f.get("cell_id") for f in fragments)
    )
    if cell_anchored:
        return "cell"
    if has_bbox:
        return "block"
    if span.get("page_number") is not None:
        return "page"
    if any(f.get("page_number") for f in fragments):
        return "page"
    return "none"


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
