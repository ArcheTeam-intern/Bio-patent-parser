"""Review queue read routes.

Decision submission (confirm/correct/reject with atomic versioning) lands
with the D3 ReviewService; this router exposes the queue so the frontend
can build against real data from D1 on.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from bio_pdf.api.contracts import ReviewItemView, ReviewsResponse
from bio_pdf.api.dependencies import ServiceDep

router = APIRouter(prefix="/reviews", tags=["reviews"])

_KIND_PREFIXES = (
    ("sha256:", "seq:", "sequence"),
    ("sha256:", "prose:", "measurement"),
)


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
    status: str = "open",
    limit: int = 50,
) -> ReviewsResponse:
    limit = max(1, min(limit, 200))
    clauses = ["status = ?"]
    params: list[object] = [status]
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    rows = service.repository.connection.execute(
        f"SELECT * FROM review_items WHERE {' AND '.join(clauses)} LIMIT {limit}",
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
    return ReviewsResponse(reviews=items, total=len(items))


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


__all__ = ["router"]
