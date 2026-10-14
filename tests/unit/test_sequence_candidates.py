"""Inline sequence candidate and variant relation tests."""

from __future__ import annotations

from bio_pdf.extract.sequences.inline import extract_inline_sequences
from bio_pdf.extract.sequences.variants import extract_variant_relations


# ---------------------------------------------------------------------------
# inline sequence candidates
# ---------------------------------------------------------------------------


def test_context_free_long_run_is_not_a_candidate() -> None:
    # Plan T07: without an explicit sequence context a long letter run is a
    # raw mention / diagnostic, never a formal candidate.
    blocks = [
        {
            "text": "MKTVFLIQDKVWAPYTNRG",
            "kind": "text",
            "page_number": 1,
        },
    ]
    diagnostics: list[dict[str, str]] = []
    records = extract_inline_sequences(
        blocks, document_id="sha256:fixture", diagnostics=diagnostics
    )
    assert records == []
    assert any(d["code"] == "NO_SEQUENCE_CONTEXT" for d in diagnostics)


def test_context_hint_promotes_the_same_run() -> None:
    blocks = [
        {
            "text": "The amino acid sequence MKTVFLIQDKVWAPYTNRG was used.",
            "kind": "text",
            "semantic_role": "body",
            "page_number": 1,
        },
    ]
    records = extract_inline_sequences(blocks, document_id="sha256:fixture")
    assert len(records) == 1
    assert records[0].observed_length == 19
    assert records[0].molecule_type == "protein"


def test_title_block_is_not_a_candidate() -> None:
    blocks = [
        {
            "text": "MKTVFLIQDKVWAPYTNRG",
            "kind": "title",
            "page_number": 1,
        },
    ]
    records = extract_inline_sequences(blocks, document_id="sha256:fixture")
    assert records == []


def test_identity_prose_is_not_a_candidate() -> None:
    blocks = [
        {
            "text": "MKTVFLIQDKVWAPYTNRG at least 80% identity",
            "kind": "text",
            "page_number": 1,
        },
    ]
    records = extract_inline_sequences(blocks, document_id="sha256:fixture")
    assert records == []


def test_short_run_is_not_a_candidate() -> None:
    blocks = [
        {
            "text": "MKT",
            "kind": "text",
            "page_number": 1,
        },
    ]
    records = extract_inline_sequences(blocks, document_id="sha256:fixture")
    assert records == []


def test_blank_block_is_skipped() -> None:
    blocks = [
        {"text": "", "kind": "text", "page_number": 1},
    ]
    records = extract_inline_sequences(blocks, document_id="sha256:fixture")
    assert records == []


# ---------------------------------------------------------------------------
# variant relations
# ---------------------------------------------------------------------------


def test_substitution_variant_is_detected() -> None:
    blocks = [
        {"text": "the variant A23G of the reference", "page_number": 1, "document_id": "sha256:fixture"},
    ]
    relations = extract_variant_relations(blocks, entities=[])
    assert len(relations) == 1
    assert relations[0].subject_id == "A23G"
    assert relations[0].predicate == "variant_of"


def test_variant_links_to_reference_when_available() -> None:
    blocks = [
        {"text": "variant A23G", "page_number": 1, "document_id": "sha256:fixture"},
    ]
    entities = [
        {"entity_id": "seq-1", "name_raw": "wildtype"},
    ]
    relations = extract_variant_relations(blocks, entities=entities)
    assert relations[0].object_id == "seq-1"


def test_variant_keeps_placeholder_when_no_reference() -> None:
    blocks = [
        {"text": "variant A23G", "page_number": 1, "document_id": "sha256:fixture"},
    ]
    relations = extract_variant_relations(blocks, entities=[])
    assert relations[0].object_id == "reference_unknown"


def test_deletion_variant_is_detected() -> None:
    blocks = [
        {"text": "the deletion Δ5-8 removes residues", "page_number": 1, "document_id": "sha256:fixture"},
    ]
    relations = extract_variant_relations(blocks, entities=[])
    assert len(relations) == 1
    assert relations[0].subject_id == "Δ5-8"


def test_no_variant_in_plain_prose() -> None:
    blocks = [
        {"text": "no mutation shorthand here", "page_number": 1, "document_id": "sha256:fixture"},
    ]
    relations = extract_variant_relations(blocks, entities=[])
    assert relations == []