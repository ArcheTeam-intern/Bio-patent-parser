"""Review queue and decision routes (design §8).

Decisions run through :class:`ReviewService` — one atomic transaction with
base_revision conflict protection. A stale revision returns 409 with both
values; the client re-checks, never overwrites.

C06: the queue response also carries a ``groups`` summary — one group per
(job, page/table cluster) so 192 identical-looking matrix cells render as
"第 8 页 · 药敏矩阵 (192 条)" instead of a wall of duplicate-looking rows; a
bulk decision endpoint applies checked children with per-item revisions.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bio_pdf.api.contracts import ReviewDecisionRequest, ReviewDecisionResponse, ReviewItemView, ReviewsResponse
from bio_pdf.api.dependencies import ServiceDep
from bio_pdf.application.review_service import ReviewService
from bio_pdf.errors import ReviewRevisionConflictError, WorkflowError

router = APIRouter(prefix="/reviews", tags=["reviews"])


def _record_kind(record_id: str) -> str:
    if ":seq:" in record_id or ":inline:" in record_id:
        return "sequence"
    if ":prose:" in record_id or ":meas:" in record_id:
        return "measurement"
    return "unknown"


#: ``…:meas:<page>:grid:rNcM`` — a reconstructed susceptibility cell.
_GRID_RE = re.compile(r":meas:(\d+):grid:")
#: ``…:meas:<page>:table:<id>:…`` or ``:meas:<page>:prose:…``.
_PAGE_RE = re.compile(r":meas:(\d+):([a-z_]+)")


def _group_for(record_id: str) -> tuple[str, str, int | None]:
    """(group_key_suffix, label, page) — derived from the record id only."""

    grid = _GRID_RE.search(record_id)
    if grid:
        page = int(grid.group(1))
        return f"p{page}:matrix", f"第 {page} 页 · 药敏矩阵", page
    page_match = _PAGE_RE.search(record_id)
    if page_match:
        page = int(page_match.group(1))
        source = page_match.group(2)
        if source == "table":
            return f"p{page}:table", f"第 {page} 页 · 表格", page
        return f"p{page}:prose", f"第 {page} 页 · 正文", page
    if ":seq:" in record_id or ":inline:" in record_id:
        return "sequences", "序列记录", None
    return "other", "其他记录", None


@router.get("", response_model=ReviewsResponse)
def list_reviews(
    service: ServiceDep,
    job_id: str | None = None,
    record_id: str | None = None,
    status: str = "open",
    limit: int = 100,
    offset: int = 0,
) -> ReviewsResponse:
    """Paged queue listing whose ``total`` is the TRUE matching count.

    Queues can have hundreds of entries (every cell of a reconstructed
    susceptibility matrix is one review item); a truncated page must never
    masquerade as the whole queue. ``record_id`` narrows to the review item
    of one record — the workbench panel relies on it so a record beyond the
    first page is still found. ``groups`` counts the WHOLE filtered queue
    (not just the page) so the UI can say "N 条 / M 组".
    """

    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    clauses = ["status = ?"]
    params: list[object] = [status]
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if record_id:
        clauses.append("record_id = ?")
        params.append(record_id)
    where = " AND ".join(clauses)
    conn = service.repository.connection
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM review_items WHERE {where}", params
    ).fetchone()
    total = int(total_row["n"]) if total_row is not None else 0
    rows = conn.execute(
        f"SELECT * FROM review_items WHERE {where} ORDER BY rowid DESC LIMIT {limit} OFFSET {offset}",
        params,
    ).fetchall()
    items = [
        ReviewItemView(
            review_id=row["review_id"],
            job_id=row["job_id"],
            record_id=row["record_id"],
            record_kind=_record_kind(row["record_id"]),  # type: ignore[arg-type]
            record_revision=row["record_revision"],
            reason_codes=json.loads(row["reason_codes_json"] or "[]"),
            status=row["status"],
            evidence_ids=json.loads(row["evidence_ids_json"] or "[]"),
        )
        for row in rows
    ]

    # Group counts over the WHOLE filtered queue (a page-only count would
    # understate a 192-item matrix group).
    group_rows = conn.execute(
        f"SELECT job_id, record_id FROM review_items WHERE {where}", params
    ).fetchall()
    groups: dict[str, dict] = {}
    for row in group_rows:
        suffix, label, page = _group_for(str(row["record_id"]))
        key = f"{row['job_id']}|{suffix}"
        entry = groups.setdefault(
            key,
            {"group_key": key, "job_id": row["job_id"], "label": label, "page": page, "count": 0},
        )
        entry["count"] += 1
    return ReviewsResponse(
        reviews=items,
        total=total,
        groups=sorted(groups.values(), key=lambda g: (g["job_id"], g["page"] or 0, g["label"])),
    )


class BatchDecisionItem(BaseModel):
    review_id: str
    action: str
    base_revision: int
    reason: str | None = None
    patch: dict = Field(default_factory=dict)


class BatchDecisionRequest(BaseModel):
    actor: str = "local-operator"
    decisions: list[BatchDecisionItem] = Field(min_length=1)


@router.post("/decisions/batch")
def submit_batch_decision(service: ServiceDep, request: BatchDecisionRequest) -> dict:
    """D01 bulk decision: one transaction for the whole batch.

    Any failure (missing reject reason, illegal patch, blocker, revision
    race, duplicate item) rolls back EVERYTHING — the response carries the
    offending item's structured error and nothing was written.
    """

    review_service = ReviewService(service.repository, service.paths.artifact_root)
    try:
        result = review_service.apply_batch(
            decisions=[d.model_dump() for d in request.decisions],
            actor=request.actor,
        )
    except ReviewRevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REVIEW_BATCH_CONFLICT",
                "message": exc.message,
                "details": {"conflicts": [exc.details], "applied": []},
            },
        ) from exc
    except WorkflowError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": exc.message,
                "details": {**exc.details, "applied": []},
            },
        ) from exc
    return {"api_version": "v1", **result}


@router.get("/{review_id}/decisions")
def list_decisions(service: ServiceDep, review_id: str) -> dict:
    decisions = service.repository.list_review_decisions(review_id)
    if not decisions:
        # Distinguish "unknown review" from "no decisions yet".
        row = service.repository.connection.execute(
            "SELECT review_id FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "review not found"})
    return {
        "api_version": "v1",
        "review_id": review_id,
        "decisions": [d.model_dump(mode="json") for d in decisions],
    }


@router.post("/{review_id}/decisions", response_model=ReviewDecisionResponse)
def submit_decision(
    service: ServiceDep,
    review_id: str,
    request: ReviewDecisionRequest,
) -> ReviewDecisionResponse:
    review_service = ReviewService(service.repository, service.paths.artifact_root)
    try:
        outcome = review_service.apply_decision(
            review_id=review_id,
            action=request.action,
            base_revision=request.base_revision,
            actor=request.actor,
            reason=request.reason,
            patch=request.patch,
            evidence_ids=request.evidence_ids,
        )
    except ReviewRevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        ) from exc
    except WorkflowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc

    return ReviewDecisionResponse(
        review_id=outcome.review_id,
        action=outcome.action,
        record_revision=outcome.new_revision,
        accepted=outcome.acceptance_state == "accepted",
        validation_state=outcome.validation_state,
    )


__all__ = ["router"]
