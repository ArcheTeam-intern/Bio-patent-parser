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
    """Records with the revision captured in the SAME transaction (B08).

    SnapshotService.read_current reads revision + baseline + heads inside
    one read transaction, so the returned content and the reported
    revision can never drift apart.
    """

    _job_or_404(service, job_id)
    from bio_pdf.application.result_snapshot import SnapshotService

    snapshot = SnapshotService(service.repository, service.paths.artifact_root).read_current(
        job_id, include_record_pages=True
    )
    sequences = snapshot.get("sequences", [])
    measurements = snapshot.get("measurements", [])

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
    visible_ids = {s["sequence_id"] for s in sequences} | {
        m["measurement_id"] for m in measurements
    }
    return RecordsResponse(
        job_id=job_id,
        revision=int(snapshot.get("result_revision", 0)),
        sequences=sequences,
        measurements=measurements,
        record_pages={
            record_id: pages for record_id, pages in snapshot.get("record_pages", {}).items()
            if record_id in visible_ids
        },
        table_pages=_table_pages(service, job_id),
    )


def _table_pages(service, job_id: str) -> dict[int, int]:
    """Tables per page from the normalized artifact (tables tab overview).

    Missing or unreadable normalized output degrades to an empty map —
    the overview is a convenience, never a hard dependency.
    """

    from bio_pdf.errors import WorkflowError

    try:
        data = ResultReader(service.paths.artifact_root).read_json(job_id, "normalized")
    except WorkflowError:
        return {}
    out: dict[int, int] = {}
    for page in data.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        page_no = page.get("page_number")
        count = len(page.get("tables") or [])
        if count and isinstance(page_no, int) and not isinstance(page_no, bool):
            out[page_no] = count
    return out


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


@router.get("/pages/{page_number}/figure.png")
def page_figure(
    service: ServiceDep,
    job_id: str,
    page_number: int,
    bbox: str,
    dpi: int = 200,
    pad: float = 6.0,
) -> Response:
    """Render one region of the job's OWN source page as a standalone PNG.

    This is a derived image (a crop of the PDF the client can already see),
    not a parser claim: it exists so colour-encoded result figures —
    susceptibility heat matrices where the background colour is part of the
    message — can be exported and reviewed as one picture. Coordinates are
    the normalized top-left points already served by /pages; a small
    padding includes the table's own borders/labels.
    """

    job = _job_or_404(service, job_id)
    try:
        values = [float(part) for part in bbox.split(",")]
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "BAD_BBOX", "message": "bbox must be x0,y0,x1,y1 in points"},
        ) from exc
    if len(values) != 4:
        raise HTTPException(
            status_code=422,
            detail={"code": "BAD_BBOX", "message": "bbox must have 4 numbers"},
        )
    bx0, by0, bx1, by1 = values
    x0, x1 = sorted((bx0, bx1))
    y0, y1 = sorted((by0, by1))
    if x1 - x0 <= 1 or y1 - y0 <= 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "BAD_BBOX", "message": "degenerate region"},
        )
    import os

    from bio_pdf.pdf.render import render_evidence_crop

    row = service.repository.connection.execute(
        "SELECT source_path FROM documents WHERE document_id = ?",
        (job.document_id,),
    ).fetchone()
    if row is None or not os.path.isfile(row["source_path"]):
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_MISSING", "message": "job source file not available"},
        )
    x0 = max(0.0, x0 - pad)
    y0 = max(0.0, y0 - pad)
    x1, y1 = x1 + pad, y1 + pad
    import tempfile

    with tempfile.TemporaryDirectory(prefix="figure-") as tmp:
        out = Path(tmp) / "figure.png"
        render_evidence_crop(
            Path(row["source_path"]),
            page_number=page_number,
            bbox=(x0, y0, x1, y1),
            dpi=max(72, min(dpi, 400)),
            output_path=out,
            document_id=job.document_id,
        )
        data = out.read_bytes()
    filename = f"{job_id}-p{page_number}-figure.png"
    return Response(
        content=data,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
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


@router.get("/issues")
def list_issues(service: ServiceDep, job_id: str, state: str | None = None) -> dict:
    """Structured parse issues (plan B06): scope/reason/sources/state."""

    _job_or_404(service, job_id)
    rows = service.repository.list_issues(job_id, state=state)
    issues = [
        {
            "issue_id": row["issue_id"],
            "job_id": row["job_id"],
            "scope": row["scope"],
            "reason_code": row["reason_code"],
            "source_ids": json.loads(row["source_json"] or "[]"),
            "detail": row["detail"],
            "state": row["state"],
            "baseline_id": row["baseline_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return {"api_version": "v1", "job_id": job_id, "issues": issues, "total": len(issues)}


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
