"""Review plan builder.

Sorts candidate records into a review queue, prioritising concrete
character/value conflicts over relationship/condition gaps. The budget caps
the number of auto-created review items.
"""

from __future__ import annotations

import hashlib
from typing import Any

from bio_pdf.contracts import ReviewItem

#: Reason codes by priority (lower index = review sooner).
_PRIORITY_ORDER = {
    "SEQUENCE_CONFLICT": 0,
    "ACT_VALUE_CONFLICT": 0,
    "SEQ_DECLARED_LENGTH_MATCH": 1,
    "ACT_UNIT_PRESENT": 1,
    "ACT_ENTITY_LINK": 2,
    "ACT_TARGET_LINK": 2,
    "LINK_UNRESOLVED": 3,
}


def _priority(reason_codes: list[str]) -> int:
    if not reason_codes:
        return 4
    return min(_PRIORITY_ORDER.get(code, 4) for code in reason_codes)


def build_review_plan(
    records: list[dict[str, Any]],
    budget: int,
    *,
    baseline_id: str | None = None,
) -> list[ReviewItem]:
    """Build an ordered review plan within ``budget`` items.

    Args:
        records: List of record dicts (``model_dump()`` views). Each must
            provide ``record_id`` (or ``sequence_id``/``measurement_id``),
            ``job_id``, ``validation_state``, and optionally ``reason_codes``
            / ``evidence_ids`` / ``review_reasons``.
        budget: Maximum number of review items to return (0 = unlimited).

    Returns:
        A list of :class:`ReviewItem`, prioritised by conflict severity.
    """

    items: list[ReviewItem] = []
    candidates: list[tuple[int, dict[str, Any]]] = []

    for record in records:
        reason_codes = list(record.get("reason_codes", []) or [])
        validation_state = record.get("validation_state", "unchecked")
        if validation_state == "conflict" and "SEQUENCE_CONFLICT" not in reason_codes:
            reason_codes.append("VALIDATION_CONFLICT")
        if not reason_codes and validation_state != "conflict":
            # No conflict and no reasons: nothing to review.
            continue

        record_id = record.get("record_id") or record.get("sequence_id") or record.get("measurement_id")
        if not record_id:
            continue
        candidates.append((_priority(reason_codes), record))

    candidates.sort(key=lambda pair: pair[0])

    for idx, (_prio, record) in enumerate(candidates):
        if budget > 0 and len(items) >= budget:
            break
        record_id = record.get("record_id") or record.get("sequence_id") or record.get("measurement_id")
        job_id = str(record.get("job_id", ""))
        # Stable, content-derived id: re-deriving the same baseline plan
        # yields the same ids, so a resumed/re-run job never reopens a
        # resolved review item (plan T03).
        revision = int(record.get("record_revision", 1) or 1)
        # E1/S01: baseline in the identity — a new baseline re-derives
        # DIFFERENT ids, so publish-time upserts can never rebind a stale
        # review onto new content.
        digest = hashlib.sha256(
            f"{job_id}|{baseline_id or ''}|{record_id}|{revision}".encode("utf-8")
        ).hexdigest()[:16]
        items.append(
            ReviewItem(
                review_id=f"review-{digest}",
                job_id=job_id,
                record_id=str(record_id),
                record_revision=revision,
                baseline_id=baseline_id,
                reason_codes=list(record.get("reason_codes", []) or []),
                evidence_ids=list(record.get("evidence_ids", []) or []),
                proposed_actions=list(record.get("proposed_actions", []) or []),
                status="open",
            )
        )
    return items


__all__ = ["build_review_plan"]