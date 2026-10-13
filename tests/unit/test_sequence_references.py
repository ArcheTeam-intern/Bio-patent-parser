"""SEQ ID reference extraction tests."""

from __future__ import annotations

from bio_pdf.extract.sequences.references import extract_sequence_references


def test_single_reference() -> None:
    refs = extract_sequence_references(
        "the peptide of SEQ ID NO:7 is shown",
        document_id="sha256:fixture",
    )
    assert len(refs) == 1
    assert refs[0].seq_id_no == "7"
    assert refs[0].resolved_sequence_id is None


def test_range_reference_expands() -> None:
    refs = extract_sequence_references(
        "SEQ ID NOs: 7-9 are disclosed",
        document_id="sha256:fixture",
    )
    assert [r.seq_id_no for r in refs] == ["7", "8", "9"]


def test_comma_list_reference() -> None:
    refs = extract_sequence_references(
        "see SEQ ID NO: 7, 9",
        document_id="sha256:fixture",
    )
    assert [r.seq_id_no for r in refs] == ["7", "9"]


def test_multiple_mentions_produce_multiple_references() -> None:
    refs = extract_sequence_references(
        "SEQ ID NO:1 and SEQ ID NO:2",
        document_id="sha256:fixture",
    )
    assert len(refs) == 2


def test_identity_prose_is_not_a_reference() -> None:
    # "at least 80% identity" must not produce a sequence reference.
    refs = extract_sequence_references(
        "has at least 80% identity to the sequence",
        document_id="sha256:fixture",
    )
    assert refs == []


def test_case_insensitive_and_punctuation() -> None:
    refs = extract_sequence_references(
        "SEQ ID No. 7",
        document_id="sha256:fixture",
    )
    assert len(refs) == 1
    assert refs[0].seq_id_no == "7"


def test_no_reference_in_plain_text() -> None:
    refs = extract_sequence_references(
        "the amino acid sequence Met Lys Thr",
        document_id="sha256:fixture",
    )
    assert refs == []