"""Sequence validation rules.

Each rule returns a structured check (pass/fail/unknown + reason), never a
bare boolean. The aggregate :class:`ValidationResult` carries the resolved
``validation_state``.
"""

from __future__ import annotations

from typing import Any

from bio_pdf.contracts import CheckResult, SequenceRecord, ValidationResult
from bio_pdf.extract.sequences.amino_acids import ALLOWED_ALPHABETS

CHECK_LENGTH = "SEQ_DECLARED_LENGTH_MATCH"
CHECK_ALPHABET = "SEQ_ALLOWED_ALPHABET"
CHECK_POSITION = "SEQ_POSITION_CONTINUITY"
CHECK_COMPLETENESS = "SEQ_COMPLETENESS"
CHECK_REFERENCE = "SEQ_REFERENCE_MATCH"


def _check(check_id: str, outcome: str, details: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        outcome=outcome,  # type: ignore[arg-type]
        details={"message": details},
    )


def _allowed_alphabet(molecule_type: str) -> frozenset[str] | None:
    return ALLOWED_ALPHABETS.get(molecule_type)


def validate_sequence(
    record: SequenceRecord,
    evidence_index: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate a :class:`SequenceRecord`.

    The result never overrides the caller's ``acceptance_state``; it only
    reports the structured ``validation_state`` and review reasons.
    """

    checks: list[CheckResult] = []
    reasons: list[str] = []

    # Declared vs observed length.
    if record.declared_length is not None and record.observed_length is not None:
        if record.declared_length == record.observed_length:
            checks.append(_check(CHECK_LENGTH, "pass", "declared length matches observed"))
        else:
            checks.append(
                _check(
                    CHECK_LENGTH,
                    "fail",
                    f"declared {record.declared_length} != observed {record.observed_length}",
                )
            )
            reasons.append("declared length does not match observed length")
    else:
        checks.append(_check(CHECK_LENGTH, "unknown", "length not fully known"))

    # Allowed alphabet.
    alphabet = _allowed_alphabet(record.molecule_type)
    if alphabet is not None and record.normalized_sequence:
        disallowed = sorted({c for c in record.normalized_sequence if c not in alphabet})
        if disallowed:
            checks.append(
                _check(CHECK_ALPHABET, "fail", f"disallowed residues: {disallowed}")
            )
            reasons.append(f"sequence contains disallowed residues: {disallowed}")
        else:
            checks.append(_check(CHECK_ALPHABET, "pass", "all residues in allowed alphabet"))
    else:
        checks.append(_check(CHECK_ALPHABET, "unknown", "alphabet not defined for molecule type"))

    # Completeness. A sequence explicitly DISPLAYED as a fragment is
    # complete in its own scope (plan B07): rejecting it forever blocked
    # the review loop for genuinely displayed short peptides.
    is_displayed_fragment = (
        getattr(record, "sequence_scope", "undetermined") == "fragment"
        and getattr(record, "is_explicit_sequence", False)
    )
    if record.is_complete:
        checks.append(_check(CHECK_COMPLETENESS, "pass", "sequence marked complete"))
    elif is_displayed_fragment:
        checks.append(
            _check(
                CHECK_COMPLETENESS,
                "pass",
                "explicitly displayed fragment; full-listing completeness not claimed",
            )
        )
    else:
        checks.append(_check(CHECK_COMPLETENESS, "fail", "sequence incomplete or trailing"))
        reasons.append("sequence is incomplete")

    # Reference match cannot be determined without an official reference.
    checks.append(_check(CHECK_REFERENCE, "unknown", "no official reference to compare"))

    state = "conflict" if reasons else "checks_passed"
    return ValidationResult(checks=checks, validation_state=state, review_reasons=reasons)


__all__ = ["validate_sequence", "CHECK_LENGTH", "CHECK_ALPHABET", "CHECK_POSITION", "CHECK_COMPLETENESS", "CHECK_REFERENCE"]