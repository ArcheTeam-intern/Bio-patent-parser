"""Measurement validation rules."""

from __future__ import annotations

from typing import Any

from bio_pdf.contracts import CheckResult, Measurement, ValidationResult

CHECK_VALUE = "ACT_VALUE_PARSE"
CHECK_UNIT = "ACT_UNIT_PRESENT"
CHECK_ENTITY = "ACT_ENTITY_LINK"
CHECK_TARGET = "ACT_TARGET_LINK"
CHECK_EVIDENCE_KIND = "ACT_EVIDENCE_KIND"


def _check(check_id: str, outcome: str, details: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        outcome=outcome,  # type: ignore[arg-type]
        details={"message": details},
    )


def validate_measurement(
    record: Measurement,
    evidence_index: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate a :class:`Measurement`.

    Checks that the value parsed, a unit is present, and the entity/target
    links are non-empty where required. A categorical ``ND`` is a valid
    parse (it is an explicit "not determined", not a missing value).
    """

    checks: list[CheckResult] = []
    reasons: list[str] = []

    if record.value.kind == "unparsed":
        checks.append(_check(CHECK_VALUE, "fail", f"value unparsed: {record.value.raw!r}"))
        reasons.append(f"value could not be parsed: {record.value.raw!r}")
    elif record.value.kind == "categorical":
        checks.append(_check(CHECK_VALUE, "pass", f"categorical value: {record.value.qualifier}"))
    else:
        checks.append(_check(CHECK_VALUE, "pass", f"value kind: {record.value.kind}"))

    # C05: the unit requirement comes from the metric registry — a
    # categorical S/R/I susceptibility result has no concentration unit by
    # design and must not be reported as "missing unit" (255 spurious review
    # items in the demo corpus).
    from bio_pdf.extract.activities.metrics import METRIC_REGISTRY

    definition = METRIC_REGISTRY.get(record.metric)
    unit_requirement = definition.unit_requirement if definition else "required"
    if record.unit_raw:
        checks.append(_check(CHECK_UNIT, "pass", f"unit present: {record.unit_raw}"))
    elif unit_requirement == "not_applicable":
        checks.append(
            _check(
                CHECK_UNIT,
                "pass",
                f"unit not applicable for metric {record.metric}",
            )
        )
    elif unit_requirement == "optional":
        checks.append(
            _check(
                CHECK_UNIT,
                "unknown",
                f"unit optional for metric {record.metric}; none recorded",
            )
        )
    else:
        checks.append(_check(CHECK_UNIT, "unknown", "no unit recorded"))
        reasons.append("measurement has no unit")

    if record.entity_id:
        checks.append(_check(CHECK_ENTITY, "pass", f"entity linked: {record.entity_id}"))
    else:
        checks.append(_check(CHECK_ENTITY, "unknown", "no entity linked"))
        reasons.append("measurement has no linked entity")

    if record.target_raw:
        checks.append(_check(CHECK_TARGET, "pass", f"target linked: {record.target_raw}"))
    else:
        checks.append(_check(CHECK_TARGET, "unknown", "no target linked"))

    if record.evidence_kind == "undetermined":
        checks.append(_check(CHECK_EVIDENCE_KIND, "unknown", "evidence kind undetermined"))
    else:
        checks.append(_check(CHECK_EVIDENCE_KIND, "pass", f"evidence kind: {record.evidence_kind}"))

    state = "conflict" if any(c.outcome == "fail" for c in checks) else "checks_passed"
    return ValidationResult(checks=checks, validation_state=state, review_reasons=reasons)


__all__ = ["validate_measurement", "CHECK_VALUE", "CHECK_UNIT", "CHECK_ENTITY", "CHECK_TARGET", "CHECK_EVIDENCE_KIND"]