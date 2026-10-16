"""C04 entity/relation tests: real evidence, constructs, variants, notes."""

from __future__ import annotations

from bio_pdf.contracts import Entity, SequenceRecord
from bio_pdf.extract.entities import (
    entities_from_sequence_notes,
    extract_entities,
    link_measurements_to_entities,
)
from bio_pdf.extract.evidence import EvidenceIndex
from bio_pdf.extract.relations import link_explicit_relations


def _block(text: str, page: int = 1, block_id: str = "b1") -> dict:
    return {
        "text": text,
        "page_number": page,
        "block_id": block_id,
        "bbox": [10.0, 10.0, 500.0, 40.0],
        "source_parser": "native",
        "source_artifact_id": None,
    }


def _sequence(seq_no: str, *, notes: list[str] | None = None) -> SequenceRecord:
    return SequenceRecord(
        sequence_id=f"sha256:doc:seq:{seq_no}",
        document_id="sha256:doc",
        seq_id_no=seq_no,
        molecule_type="protein",
        representation="one_letter",
        declared_length=9,
        observed_length=9,
        raw_sequence="Ala Gly Leu Lys Ala Gly Leu Lys",
        normalized_sequence="AGLKAGLKK",
        feature_notes=notes or [],
        evidence_ids=[f"sha256:doc:p1:st25:{seq_no}"],
    )


def test_hash_number_entity_gets_real_evidence_span() -> None:
    index = EvidenceIndex()
    blocks = [_block("Synthetic sequence: #134 and 136 VH CDR 2")]
    entities = extract_entities(blocks, "sha256:doc", evidence_index=index)
    names = {e.name_raw for e in entities}
    assert "#134" in names
    hash_entity = next(e for e in entities if e.name_raw == "#134")
    assert hash_entity.evidence_ids
    # The evidence id resolves to a REGISTERED span (not a lookalike string).
    assert all(eid in index.spans for eid in hash_entity.evidence_ids)


def test_variant_entity_and_alnum_guard() -> None:
    index = EvidenceIndex()
    blocks = [_block("The W317A variant shows reduced binding. lotA12B99")]
    entities = extract_entities(blocks, "sha256:doc", evidence_index=index)
    names = {e.name_raw for e in entities}
    assert "W317A" in names
    assert "A12B" not in names  # embedded in an alphanumeric run


def test_note_entities_and_links_with_real_span() -> None:
    index = EvidenceIndex()
    note = "Synthetic sequence: #134 and 136 VH CDR 2 amino acid sequence"
    blocks = [_block(f"<223> {note}", page=31), _block("<210> 7", page=31, block_id="b2")]
    sequences = [_sequence("7", notes=[note])]
    entities, links = entities_from_sequence_notes(
        sequences, blocks, "sha256:doc", evidence_index=index
    )
    names = {e.name_raw for e in entities}
    assert {"#134", "#136"} <= names
    assert links and links[0]["predicate"] == "has_cdr"
    assert links[0]["chain"] == "VH"
    assert links[0]["evidence_id"] in index.spans

    construct = next(e for e in entities if e.name_raw == "#134")
    assert construct.chain_map.get("VH") == "7"

    result = link_explicit_relations(
        entities, sequences, [], [], note_links=links
    )
    assert result.relations
    relation = result.relations[0]
    assert relation.link_status == "linked"
    assert relation.predicate == "has_cdr"
    assert relation.evidence_ids[0] in index.spans


def test_note_link_missing_sequence_is_reported_not_guessed() -> None:
    index = EvidenceIndex()
    note = "Synthetic sequence: #134 VH CDR 3 amino acid sequence"
    blocks = [_block(f"<223> {note}", page=31)]
    sequences = [_sequence("7", notes=[note])]
    entities, links = entities_from_sequence_notes(
        sequences, blocks, "sha256:doc", evidence_index=index
    )
    # No record for SEQ ID 7 in the sequence list -> unresolved, no guessing.
    result = link_explicit_relations(entities, [], [], [], note_links=links)
    assert not result.relations
    assert result.unresolved_references
    assert any("not recovered" in r for r in result.review_reasons)

    # With the inventory saying the declaration is outside scope, the reason
    # says so instead of pretending it is missing.
    result2 = link_explicit_relations(
        entities,
        [],
        [],
        [],
        note_links=links,
        inventory_status_by_seq={"7": "outside_scope"},
    )
    assert any("outside_scope" in r for r in result2.review_reasons)


def test_measurement_link_still_exact_name_only() -> None:
    from bio_pdf.contracts import ActivityValue, Measurement

    entity = Entity(
        entity_id="ent:1", document_id="sha256:doc", name_raw="peptide A", kind="peptide"
    )
    value = ActivityValue(kind="scalar", number="3.5", raw="3.5")
    hit = Measurement(
        measurement_id="m1",
        document_id="sha256:doc",
        entity_raw="Peptide A",
        metric="ic50",
        value=value,
        unit_raw="nM",
        evidence_ids=["ev-1"],
    )
    miss = Measurement(
        measurement_id="m2",
        document_id="sha256:doc",
        entity_raw="peptide B",
        metric="ic50",
        value=value,
        unit_raw="nM",
        evidence_ids=["ev-2"],
    )
    updated, reasons = link_measurements_to_entities([hit, miss], [entity])
    assert updated[0].entity_id == "ent:1"
    assert updated[1].entity_id is None
    assert any("peptide B" in r for r in reasons)
