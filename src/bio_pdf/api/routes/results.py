"""Result routes: records, page projection, evidence and artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
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

    C07: ``page_number`` filters to one page (the 图表 tab follows the
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
            note = f"第 {page_number} 页没有图表（全文共 {total} 个，可切换“全文”查看）。"
        else:
            pages = [p for p in data.get("pages", []) or [] if isinstance(p, dict)]
            layout_seen = any(str(p.get("route", "")).startswith("layout_") for p in pages)
            if not layout_seen:
                note = (
                    "此任务未使用结构增强引擎（Docling），没有图表区域检测；"
                    "重新创建任务并选择“结构增强”后可提取统计图/曲线图。"
                )
            else:
                note = "结构引擎在本文件未检测到任何图区（可能是纯文本论文或图表未被识别）。"
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
    issues: list[dict[str, Any]] = []
    for row in rows:
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
        }
        issue["severity"] = _ISSUE_SEVERITY.get(str(row["reason_code"]), "warning")
        # D09: prefer the page/region captured at creation time; the
        # fallbacks below only serve historical rows.
        row_keys = row.keys()
        if "region_json" in row_keys and row["region_json"]:
            try:
                issue["region"] = json.loads(row["region_json"])
            except json.JSONDecodeError:
                issue["region"] = None
        if "page_number" in row_keys and row["page_number"] is not None:
            issue["page_number"] = int(row["page_number"])
        else:
            issue["page_number"] = _issue_page(issue)
        issued_guidance = _ISSUE_GUIDANCE.get(str(row["reason_code"]))
        if issued_guidance:
            issue.update(issued_guidance)
        else:
            issue.update(
                {
                    "title": str(row["reason_code"]),
                    "impact": "见详情",
                    "suggestion": "按详情人工核对",
                }
            )
        issues.append(issue)

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


# ---- D09 issue actions (backend + audit log) -------------------------------


class _IssueActionRequest(BaseModel):
    action: str
    actor: str = "local-operator"
    note: str | None = None


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

    reparse_pages = {
        "SEQ_INVENTORY_MISSING",
        "ZERO_RESIDUE_LISTING",
        "SOURCE_CONFLICT",
    }
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

    try:
        ocr = ocr_residue_pages(Path(str(doc_row["source_path"])), [int(page)])
    except Exception as exc:  # noqa: BLE001 — the failure becomes the log entry
        service.repository.append_issue_event(
            issue_id=issue_id,
            actor="reparse",
            action="reparse_failed",
            note=f"{type(exc).__name__}: {exc}"[:300],
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "REPARSE_FAILED",
                "message": f"region OCR failed: {exc}",
                "details": {"page": page},
            },
        ) from exc

    lines = ocr.get(int(page), [])
    # region_ocr returns (x0, y0, x1, y1, text) — text is index 4 (E5).
    text = "\n".join(line[4] for line in lines)
    rel_dir = Path("jobs") / job_id / "issues" / issue_id
    out_dir = service.paths.artifact_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = out_dir / "candidate_ocr.txt"
    candidate.write_text(text, encoding="utf-8")
    rel = (rel_dir / "candidate_ocr.txt").as_posix()

    service.repository.update_issue_state(issue_id, state="reparse_requested")
    service.repository.connection.commit()
    service.repository.append_issue_event(
        issue_id=issue_id,
        actor="reparse",
        action="reparse_candidate",
        note=(
            f"page {page}: {len(lines)} OCR lines, {len(text)} chars"
            + (f"; preview: {text[:120]!r}" if text else "; region empty — still unresolved")
        ),
        artifact_rel=rel,
    )
    refreshed = _issue_or_404(service, job_id, issue_id)
    return {
        "api_version": "v1",
        "issue": _issue_view(service, refreshed),
        "candidate": {
            "artifact_rel": rel,
            "line_count": len(lines),
            "char_count": len(text),
            "preview": text[:400],
            "note": "候选未自动采用；人工核对后才能并入基线",
        },
        "events": _issue_events(service, issue_id),
    }
