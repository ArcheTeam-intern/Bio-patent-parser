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


# ---- D04 additions ---------------------------------------------------------


def test_dna_target_is_encoded_not_a_chain() -> None:
    """D04: a DNA nucleotide sequence ENCODES the domain — the acceptor
    review found DNA presented as 重链/轻链."""

    index = EvidenceIndex()
    note = "Synthetic sequence: #134 VH domain nucleotide sequence"
    blocks = [_block(f"<223> {note}", page=32)]
    dna = _sequence("7", notes=[note]).model_copy(
        update={"molecule_type": "DNA", "representation": "one_letter"}
    )
    entities, links = entities_from_sequence_notes(
        dna and [dna], blocks, "sha256:doc", evidence_index=index
    )
    assert links and links[0]["predicate"] == "encodes"

    result = link_explicit_relations(entities, [dna], [], [], note_links=links)
    assert result.relations[0].predicate == "encodes"
    assert result.relations[0].target_status == "unknown"


def test_protein_domain_is_has_domain_and_cdr_is_has_cdr() -> None:
    index = EvidenceIndex()
    domain_note = "Synthetic sequence: #134 VH domain amino acid sequence"
    cdr_note = "Synthetic sequence: #134 and 136 VH CDR 2 amino acid sequence"
    blocks = [_block(f"<223> {domain_note}", page=32), _block(f"<223> {cdr_note}", page=31, block_id="b2")]
    domain_seq = _sequence("8", notes=[domain_note])
    cdr_seq = _sequence("2", notes=[cdr_note])
    entities, links = entities_from_sequence_notes(
        [domain_seq, cdr_seq], blocks, "sha256:doc", evidence_index=index
    )
    by_seq = {link["seq_no"]: link["predicate"] for link in links}
    assert by_seq["8"] == "has_domain"
    assert by_seq["2"] == "has_cdr"


def test_both_named_constructs_get_their_own_links() -> None:
    """D04: '#134 and 136' must keep BOTH subjects (previously only #134)."""

    index = EvidenceIndex()
    note = "Synthetic sequence: #134 and 136 VH CDR 2 amino acid sequence"
    blocks = [_block(f"<223> {note}", page=31)]
    entities, links = entities_from_sequence_notes(
        [_sequence("2", notes=[note])], blocks, "sha256:doc", evidence_index=index
    )
    named = {link["entity_name"] for link in links}
    assert named == {"#134", "#136"}
    # Both subjects carry the chain map (otherwise relation linking drops one).
    for entity in entities:
        assert entity.chain_map.get("VH") == "2"


def test_target_status_reflects_inventory_partial_recovery() -> None:
    """D04: linked 不等于完整 — the relation carries the recovery state."""

    index = EvidenceIndex()
    note = "Synthetic sequence: #134 VH domain nucleotide sequence"
    blocks = [_block(f"<223> {note}", page=32)]
    dna = _sequence("7", notes=[note]).model_copy(
        update={"molecule_type": "DNA", "representation": "one_letter"}
    )
    entities, links = entities_from_sequence_notes(
        [dna], blocks, "sha256:doc", evidence_index=index
    )
    result = link_explicit_relations(
        entities, [dna], [], [], note_links=links,
        inventory_status_by_seq={"7": "partial"},
    )
    assert result.relations[0].target_status == "partial"


def test_measurement_labels_become_non_sequence_entities() -> None:
    """D04: assay-table row labels (antibiotics) are explicit entities —
    they get activity results and no sequence by design."""

    from bio_pdf.contracts import ActivityValue, Measurement
    from bio_pdf.extract.entities import entities_from_measurement_labels

    value = ActivityValue(kind="categorical", qualifier="S", raw="S")
    measurements = [
        Measurement(
            measurement_id="m1",
            document_id="sha256:doc",
            entity_raw="Vancomycin",
            metric="antimicrobial_susceptibility",
            value=value,
            evidence_ids=["ev-1"],
            field_evidence={"entity_raw": ["ev-1"], "value": ["ev-2"]},
        ),
        Measurement(
            measurement_id="m2",
            document_id="sha256:doc",
            entity_raw="Vancomycin",
            metric="antimicrobial_susceptibility",
            value=value,
            evidence_ids=["ev-3"],
            field_evidence={"entity_raw": ["ev-3"], "value": ["ev-4"]},
        ),
    ]
    entities = entities_from_measurement_labels(measurements, "sha256:doc")
    assert len(entities) == 1
    assert entities[0].name_raw == "Vancomycin"
    assert entities[0].kind == "compound"
    assert entities[0].evidence_ids == ["ev-1"]

    updated, reasons = link_measurements_to_entities(measurements, entities)
    assert all(m.entity_id == entities[0].entity_id for m in updated)
    assert reasons == []


def test_construct_with_residue_range_pattern() -> None:
    """D04: TAT-RasGAP317−326 style constructs become traceable entities;
    all-caps gene names with a hyphen-number (IL-17) do not."""

    index = EvidenceIndex()
    blocks = [
        _block(
            "TAT-RasGAP317-326 is a retro-inverso peptide; IL-17 levels were measured"
        )
    ]
    entities = extract_entities(blocks, "sha256:doc", evidence_index=index)
    names = {e.name_raw for e in entities}
    assert "TAT-RasGAP317-326" in names
    assert "IL-17" not in names
    construct = next(e for e in entities if e.name_raw == "TAT-RasGAP317-326")
    assert construct.kind == "construct"
    assert construct.evidence_ids and construct.evidence_ids[0] in index.spans
