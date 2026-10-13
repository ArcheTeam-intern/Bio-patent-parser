"""Relation linking tests (antibody chain pairing, unresolved refs)."""

from __future__ import annotations

from bio_pdf.contracts import Entity, SequenceRecord
from bio_pdf.extract.relations import link_explicit_relations


def _antibody_pairs() -> tuple[list[Entity], list[SequenceRecord]]:
    entities = [
        Entity(
            entity_id="ab-A",
            document_id="sha256:fixture",
            name_raw="Antibody A",
            kind="antibody",
            chain_map={"VH": "1", "VL": "2"},
        ),
        Entity(
            entity_id="ab-B",
            document_id="sha256:fixture",
            name_raw="Antibody B",
            kind="antibody",
            chain_map={"VH": "3", "VL": "4"},
        ),
    ]
    sequences = [
        SequenceRecord(
            sequence_id="sha256:fixture:seq:1", seq_id_no="1",
            document_id="sha256:fixture", molecule_type="protein",
            representation="one_letter", raw_sequence="QVQLVES",
            normalized_sequence="QVQLVES", evidence_ids=["ev-1"],
        ),
        SequenceRecord(
            sequence_id="sha256:fixture:seq:2", seq_id_no="2",
            document_id="sha256:fixture", molecule_type="protein",
            representation="one_letter", raw_sequence="DIQMTQSP",
            normalized_sequence="DIQMTQSP", evidence_ids=["ev-2"],
        ),
        SequenceRecord(
            sequence_id="sha256:fixture:seq:3", seq_id_no="3",
            document_id="sha256:fixture", molecule_type="protein",
            representation="one_letter", raw_sequence="EVQLVES",
            normalized_sequence="EVQLVES", evidence_ids=["ev-3"],
        ),
        SequenceRecord(
            sequence_id="sha256:fixture:seq:4", seq_id_no="4",
            document_id="sha256:fixture", molecule_type="protein",
            representation="one_letter", raw_sequence="DIVMTQSP",
            normalized_sequence="DIVMTQSP", evidence_ids=["ev-4"],
        ),
    ]
    return entities, sequences


def test_two_antibody_pairs_no_cartesian_product() -> None:
    entities, sequences = _antibody_pairs()
    result = link_explicit_relations(entities, sequences, [], [])

    # 4 chain relations: A-VH(1), A-VL(2), B-VH(3), B-VL(4).
    assert len(result.relations) == 4

    # No cross-pairing: A must not link to VL SEQ 4 or SEQ 3.
    subjects = {r.subject_id for r in result.relations}
    assert subjects == {"ab-A", "ab-B"}

    a_objects = {r.object_id for r in result.relations if r.subject_id == "ab-A"}
    assert a_objects == {"sha256:fixture:seq:1", "sha256:fixture:seq:2"}

    b_objects = {r.object_id for r in result.relations if r.subject_id == "ab-B"}
    assert b_objects == {"sha256:fixture:seq:3", "sha256:fixture:seq:4"}


def test_heavy_light_predicates_are_correct() -> None:
    entities, sequences = _antibody_pairs()
    result = link_explicit_relations(entities, sequences, [], [])

    by_key = {(r.subject_id, r.object_id): r for r in result.relations}
    assert by_key[("ab-A", "sha256:fixture:seq:1")].predicate == "heavy_chain"
    assert by_key[("ab-A", "sha256:fixture:seq:2")].predicate == "light_chain"
    assert by_key[("ab-B", "sha256:fixture:seq:3")].predicate == "heavy_chain"
    assert by_key[("ab-B", "sha256:fixture:seq:4")].predicate == "light_chain"


def test_missing_sequence_produces_unresolved_reference() -> None:
    entities = [
        Entity(
            entity_id="ab-B",
            document_id="sha256:fixture",
            name_raw="Antibody B",
            kind="antibody",
            chain_map={"VH": "3", "VL": "4"},
        ),
    ]
    # Only provide SEQ 3 (VH); SEQ 4 (VL) is missing.
    sequences = [
        SequenceRecord(
            sequence_id="sha256:fixture:seq:3", seq_id_no="3",
            document_id="sha256:fixture", molecule_type="protein",
            representation="one_letter", raw_sequence="EVQLVES",
            normalized_sequence="EVQLVES", evidence_ids=["ev-3"],
        ),
    ]
    result = link_explicit_relations(entities, sequences, [], [])
    assert len(result.relations) == 1  # only the VH link
    assert len(result.unresolved_references) == 1
    assert result.unresolved_references[0].seq_id_no == "4"
    assert result.unresolved_references[0].resolved_sequence_id is None


def test_missing_sequence_never_substituted_from_sibling() -> None:
    entities, sequences = _antibody_pairs()
    # Remove SEQ 3 (B's VH). The linker must NOT substitute A's VH (SEQ 1).
    sequences = [s for s in sequences if s.seq_id_no != "3"]
    result = link_explicit_relations(entities, sequences, [], [])
    b_vh = [r for r in result.relations if r.subject_id == "ab-B" and r.predicate == "heavy_chain"]
    assert b_vh == []
    unresolved = [u for u in result.unresolved_references if u.seq_id_no == "3"]
    assert len(unresolved) == 1


def test_different_documents_do_not_merge_same_seq_no() -> None:
    entities = [
        Entity(
            entity_id="ab-X",
            document_id="sha256:doc1",
            name_raw="Antibody X",
            kind="antibody",
            chain_map={"VH": "1"},
        ),
    ]
    # A sequence with seq_id_no "1" from a *different* document must not be
    # linked — the namespaces differ.
    sequences = [
        SequenceRecord(
            sequence_id="sha256:doc2:seq:1", seq_id_no="1",
            document_id="sha256:doc2", molecule_type="protein",
            representation="one_letter", raw_sequence="AAAA",
            normalized_sequence="AAAA", evidence_ids=["ev-x"],
        ),
    ]
    result = link_explicit_relations(entities, sequences, [], [])
    # The linker only links sequences that live in the same document.
    assert result.relations == []
    assert len(result.unresolved_references) == 1