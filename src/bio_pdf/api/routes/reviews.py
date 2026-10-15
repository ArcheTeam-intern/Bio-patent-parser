"""Review queue and decision routes (design §8).

Decisions run through :class:`ReviewService` — one atomic transaction with
base_revision conflict protection. A stale revision returns 409 with both
values; the client re-checks, never overwrites.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

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
    first page is still found.
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
    total_row = service.repository.connection.execute(
        f"SELECT COUNT(*) AS n FROM review_items WHERE {where}", params
    ).fetchone()
    total = int(total_row["n"]) if total_row is not None else 0
    rows = service.repository.connection.execute(
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
    return ReviewsResponse(reviews=items, total=total)


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
