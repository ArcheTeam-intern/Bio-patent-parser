"""Acceptance policy tests."""

from __future__ import annotations

from bio_pdf.validation.acceptance import decide_acceptance


def test_length_and_alphabet_success_do_not_prove_sequence_truth() -> None:
    record = {
        "record_kind": "sequence",
        "validation_state": "checks_passed",
        "acceptance_state": "candidate",
        "is_complete": True,
        "checks": [{"check_id": "SEQ_DECLARED_LENGTH_MATCH", "outcome": "pass"}],
        "evidence_ids": ["ev1"],
    }
    decision = decide_acceptance(record, auto_accept=False)
    assert decision.acceptance_state == "needs_review"


def test_conflict_requires_review() -> None:
    record = {
        "validation_state": "conflict",
        "checks": [{"check_id": "SEQ_DECLARED_LENGTH_MATCH", "outcome": "fail"}],
        "evidence_ids": ["ev1"],
    }
    decision = decide_acceptance(record, auto_accept=True)
    assert decision.acceptance_state == "needs_review"


def test_verified_reference_is_accepted() -> None:
    record = {
        "validation_state": "verified_reference",
        "checks": [],
        "evidence_ids": ["ev1"],
    }
    decision = decide_acceptance(record, auto_accept=False)
    assert decision.acceptance_state == "accepted"
    assert decision.verification_method == "verified_reference"


def test_verified_human_is_accepted() -> None:
    record = {"validation_state": "verified_human", "checks": []}
    decision = decide_acceptance(record, auto_accept=False)
    assert decision.acceptance_state == "accepted"


def test_auto_accept_requires_checks_passed() -> None:
    record = {"validation_state": "checks_passed", "checks": []}
    decision = decide_acceptance(record, auto_accept=True)
    assert decision.acceptance_state == "accepted"


def test_unchecked_stays_needs_review_even_with_auto_accept() -> None:
    record = {"validation_state": "unchecked", "checks": []}
    decision = decide_acceptance(record, auto_accept=True)
    assert decision.acceptance_state == "needs_review"