"""Human review decisions.

Correct/reject actions append a :class:`ReviewDecision` to the repository
(audit history). Raw evidence is never mutated: a correction stores a field
patch keyed to a ``base_revision``, and a stale revision raises
:class:`ReviewRevisionConflictError`.
"""

from __future__ import annotations

import uuid
from typing import Any

from bio_pdf.contracts import ReviewDecision
from bio_pdf.errors import ReviewRevisionConflictError
from bio_pdf.storage.repository import Repository


def apply_decision(
    repository: Repository,
    *,
    review_id: str,
    actor: str,
    action: str,
    base_revision: int,
    patch: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
) -> ReviewDecision:
    """Persist a human decision against a review item.

    Raises:
        ReviewRevisionConflictError: If ``base_revision`` does not match the
            live record revision (the record changed since the review was
            opened).
    """

    decision = ReviewDecision(
        decision_id=f"dec-{uuid.uuid4().hex[:12]}",
        review_id=review_id,
        actor=actor,
        action=action,  # type: ignore[arg-type]
        patch_json=patch or {},
        evidence_ids=evidence_ids or [],
        base_revision=base_revision,
    )
    try:
        repository.append_review_decision(decision)
    except ReviewRevisionConflictError:
        raise
    return decision


__all__ = ["apply_decision"]