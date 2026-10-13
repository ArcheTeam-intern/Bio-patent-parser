"""Evidence-level validation.

Checks whether a record's evidence references actually exist and carry
geometry. A record with no evidence cannot be accepted, and a reference to a
missing evidence id is flagged (never silently treated as "no evidence").
"""

from __future__ import annotations

from typing import Any

from bio_pdf.contracts import CheckResult, ValidationResult

CHECK_EVIDENCE_EXISTS = "EVIDENCE_EXISTS"
CHECK_EVIDENCE_GEOMETRY = "EVIDENCE_GEOMETRY_VALID"


def _check(check_id: str, outcome: str, details: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        outcome=outcome,  # type: ignore[arg-type]
        details={"message": details},
    )


def validate_evidence(
    evidence_ids: list[str],
    evidence_index: dict[str, Any] | None,
) -> ValidationResult:
    """Validate evidence existence and geometry.

    Args:
        evidence_ids: Evidence ids referenced by a record.
        evidence_index: Optional mapping of ``evidence_id → span``. When
            ``None``, existence is unknown (not asserted).

    Returns:
        A :class:`ValidationResult` with ``EVIDENCE_EXISTS`` and (when the
        index is available) ``EVIDENCE_GEOMETRY_VALID`` checks.
    """

    checks: list[dict[str, str]] = []

    if not evidence_ids:
        checks.append(
            _check(CHECK_EVIDENCE_EXISTS, "fail", "no evidence ids present")
        )
        return ValidationResult(
            checks=checks, validation_state="conflict", review_reasons=["record has no evidence"]
        )

    if evidence_index is None:
        checks.append(_check(CHECK_EVIDENCE_EXISTS, "unknown", "evidence index unavailable"))
    else:
        missing = [eid for eid in evidence_ids if eid not in evidence_index]
        if missing:
            checks.append(
                _check(CHECK_EVIDENCE_EXISTS, "fail", f"missing evidence: {missing}")
            )
            return ValidationResult(
                checks=checks,
                validation_state="conflict",
                review_reasons=[f"missing evidence ids: {missing}"],
            )
        checks.append(_check(CHECK_EVIDENCE_EXISTS, "pass", f"{len(evidence_ids)} evidence refs"))

        # Geometry check: any span with page/bbox counts as anchored.
        unanchored = [
            eid
            for eid in evidence_ids
            if not (getattr(evidence_index.get(eid), "page_number", None) is not None
                    or getattr(evidence_index.get(eid), "bbox", None) is not None)
        ]
        if unanchored:
            checks.append(
                _check(CHECK_EVIDENCE_GEOMETRY, "unknown", f"unanchored evidence: {unanchored}")
            )
        else:
            checks.append(_check(CHECK_EVIDENCE_GEOMETRY, "pass", "all evidence has geometry"))

    return ValidationResult(checks=checks, validation_state="checks_passed", review_reasons=[])


__all__ = ["validate_evidence", "CHECK_EVIDENCE_EXISTS", "CHECK_EVIDENCE_GEOMETRY"]