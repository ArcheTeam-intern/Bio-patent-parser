"""Evidence kind classification tests."""

from __future__ import annotations

from bio_pdf.extract.relations import classify_evidence_kind


def test_claim_threshold_for_claim_prose() -> None:
    kind = classify_evidence_kind(
        "antibody of claim 1 with IC50 less than 1 nM",
        has_table_row=False,
        has_method=False,
    )
    assert kind == "claim_threshold"


def test_measured_for_table_row_with_method() -> None:
    kind = classify_evidence_kind(
        "IC50 1.2 nM",
        has_table_row=True,
        has_method=True,
    )
    assert kind == "measured"


def test_undetermined_when_no_method() -> None:
    kind = classify_evidence_kind(
        "IC50 1.2 nM",
        has_table_row=True,
        has_method=False,
    )
    assert kind == "undetermined"


def test_undetermined_for_general_statement() -> None:
    kind = classify_evidence_kind(
        "the peptide showed activity",
        has_table_row=False,
        has_method=False,
    )
    assert kind == "undetermined"