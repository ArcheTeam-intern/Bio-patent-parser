"""Result routes: records, page projection, evidence and artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, PositiveInt, field_validator
from fastapi.responses import FileResponse

from bio_pdf.api.contracts import (
    ArtifactInfo,
    ArtifactsResponse,
    FiguresResponse,
    PageResponse,
    RecordsResponse,
)
from bio_pdf.api.dependencies import ServiceDep, SettingsDep
from bio_pdf.errors import WorkflowError
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
    entities, relations, relation_summary = _entities_relations(service, job_id)
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
        sequence_inventory=_sequence_inventory(service, job_id),
        sequence_links=_sequence_links(service, job_id),
        entities=entities,
        relations=relations,
        relation_summary=relation_summary,
    )


def _entities_relations(
    service, job_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """C04 entities/relations from the extract artifact (real evidence ids).

    Missing/unreadable output degrades to empty lists + a summary carrying
    ``extracted: false`` — never fabricated links.
    """

    data = _read_result_or_409(service, job_id, "extract")
    if data is None:
        return [], [], {"extracted": False}
    entities = data.get("entities") or []
    relations = data.get("relations") or []
    by_status: dict[str, int] = {}
    by_predicate: dict[str, int] = {}
    for relation in relations:
        status = str(relation.get("link_status") or "linked")
        predicate = str(relation.get("predicate") or "?")
        by_status[status] = by_status.get(status, 0) + 1
        by_predicate[predicate] = by_predicate.get(predicate, 0) + 1
    return (
        entities,
        relations,
        {
            "extracted": True,
            "entities": len(entities),
            "relations": len(relations),
            "by_link_status": by_status,
            "by_predicate": by_predicate,
            "unresolved_references": len(data.get("unresolved_references") or []),
        },
    )


def _sequence_inventory(service, job_id: str) -> dict[str, Any] | None:
    """Declaration-level sequence inventory from the extract artifact (C02).

    Missing/unreadable/older extract output degrades to None — the response
    stays valid and the UI reports "未评估" instead of fabricating coverage.
    """

    data = _read_result_or_409(service, job_id, "extract")
    if data is None:
        return None
    value = data.get("sequence_inventory")
    return value if isinstance(value, dict) else None


def _sequence_links(service, job_id: str) -> dict[str, Any]:
    """Candidate entity → SEQ ID links from the extract artifact.

    Empty for jobs extracted before the links existed, and empty when the
    document never states a relation — the UI must not imply coverage it
    does not have.
    """

    data = _read_result_or_409(service, job_id, "extract")
    if data is None:
        return {}
    value = data.get("sequence_links")
    return value if isinstance(value, dict) else {}



def _read_result_or_409(service, job_id: str, name: str) -> dict[str, Any] | None:
    """Read a result artifact (D09): missing → None (the caller shows the
    honest empty state); CORRUPT → 409 RESULT_CORRUPT.

    The previous broad ``except WorkflowError`` turned a corrupted published
    artifact into "no result yet" with empty content — exactly the silent
    degradation the acceptance flagged.
    """

    from bio_pdf.storage.result_reader import ResultCorruptError

    try:
        return ResultReader(service.paths.artifact_root).read_json(job_id, name)
    except ResultCorruptError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RESULT_CORRUPT",
                "message": str(exc) or "published result is corrupted",
                "details": getattr(exc, "details", {}) or {},
            },
        ) from exc
    except WorkflowError:
        return None

def _table_pages(service, job_id: str) -> dict[int, int]:
    """Tables per page from the normalized artifact (tables tab overview).

    Missing or unreadable normalized output degrades to an empty map —
    the overview is a convenience, never a hard dependency.
    """

    data = _read_result_or_409(service, job_id, "normalized")
    if data is None:
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

    def _channel(block) -> str:
        # D08 来源对照: the block's OWN provenance decides; the document
        # channel is only the fallback.
        for prov in block.provenance:
            if prov.source_type in ("native", "docling", "mineru", "ocr", "tesseract"):
                return prov.source_type
        lowered = document.parser_run_id.lower()
        if "native" in lowered:
            return "native"
        if "docling" in lowered or "mineru" in lowered:
            return "layout"
        return "unknown"

    blocks_out = []
    for block in page.blocks:
        payload = block.model_dump(mode="json")
        payload["source_parser"] = _channel(block)
        blocks_out.append(payload)
    return PageResponse(
        job_id=job_id,
        page_number=page.page_number,
        width_pt=page.width_pt,
        height_pt=page.height_pt,
        route=page.route,
        blocks=blocks_out,
        tables=[t.model_dump(mode="json") for t in page.tables],
        figures=[f.model_dump(mode="json") for f in page.figures],
    )


@router.get("/figures", response_model=FiguresResponse)
def list_figures(
    service: ServiceDep,
    job_id: str,
    page_number: int | None = None,
) -> FiguresResponse:
    """Every detected visual region (chart / curve / diagram) across pages.

    C07: ``page_number`` filters to one page (the 图片 tab follows the
    current page by default); the unfiltered call keeps returning all
    figures for compatibility, and ``total`` always reports the document
    count so the UI can show "当前页 2 / 全文 7".
    """

    _job_or_404(service, job_id)

    data = _read_result_or_409(service, job_id, "normalized")
    if data is None:
        return FiguresResponse(
            job_id=job_id,
            figures=[],
            note="no normalized result yet",
        )
    out: list[dict] = []
    for page in data.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        for fig in page.get("figures", []) or []:
            if isinstance(fig, dict):
                out.append(fig)
    total = len(out)
    if page_number is not None:
        out = [f for f in out if int(f.get("page_number", 0)) == page_number]
    note = None
    if not out:
        if page_number is not None and total > 0:
            note = f"第 {page_number} 页没有图片（全文共 {total} 个，可切换“全文”查看）。"
        else:
            pages = [p for p in data.get("pages", []) or [] if isinstance(p, dict)]
            layout_seen = any(str(p.get("route", "")).startswith("layout_") for p in pages)
            if not layout_seen:
                note = (
                    "此任务未使用结构增强引擎（Docling），没有图片区域检测；"
                    "重新创建任务并选择“结构增强”后可提取统计图/曲线图。"
                )
            else:
                note = "结构引擎在本文件未检测到任何图区（可能是纯文本论文或图片未被识别）。"
    return FiguresResponse(
        job_id=job_id,
        figures=out,
        note=note,
        total=total,
        page_number=page_number,
    )


@router.get("/pages/{page_number}/figure.png")
def page_figure(
    service: ServiceDep,
    job_id: str,
    page_number: int,
    bbox: str | None = None,
    figure: str | None = None,
    dpi: int = 200,
    pad: float = 6.0,
) -> Response:
    """Render one region of the job's OWN source page as a standalone PNG.

    This is a derived image (a crop of the PDF the client can already see),
    not a parser claim: it exists so colour-encoded result figures —
    susceptibility heat matrices and stat charts where the drawing itself
    carries information — can be exported and reviewed as one picture.
    Coordinates come either from an explicit top-left ``bbox`` (the values
    /pages serves) or a detected ``figure`` id (resolved server-side, no
    coordinate round-trip). A small padding includes borders/labels.
    """

    job = _job_or_404(service, job_id)
    from bio_pdf.errors import WorkflowError

    if figure and not bbox:
        try:
            norm = ResultReader(service.paths.artifact_root).read_json(job_id, "normalized")
        except WorkflowError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "RESULT_NOT_READY", "message": str(exc)},
            ) from exc
        found = None
        for page in norm.get("pages", []) or []:
            for fig in (page or {}).get("figures", []) or []:
                if isinstance(fig, dict) and fig.get("figure_id") == figure:
                    found = fig
                    break
            if found:
                break
        if found is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": f"figure {figure!r} not in result"},
            )
        page_number = int(found.get("page_number", page_number))
        bbox = ",".join(str(v) for v in found["bbox"])
    if not bbox:
        raise HTTPException(
            status_code=422,
            detail={"code": "BAD_BBOX", "message": "bbox or figure is required"},
        )
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
    """F09: the ONE outward baseline id.

    The DB publication row is the source of truth (it is what the review
    flow fences against); the published manifest records the same value at
    publish time. ``published_at``/``manifest_sha`` are separate fields and
    are NOT substituted for the baseline except for legacy jobs that never
    recorded one.
    """

    publication = service.repository.get_publication(job_id)
    if publication is not None and "baseline_id" in publication.keys():
        value = publication["baseline_id"]
        if value:
            return str(value)
    from bio_pdf.storage.result_reader import ResultReader

    manifest = ResultReader(service.paths.artifact_root).read_published_manifest(job_id)
    if manifest is None:
        return None
    return manifest.get("baseline_id") or manifest.get("published_at") or None


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


#: C09 severity by reason code — 残基缺失/长度与结构冲突 = warning/error，
#: "评分表""菌株目录"类分类结论 = info（提示，不是错误）。未知代码保守给 warning。
_ISSUE_SEVERITY: dict[str, str] = {
    "SEQ_INVENTORY_CONFLICT": "error",
    "SEQ_LENGTH_CONFLICT": "error",
    "VALIDATION_CONFLICT": "error",
    "SOURCE_CONFLICT": "error",
    "TEXT_CONFLICT": "error",
    "MISSING_TABLE": "error",
    "TABLE_GRID_OVERLAP": "error",
    "ZERO_RESIDUE_LISTING": "warning",
    "SEQ_INVENTORY_MISSING": "warning",
    "TABLE_GEOMETRY_RECONSTRUCTED": "warning",
    "TABLE_STRUCTURE": "warning",
    "RESULT_WITHOUT_METRIC": "warning",
    "REPARSE_EXHAUSTED": "warning",
    "TABLE_SCORING": "info",
    "TABLE_INVENTORY": "info",
    "TABLE_UNCLASSIFIED": "info",
    "TABLE_ASSAY_CONDITIONS": "info",
    "TABLE_SEQUENCE": "info",
}

#: C09 为什么 / 影响什么 / 下一步怎么办 模板（按代码；detail 提供具体对象）。
_ISSUE_GUIDANCE: dict[str, dict[str, str]] = {
    "SEQ_INVENTORY_MISSING": {
        "title": "序列残基未恢复",
        "impact": "完整性未知，暂不能导出完整 FASTA 或建立确认的序列关联",
        "suggestion": "对该区域做区域 OCR，并人工核对长度与字符",
    },
    "SEQ_INVENTORY_CONFLICT": {
        "title": "序列声明冲突",
        "impact": "同一 SEQ ID 的声明与恢复结果互相矛盾，不能据此导出",
        "suggestion": "人工核对原文与恢复长度，确认以哪一侧为准",
    },
    "ZERO_RESIDUE_LISTING": {
        "title": "清单声明无残基",
        "impact": "该声明编号没有可用的残基内容",
        "suggestion": "检查渲染区域是否需要 OCR，或确认该声明在原文中即为空",
    },
    "TABLE_GEOMETRY_RECONSTRUCTED": {
        "title": "表格为几何重建",
        "impact": "结构与轴标签由坐标聚类重建，可能错位",
        "suggestion": "对照原图逐格核对后再接受",
    },
    "RESULT_WITHOUT_METRIC": {
        "title": "结果缺少可解析指标",
        "impact": "该活性描述没有匹配到已注册指标",
        "suggestion": "人工确认指标，或在指标注册表补充该写法",
    },
    "TABLE_SCORING": {
        "title": "评分表（提示）",
        "impact": "结构化保留、不做活性提取——通常是评分标准而非实验结果",
        "suggestion": "如需提取请人工标注目标列",
    },
    "TABLE_INVENTORY": {
        "title": "清单/目录表（提示）",
        "impact": "结构化保留、不做活性提取",
        "suggestion": "无需处理；如含实验结果请人工标注",
    },
}


def _issue_page(issue: dict[str, Any]) -> int | None:
    """Page from REAL fields only — never guessed.

    D09: newly created issues carry ``page_number`` directly. For historical
    rows the fallbacks use fields that MEAN a page: the ``page N`` text in
    the detail, and OUR documented id conventions — ``<page>:t<idx>`` /
    ``<page>:grid`` (normalize_docling and grid_matrix generate exactly
    these shapes, so the prefix is a fact, not an opaque-id guess).
    """

    if issue.get("page_number") is not None:
        try:
            return int(issue["page_number"])
        except (TypeError, ValueError):
            pass

    import re as _re

    for pattern in (r"\bpage\s+(\d+)\b", r"第\s*(\d+)\s*页", r":p(\d+)[:\-]"):
        match = _re.search(pattern, str(issue.get("detail") or ""))
        if match:
            return int(match.group(1))
    for source_id in issue.get("source_ids") or []:
        sid = str(source_id)
        for pattern in (r"^(?:p)?(\d+):t\d+$", r"^(\d+):grid$", r":p(\d+)[:\-]"):
            match = _re.search(pattern, sid)
            if match:
                return int(match.group(1))
    return None


@router.get("/issues")
def list_issues(
    service: ServiceDep,
    job_id: str,
    state: str | None = None,
    severity: str | None = None,
    page_number: int | None = None,
) -> dict:
    """Structured parse issues with C09 detail (severity / page / guidance).

    Filters: ``state`` (open/dismissed/…), ``severity`` (info/warning/error)
    and ``page_number`` (位置来自真实 detail/source 字段，不从 id 猜页号).
    """

    _job_or_404(service, job_id)
    rows = service.repository.list_issues(job_id, state=state)
    # H08/P05: the list uses the SAME view builder as the single-issue
    # endpoint — one serializer, so capabilities can never drift between
    # the list the panel renders and the detail the route enforces.
    issues: list[dict[str, Any]] = [_issue_view(service, row) for row in rows]

    if severity:
        issues = [i for i in issues if i["severity"] == severity]
    if page_number is not None:
        issues = [i for i in issues if i["page_number"] == page_number]
    counts: dict[str, int] = {"info": 0, "warning": 0, "error": 0}
    for i in issues:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    return {
        "api_version": "v1",
        "job_id": job_id,
        "issues": issues,
        "total": len(issues),
        "severity_counts": counts,
    }


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



@router.get("/pages/{page_number}/channels")
def page_channels(service: ServiceDep, job_id: str, page_number: int) -> dict:
    """E9/P05 + F08/P04+P05: the RAW per-engine content of one page.

    The fused normalized document answers "what the pipeline read"; this
    endpoint answers "what each ENGINE produced". F08: the CURRENT published
    manifest is the authority — every channel is read from a manifest ref
    whose sha256 is VERIFIED before parsing (a corrupted sidecar is
    RESULT_CORRUPT, never silently skipped). Channels carry text blocks AND
    table cells (a table-only page still has a channel). Stage attempts that
    are NOT part of the current publication are listed separately under
    ``history`` — never mixed into the current channels. Legacy jobs without
    a published manifest fall back to the working tree, clearly marked.
    """

    from bio_pdf.storage.result_reader import ResultCorruptError

    _job_or_404(service, job_id)
    root = service.paths.artifact_root

    def _verified_json(entry: dict[str, Any], *, label: str) -> dict[str, Any]:
        """Read one manifest ref, hash-verified. Corruption → 409."""

        from bio_pdf.storage.artifacts import compute_sha256_bytes

        rel = str(entry.get("relative_path") or "")
        sha = str(entry.get("sha256") or "")
        path = root / rel
        if not rel or not sha or not path.is_file():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RESULT_CORRUPT",
                    "message": f"published artifact missing for {label}: {rel or '?'}",
                    "details": {"relative_path": rel},
                },
            )
        data = path.read_bytes()
        if compute_sha256_bytes(data) != sha:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RESULT_CORRUPT",
                    "message": f"published artifact hash mismatch for {label}: {rel}",
                    "details": {"relative_path": rel},
                },
            )
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RESULT_CORRUPT",
                    "message": f"published artifact is not valid JSON: {rel}",
                    "details": {"relative_path": rel},
                },
            ) from exc

    def _page_view(page: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        lines: list[dict[str, Any]] = []
        for block in page.get("blocks", []) or []:
            if not isinstance(block, dict):
                continue
            lines.append(
                {
                    "text": str(block.get("text_raw") or ""),
                    "bbox": block.get("bbox"),
                    "kind": str(block.get("kind") or "text"),
                }
            )
        tables: list[dict[str, Any]] = []
        for table in page.get("tables", []) or []:
            if not isinstance(table, dict):
                continue
            tables.append(
                {
                    "table_id": str(table.get("table_id") or ""),
                    "caption": table.get("caption"),
                    "bbox": table.get("bbox"),
                    "row_count": table.get("row_count"),
                    "column_count": table.get("column_count"),
                    "cells": [
                        {
                            "row": cell.get("row_index"),
                            "col": cell.get("column_index"),
                            "rowspan": cell.get("rowspan"),
                            "colspan": cell.get("colspan"),
                            "text": str(cell.get("text_raw") or ""),
                            "header_role": cell.get("header_role"),
                            "bbox": cell.get("bbox"),
                        }
                        for cell in table.get("cells", []) or []
                        if isinstance(cell, dict)
                    ],
                }
            )
        return lines, tables

    def _stage_of(run_name: str) -> str | None:
        if "--native_parse--" in run_name:
            return "native_parse"
        if "--layout_parse--" in run_name:
            return "layout_parse"
        if "--extract--" in run_name:
            return "extract"
        return None

    channels: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    manifest_verified = False

    from bio_pdf.storage.result_reader import ResultReader

    try:
        manifest = ResultReader(root).read_published_manifest(job_id)
    except ResultCorruptError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RESULT_CORRUPT",
                "message": str(exc) or "published result is corrupted",
                "details": getattr(exc, "details", {}) or {},
            },
        ) from exc

    stage_runs_root = root / "jobs" / job_id / "stage_runs"
    current_run_ids: set[str] = set()

    if manifest is not None:
        manifest_verified = True
        stages = manifest.get("stages") or {}
        for stage_name, outputs in stages.items():
            if not isinstance(outputs, dict):
                continue
            for _logical, entry in outputs.items():
                if not isinstance(entry, dict):
                    continue
                rel = str(entry.get("relative_path") or "")
                parts = rel.split("/")
                if len(parts) >= 4 and parts[0] == "jobs" and parts[2] == "stage_runs":
                    current_run_ids.add(parts[3])
                filename = parts[-1] if parts else ""
                if stage_name in ("native_parse", "layout_parse") and filename == "normalized.json":
                    data = _verified_json(entry, label=f"{stage_name} normalized")
                    for page in data.get("pages", []) or []:
                        if not isinstance(page, dict) or int(page.get("page_number", -1)) != page_number:
                            continue
                        lines, tables = _page_view(page)
                        if not lines and not tables:
                            continue
                        channels.append(
                            {
                                "engine": "native" if stage_name == "native_parse" else "layout",
                                "run": parts[3] if len(parts) >= 4 else None,
                                "artifact_rel": rel,
                                "manifest_verified": True,
                                "lines": lines,
                                "tables": tables,
                            }
                        )
                elif stage_name == "extract" and filename == "region-ocr.json":
                    data = _verified_json(entry, label="region-ocr")
                    page_lines = (data.get("pages") or {}).get(str(page_number)) or []
                    conflicts = [
                        record
                        for record in (data.get("content_conflicts") or [])
                        if int(record.get("page_number") or -1) == page_number
                    ]
                    supersedes = [
                        record
                        for record in (data.get("native_superseded") or [])
                        if int(record.get("page_number") or -1) == page_number
                    ]
                    if page_lines or conflicts or supersedes:
                        channels.append(
                            {
                                "engine": "rapidocr_region",
                                "artifact_rel": rel,
                                "manifest_verified": True,
                                "lines": [
                                    {
                                        "text": str(ln.get("text") or ""),
                                        "bbox": [ln.get("x0"), ln.get("y0"), ln.get("x1"), ln.get("y1")],
                                    }
                                    for ln in page_lines
                                ],
                                "tables": [],
                                "content_conflicts": conflicts,
                                "native_superseded": supersedes,
                            }
                        )
                    # F09: adopted-candidate content is its OWN channel — it
                    # entered the feed from a human-adopted reparse, not from
                    # this run's region OCR; the two must never blur.
                    adopted_lines = (data.get("adopted_candidates") or {}).get(str(page_number)) or []
                    if adopted_lines:
                        channels.append(
                            {
                                "engine": "adopted_candidate",
                                "artifact_rel": rel,
                                "manifest_verified": True,
                                "lines": [
                                    {
                                        "text": str(ln.get("text") or ""),
                                        "bbox": [ln.get("x0"), ln.get("y0"), ln.get("x1"), ln.get("y1")],
                                    }
                                    for ln in adopted_lines
                                ],
                                "tables": [],
                            }
                        )

    # History: stage-run directories that are NOT part of the current
    # published manifest (failed/earlier attempts). Kept apart from the
    # current channels — an old attempt must never masquerade as current.
    if stage_runs_root.is_dir():
        for run_dir in sorted(stage_runs_root.iterdir()):
            if not run_dir.is_dir():
                continue
            stage = _stage_of(run_dir.name)
            if stage is None:
                continue
            is_current = run_dir.name in current_run_ids
            if is_current:
                continue
            stage_manifest = run_dir / "stage.manifest.json"
            history.append(
                {
                    "run": run_dir.name,
                    "stage": stage,
                    "status": "completed" if stage_manifest.is_file() else "failed_or_incomplete",
                    "artifact_rel": stage_manifest.relative_to(root).as_posix()
                    if stage_manifest.is_file()
                    else None,
                }
            )

    # Legacy fallback: no published manifest at all (old jobs) — read the
    # working stage_runs tree, clearly marked as unverified.
    if manifest is None and stage_runs_root.is_dir():
        for run_dir in sorted(stage_runs_root.iterdir()):
            if not run_dir.is_dir():
                continue
            stage = _stage_of(run_dir.name)
            if stage is None:
                continue
            if stage == "extract":
                sidecar = run_dir / "region-ocr.json"
                if sidecar.is_file():
                    try:
                        data = json.loads(sidecar.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        continue
                    lines = (data.get("pages") or {}).get(str(page_number)) or []
                    if lines:
                        channels.append(
                            {
                                "engine": "rapidocr_region",
                                "artifact_rel": sidecar.relative_to(root).as_posix(),
                                "manifest_verified": False,
                                "lines": [
                                    {"text": str(ln.get("text") or ""),
                                     "bbox": [ln.get("x0"), ln.get("y0"), ln.get("x1"), ln.get("y1")]}
                                    for ln in lines
                                ],
                                "tables": [],
                            }
                        )
                continue
            candidates = [run_dir / "normalized.json", *sorted(run_dir.glob("run-*/normalized.json"))]
            for path in candidates:
                if not path.is_file():
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                for page in data.get("pages", []) or []:
                    if not isinstance(page, dict) or int(page.get("page_number", -1)) != page_number:
                        continue
                    lines, tables = _page_view(page)
                    if not lines and not tables:
                        continue
                    channels.append(
                        {
                            "engine": "native" if stage == "native_parse" else "layout",
                            "run": path.parent.name if path.parent != run_dir else None,
                            "artifact_rel": path.relative_to(root).as_posix(),
                            "manifest_verified": False,
                            "lines": lines,
                            "tables": tables,
                        }
                    )

    channels.sort(key=lambda ch: (ch["engine"], str(ch.get("run") or "")))
    return {
        "api_version": "v1",
        "job_id": job_id,
        "page_number": page_number,
        "manifest_verified": manifest_verified,
        "channels": channels,
        "history": history,
    }


# ---- D09 issue actions (backend + audit log) -------------------------------


class _IssueActionRequest(BaseModel):
    action: str
    actor: str = "local-operator"
    note: str | None = None


#: H08/P05: the SINGLE source of truth for which issues have a bounded
#: page-level reparse path. The issue view exposes it as a capability so the
#: frontend never keeps a second, drifting copy.
_REPARSE_PAGE_CODES = {
    "SEQ_INVENTORY_MISSING",
    "ZERO_RESIDUE_LISTING",
    "SOURCE_CONFLICT",
    # G11/P07: a distorted table grid invites a bounded region re-parse.
    "TABLE_STRUCTURE_NO_EXTRACTION",
    "TABLE_STRUCTURE_SUSPECT",
    # Real legacy/structural table issues with a known page.
    "TABLE_STRUCTURE",
}


def _issue_or_404(service, job_id: str, issue_id: str):
    """E4/S04: an issue is addressed by ITS JOB. Reading it through another
    job's URL is a 404 with zero writes — never a cross-job mutation, and
    never a reparse whose source/page mix tasks."""

    row = service.repository.get_issue(issue_id)
    if row is None or str(row["job_id"]) != str(job_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "issue not found for this job"},
        )
    return row


def _issue_view(service, row) -> dict[str, Any]:
    issue = {
        "issue_id": row["issue_id"],
        "job_id": row["job_id"],
        "scope": row["scope"],
        "reason_code": row["reason_code"],
        "source_ids": json.loads(row["source_json"] or "[]"),
        "detail": row["detail"],
        "state": row["state"],
        "baseline_id": row["baseline_id"],
        "created_at": row["created_at"],
        "severity": _ISSUE_SEVERITY.get(str(row["reason_code"]), "warning"),
    }
    row_keys = row.keys()
    if "page_number" in row_keys and row["page_number"] is not None:
        issue["page_number"] = int(row["page_number"])
    else:
        issue["page_number"] = _issue_page(issue)
    if "region_json" in row_keys and row["region_json"]:
        try:
            issue["region"] = json.loads(row["region_json"])
        except json.JSONDecodeError:
            issue["region"] = None
    issue.update(
        _ISSUE_GUIDANCE.get(
            str(row["reason_code"]),
            {"title": str(row["reason_code"]), "impact": "见详情", "suggestion": "按详情人工核对"},
        )
    )
    # H08/P05: server-driven capabilities — the frontend renders exactly the
    # actions the backend can execute, with the artifact each one produces.
    reason = str(row["reason_code"])
    page = issue.get("page_number")
    actions: list[dict[str, Any]] = [
        {"action": "dismiss", "supported": True, "label": "标记非目标（需原因）", "requires_reason": True},
        {"action": "note", "supported": True, "label": "保留未解决（加说明）"},
        {"action": "reopen", "supported": True, "label": "恢复为待处理"},
    ]
    if reason in _REPARSE_PAGE_CODES and page is not None:
        actions.append(
            {
                "action": "reparse",
                "supported": True,
                "label": "区域复解析",
                "produces": (
                    "region OCR text candidate for this page (bounded reparse; "
                    "table GRID structure is NOT reconstructed — a human check "
                    "of the grid is still required)"
                ),
                "endpoint": f"POST /jobs/{row['job_id']}/issues/{row['issue_id']}/reparse",
            }
        )
    else:
        actions.append(
            {
                "action": "reparse",
                "supported": False,
                "label": "区域复解析（不可用）",
                "reason": (
                    "issue has no known page"
                    if page is None
                    else "this issue scope has no bounded region-reparse path"
                ),
            }
        )
    issue["allowed_actions"] = actions
    return issue


def _issue_events(service, issue_id: str) -> list[dict[str, Any]]:
    return [
        {
            "event_id": e["event_id"],
            "actor": e["actor"],
            "action": e["action"],
            "note": e["note"],
            "artifact_rel": e["artifact_rel"],
            "created_at": e["created_at"],
        }
        for e in service.repository.list_issue_events(issue_id)
    ]


@router.get("/issues/{issue_id}")
def get_issue(service: ServiceDep, job_id: str, issue_id: str) -> dict:
    """One issue with its full action history (D09)."""

    _job_or_404(service, job_id)
    row = _issue_or_404(service, job_id, issue_id)
    return {
        "api_version": "v1",
        "issue": _issue_view(service, row),
        "events": _issue_events(service, issue_id),
    }


@router.post("/issues/{issue_id}/actions")
def issue_action(
    service: ServiceDep,
    job_id: str,
    issue_id: str,
    request: _IssueActionRequest,
) -> dict:
    """dismiss / note / reopen — every action is logged with actor+note.

    - ``dismiss`` (标记非目标): requires a reason (note) — an unexplained
      dismissal is not an audit trail.
    - ``note`` (保留未解决并添加说明): requires a note; a dismissed issue
      reopens with it.
    - ``reopen``: returns a dismissed issue to the open state.
    """

    _job_or_404(service, job_id)
    row = _issue_or_404(service, job_id, issue_id)
    action = request.action
    note = (request.note or "").strip()

    if action == "dismiss":
        if not note:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ISSUE_NOTE_REQUIRED",
                    "message": "dismissing an issue requires a reason in note",
                    "details": {"issue_id": issue_id},
                },
            )
        service.repository.update_issue_state(issue_id, state="dismissed")
    elif action == "note":
        if not note:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ISSUE_NOTE_REQUIRED",
                    "message": "adding a note requires note text",
                    "details": {"issue_id": issue_id},
                },
            )
        if str(row["state"]) == "dismissed":
            service.repository.update_issue_state(issue_id, state="open")
    elif action == "reopen":
        service.repository.update_issue_state(issue_id, state="open")
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ISSUE_ACTION_UNKNOWN",
                "message": f"unknown issue action: {action}",
                "details": {"choices": ["dismiss", "note", "reopen"]},
            },
        )

    service.repository.connection.commit()
    service.repository.append_issue_event(
        issue_id=issue_id, actor=request.actor, action=action, note=note or None
    )
    refreshed = _issue_or_404(service, job_id, issue_id)
    return {
        "api_version": "v1",
        "issue": _issue_view(service, refreshed),
        "events": _issue_events(service, issue_id),
    }


@router.post("/issues/{issue_id}/reparse")
def issue_reparse(service: ServiceDep, job_id: str, issue_id: str) -> dict:
    """Bounded region reparse (D09): OCR the issue's own page, store the
    candidate artifact and log the action.

    Produces a CANDIDATE (never auto-published — adoption stays a human
    decision). Scopes without a bounded reparse path are refused with a
    reason instead of pretending to run.
    """

    _job_or_404(service, job_id)
    row = _issue_or_404(service, job_id, issue_id)
    view = _issue_view(service, row)
    page = view.get("page_number")
    reason = str(row["reason_code"])

    reparse_pages = _REPARSE_PAGE_CODES
    if reason not in reparse_pages or page is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ISSUE_REPARSE_UNSUPPORTED",
                "message": (
                    "bounded reparse is available for declarations/residue "
                    "regions with a known page; this issue has no such path"
                ),
                "details": {"issue_id": issue_id, "reason_code": reason, "page": page},
            },
        )

    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    doc_row = service.repository.connection.execute(
        "SELECT source_path FROM documents WHERE document_id = ?", (job.document_id,)
    ).fetchone()
    if doc_row is None or not Path(str(doc_row["source_path"])).is_file():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_MISSING",
                "message": "the source PDF is no longer available for reparse",
            },
        )

    from bio_pdf.extract.sequences.region_ocr import ocr_residue_pages

    # F02/S01: reserve the candidate IDENTITY first, in one atomic DB
    # transaction (unique candidate_id + per-issue display attempt). Two
    # concurrent requests can never share an identity or a file, and a
    # failure below consumes the reservation ('failed') instead of leaving
    # a reusable identity behind.
    reservation = service.repository.reserve_issue_candidate(issue_id=issue_id, job_id=job_id)
    candidate_id = str(reservation["candidate_id"])
    attempt = int(reservation["attempt"])

    try:
        ocr = ocr_residue_pages(Path(str(doc_row["source_path"])), [int(page)])
    except Exception as exc:  # noqa: BLE001 — the failure becomes the log entry
        service.repository.fail_issue_candidate(
            candidate_id=candidate_id, error=f"{type(exc).__name__}: {exc}"
        )
        service.repository.append_issue_event(
            issue_id=issue_id,
            actor="reparse",
            action="reparse_failed",
            note=(
                f"candidate {candidate_id} (attempt {attempt}) failed: "
                f"{type(exc).__name__}: {exc}"
            )[:300],
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "REPARSE_FAILED",
                "message": f"region OCR failed: {exc}",
                "details": {"page": page, "candidate_id": candidate_id, "attempt": attempt},
            },
        ) from exc

    lines = ocr.get(int(page), [])
    # region_ocr returns (x0, y0, x1, y1, text) — text is index 4 (E5).
    text = "\n".join(line[4] for line in lines)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # E3/S03 + F02/S01: every candidate has its OWN immutable identity and
    # directory. The file name carries the unique candidate_id (the
    # per-issue attempt is only the display number); a second run never
    # overwrites the file an earlier event references — the audit trail
    # must be able to replay exactly what it recorded.
    rel_dir = Path("jobs") / job_id / "issues" / issue_id
    out_dir = service.paths.artifact_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_name = f"candidate-{candidate_id}.txt"
    meta_name = f"candidate-{candidate_id}.meta.json"
    rel = (rel_dir / candidate_name).as_posix()
    meta_rel = (rel_dir / meta_name).as_posix()

    publication = service.repository.get_publication(job_id)
    doc_row = service.repository.connection.execute(
        "SELECT sha256 FROM documents WHERE document_id = ?", (job.document_id,)
    ).fetchone()
    region_raw = row["region_json"] if "region_json" in row.keys() else None
    meta = {
        "candidate_id": candidate_id,
        "issue_id": issue_id,
        "job_id": job_id,
        "attempt": attempt,
        "artifact_rel": rel,
        "meta_rel": meta_rel,
        "content_sha256": digest,
        "char_count": len(text),
        "line_count": len(lines),
        "page_number": int(page),
        "region": json.loads(region_raw) if region_raw else None,
        # F09: the 2D lines (boxes + text) travel with the candidate so an
        # ADOPTED candidate can be consumed as a real OCR source by the
        # new job's extract stage (float() kills numpy scalars — D10).
        "lines": [
            {
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
                "text": str(line_text),
            }
            for (x0, y0, x1, y1, line_text) in lines
        ],
        "source_sha256": str(doc_row["sha256"]) if doc_row else None,
        "baseline_id": str(publication["baseline_id"]) if publication else None,
        "params": {
            "engine": "rapidocr_region",
            "dpi": 300,
            "policy": "st25-residue-fallback",
        },
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    meta_text = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True)
    meta_digest = hashlib.sha256(meta_text.encode("utf-8")).hexdigest()

    # Exclusive create ("xb"): a collision is impossible by construction,
    # and a leftover file becomes a loud failure — never an in-place
    # overwrite of evidence another event may reference. Bytes, not text
    # mode, so the on-disk sha matches the recorded sha on every platform.
    try:
        with open(out_dir / candidate_name, "xb") as fh:
            fh.write(text.encode("utf-8"))
        with open(out_dir / meta_name, "xb") as fh:
            fh.write(meta_text.encode("utf-8"))
    except FileExistsError as exc:
        service.repository.fail_issue_candidate(
            candidate_id=candidate_id, error=f"artifact already exists: {exc}"
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CANDIDATE_ARTIFACT_EXISTS",
                "message": "candidate artifact path already exists — refusing to overwrite",
                "details": {"candidate_id": candidate_id, "artifact_rel": rel},
            },
        ) from exc
    except OSError as exc:
        service.repository.fail_issue_candidate(
            candidate_id=candidate_id, error=f"write failed: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CANDIDATE_WRITE_FAILED",
                "message": f"candidate artifact could not be written: {exc}",
                "details": {"candidate_id": candidate_id},
            },
        ) from exc

    committed = service.repository.finish_issue_candidate(
        candidate_id=candidate_id,
        artifact_rel=rel,
        content_sha256=digest,
        meta_sha256=meta_digest,
        meta_json=meta_text,
    )
    if not committed:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CANDIDATE_IDENTITY_LOST",
                "message": "the candidate reservation was no longer writable",
                "details": {"candidate_id": candidate_id},
            },
        )

    service.repository.update_issue_state(issue_id, state="reparse_requested")
    service.repository.connection.commit()
    service.repository.append_issue_event(
        issue_id=issue_id,
        actor="reparse",
        action="reparse_candidate",
        note=(
            f"attempt {attempt} candidate {candidate_id} sha256={digest[:16]} page {page}: "
            f"{len(lines)} OCR lines, {len(text)} chars"
            + (f"; preview: {text[:120]!r}" if text else "; region empty — still unresolved")
        ),
        artifact_rel=rel,
    )
    refreshed = _issue_or_404(service, job_id, issue_id)
    return {
        "api_version": "v1",
        "issue": _issue_view(service, refreshed),
        "candidate": {
            "candidate_id": candidate_id,
            "artifact_rel": rel,
            "meta_rel": meta_rel,
            "attempt": attempt,
            "content_sha256": digest,
            "meta_sha256": meta_digest,
            "line_count": len(lines),
            "char_count": len(text),
            "preview": text[:400],
            "note": "候选未自动采用；POST …/adopt 建立新基线",
        },
        "events": _issue_events(service, issue_id),
    }


class _AdoptRequest(BaseModel):
    attempt: int | None = None
    actor: str = "local-operator"
    #: I04/S04: when provided, every page must be a POSITIVE integer and the
    #: set must be non-empty — [0]/[-1]/[] are 422 at the model boundary.
    pages: list[PositiveInt] | None = None

    @field_validator("pages")
    @classmethod
    def _pages_non_empty(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(value) == 0:
            raise ValueError("pages must be non-empty when provided; omit it to target the candidate page")
        return value


@router.post("/issues/{issue_id}/adopt", status_code=201)
def issue_adopt(
    service: ServiceDep,
    job_id: str,
    issue_id: str,
    request: _AdoptRequest,
) -> dict:
    """E3/S03: adopting a reparse candidate creates a NEW formal job and
    baseline — it never edits the current publication in place.

    The candidate artifact's recorded sha256 is re-verified before adoption;
    the new job runs the SAME pipeline over the affected pages (engine
    docling so the residue fallback participates), producing its own
    immutable baseline and its own review queue. The issue keeps its full
    event history (adoption is an event, not a rewrite).
    """

    _job_or_404(service, job_id)
    row = _issue_or_404(service, job_id, issue_id)
    # F02/S01: candidates come from the reservation table (immutable
    # identity + hashes), not from scanning event notes.
    candidates = [
        cand
        for cand in service.repository.list_issue_candidates(issue_id, status="written")
    ]
    if not candidates:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NO_CANDIDATE_TO_ADOPT",
                "message": "run 区域复解析 first — there is no candidate to adopt",
            },
        )
    chosen = None
    for cand in candidates:
        if request.attempt is None or int(cand["attempt"]) == int(request.attempt):
            chosen = cand
    if chosen is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CANDIDATE_NOT_FOUND",
                "message": f"no reparse candidate attempt {request.attempt}",
            },
        )
    artifact = service.paths.artifact_root / str(chosen["artifact_rel"])
    meta_path = artifact.with_name(artifact.name.replace(".txt", ".meta.json"))
    if not artifact.is_file() or not meta_path.is_file():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CANDIDATE_MISSING",
                "message": "the candidate artifact is no longer readable",
            },
        )
    from bio_pdf.storage.artifacts import compute_sha256_bytes

    recorded_meta_sha = chosen["meta_sha256"] if "meta_sha256" in chosen.keys() else None
    if recorded_meta_sha and compute_sha256_bytes(meta_path.read_bytes()) != recorded_meta_sha:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CANDIDATE_META_HASH_MISMATCH",
                "message": "candidate metadata no longer matches its recorded hash",
            },
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    actual_content_sha = compute_sha256_bytes(artifact.read_bytes())
    recorded_content_sha = str(chosen["content_sha256"] or meta.get("content_sha256") or "")
    if actual_content_sha != recorded_content_sha or actual_content_sha != meta.get("content_sha256"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CANDIDATE_HASH_MISMATCH",
                "message": "candidate content no longer matches its recorded hash",
            },
        )

    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    doc_row = service.repository.connection.execute(
        "SELECT source_path FROM documents WHERE document_id = ?", (job.document_id,)
    ).fetchone()
    if doc_row is None or not Path(str(doc_row["source_path"])).is_file():
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_MISSING", "message": "document source unavailable"},
        )

    # I04/S04: prove the explicit-vs-default branches instead of an eagerly
    # evaluated default (``meta.get(..., pages[0])`` raised IndexError on an
    # empty filtered list and produced a 500).
    requested_pages = [int(p) for p in (request.pages or [])]
    meta_page_raw = meta.get("page_number")
    candidate_page = (
        int(meta_page_raw)
        if meta_page_raw is not None
        else (requested_pages[0] if requested_pages else 0)
    )
    if candidate_page <= 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ADOPT_PAGE_INVALID",
                "message": "the candidate page is unknown or not a positive integer",
                "details": {"candidate_page": candidate_page},
            },
        )
    pages = requested_pages or [candidate_page]
    # H02/S02: the adopted page MUST be inside the requested page set —
    # otherwise the worker would silently skip the candidate while the API
    # still reported success. Reject BEFORE the job is created and before
    # any adopted event is written.
    if candidate_page not in pages:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ADOPT_PAGE_OUT_OF_SCOPE",
                "message": (
                    f"candidate targets page {candidate_page}, which is not in the "
                    f"requested page set {pages} — the adoption would never be consumed"
                ),
                "details": {"candidate_page": candidate_page, "requested_pages": pages},
            },
        )
    candidate_text = artifact.read_text(encoding="utf-8", errors="replace")
    # F09: adoption is REAL consumption — the candidate (id, content hash,
    # source SHA, origin baseline, page/region) becomes part of the new
    # job's persisted inputs, and the extract stage feeds its content into
    # the extraction for that page. A copy is hash-verified into the job's
    # own adopted/ directory at creation time.
    try:
        new_job, _page_count = service.create_job(
            Path(str(doc_row["source_path"])),
            engine="docling",
            page_numbers=tuple(pages),
            adopted_candidates=[
            {
                "candidate_id": str(chosen["candidate_id"]),
                "artifact_path": str(artifact),
                "content_sha256": actual_content_sha,
                "meta_json": meta_path.read_text(encoding="utf-8"),
                "source_sha256": meta.get("source_sha256"),
                "baseline_id": meta.get("baseline_id"),
                "origin_job_id": job_id,
                "origin_issue_id": issue_id,
                    "page_number": candidate_page,
                    "region": meta.get("region") if isinstance(meta.get("region"), dict) else None,
                }
            ],
        )
    except WorkflowError as exc:
        details = getattr(exc, "details", None) or {}
        from bio_pdf.errors import PageRangeInvalidError

        if isinstance(exc, PageRangeInvalidError):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PAGE_RANGE_INVALID",
                    "message": exc.message,
                    "details": details,
                },
            ) from exc
        if isinstance(details, dict) and details.get("code") in (
            "ADOPT_PAGE_INVALID",
            "ADOPT_PAGE_OUT_OF_SCOPE",
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": details["code"], "message": exc.message, "details": details},
            ) from exc
        raise
    service.repository.append_issue_event(
        issue_id=issue_id,
        actor=request.actor,
        action="adopted",
        note=(
            f"adopted candidate {chosen['candidate_id']} "
            f"(sha256={actual_content_sha[:16]}, {len(candidate_text)} chars) → new job "
            f"{new_job.job_id} over pages {pages}; the candidate content is CONSUMED by "
            "the new job's extract stage (input recorded in its effective config)"
        ),
        artifact_rel=chosen["artifact_rel"],
    )
    return {
        "api_version": "v1",
        "issue": _issue_view(service, row),
        "adopted": {
            "candidate_id": str(chosen["candidate_id"]),
            "candidate_rel": chosen["artifact_rel"],
            "content_sha256": actual_content_sha,
            # H02/S02: enqueueing is NOT consumption. The job is SCHEDULED
            # to consume this candidate; the proof lives in the published
            # artifact (extract.adopted_candidates with merged_block_ids).
            "status": "scheduled",
            "consumption_proof": "jobs/<job_id> extract artifact: adopted_candidates[].merged_block_ids",
            "new_job_id": new_job.job_id,
            "pages": list(pages),
        },
        "events": _issue_events(service, issue_id),
    }
