"""Explicit relation linking.

Given entities (with explicit chain assignments), sequence records and
measurements, this module produces a :class:`RelationResult` with:

- confirmed chain/sequence relations (``heavy_chain``/``light_chain``/…),
- unresolved references for any chain whose sequence is missing (never
  guessed from a sibling antibody),
- review reasons for anything a human must check.

It does NOT build a cartesian product over all heavy/light chains, and it
does NOT merge same-numbered sequences across documents (the ``document_id``
namespaces every sequence id).
"""

from __future__ import annotations

from typing import Any

from bio_pdf.contracts import (
    Entity,
    Measurement,
    RelationResult,
    SequenceRecord,
    SequenceReference,
    SequenceRelation,
)

#: Chain label → relation predicate.
_CHAIN_PREDICATE = {
    "VH": "heavy_chain",
    "heavy": "heavy_chain",
    "VL": "light_chain",
    "light": "light_chain",
    "CDR1": "has_cdr",
    "CDR2": "has_cdr",
    "CDR3": "has_cdr",
}


def _chain_predicate(chain: str) -> str:
    return _CHAIN_PREDICATE.get(chain.upper(), "has_sequence")


def link_explicit_relations(
    entities: list[Entity],
    sequences: list[SequenceRecord],
    measurements: list[Measurement],
    evidence_spans: list[Any],
    *,
    note_links: list[dict[str, Any]] | None = None,
    inventory_status_by_seq: dict[str, str] | None = None,
) -> RelationResult:
    """Link explicit entity/sequence/measurement relations.

    Args:
        entities: Named entities; antibody entities carry ``chain_map``.
        sequences: Sequence records (must have ``seq_id_no`` to be linkable).
        measurements: Measurements to link back to entities.
        evidence_spans: Evidence spans (unused for linking here, but part of
            the stable interface).
        note_links: C04 links derived from ST.25 feature notes
            (:func:`bio_pdf.extract.entities.entities_from_sequence_notes`);
            each carries its note's REAL evidence span.
        inventory_status_by_seq: Inventory status per declared SEQ ID
            (C02) — used to classify a note link whose sequence is not
            recovered as ``missing_sequence`` vs ``outside_scope``.

    Returns:
        A :class:`RelationResult` with confirmed relations, unresolved
        references and review reasons. Every relation carries a
        ``link_status`` from the C04 vocabulary.
    """

    # Key sequences by (document_id, seq_id_no) so a same-numbered sequence
    # from a *different* document can never be linked by accident.
    seq_by_no = {
        (s.document_id, s.seq_id_no): s
        for s in sequences
        if s.seq_id_no is not None
    }
    entity_by_name = {e.name_raw: e for e in entities}
    entity_by_id = {e.entity_id: e for e in entities}

    relations: list[SequenceRelation] = []
    unresolved: list[SequenceReference] = []
    review_reasons: list[str] = []

    # ---- C04: relations derived from ST.25 feature notes -----------------
    for link in note_links or []:
        entity = entity_by_name.get(str(link.get("entity_name")))
        if entity is None:
            continue
        seq_no = str(link.get("seq_no"))
        evidence_id = str(link.get("evidence_id"))
        seq = seq_by_no.get((entity.document_id, seq_no))
        if seq is None:
            status = "missing_sequence"
            if inventory_status_by_seq:
                declared = inventory_status_by_seq.get(seq_no)
                if declared == "outside_scope":
                    status = "outside_scope"
            unresolved.append(
                SequenceReference(
                    document_id=entity.document_id,
                    seq_id_no=seq_no,
                    mention_raw=str(link.get("note") or f"SEQ ID {seq_no}"),
                    evidence_ids=[evidence_id],
                    resolved_sequence_id=None,
                )
            )
            review_reasons.append(
                f"{entity.name_raw} -> SEQ ID {seq_no} ({link.get('predicate')}): "
                f"sequence not recovered ({status})"
            )
            continue
        relations.append(
            SequenceRelation(
                relation_id=(
                    f"{entity.document_id}:rel:note:{entity.entity_id}:"
                    f"{link.get('predicate')}:{seq_no}"
                ),
                document_id=entity.document_id,
                subject_id=entity.entity_id,
                predicate=str(link.get("predicate") or "has_sequence"),  # type: ignore[arg-type]
                object_id=seq.sequence_id,
                # The note's real span — resolvable through the evidence API.
                evidence_ids=[evidence_id],
                validation_state="unchecked",
                link_status="linked",
                # D04: how well the TARGET was recovered, from the C02
                # inventory — "linked" never implies completeness.
                target_status=str(
                    (inventory_status_by_seq or {}).get(seq_no, "unknown")
                ),  # type: ignore[arg-type]
            )
        )

    for entity in entities:
        if entity.kind != "antibody":
            continue
        if not entity.chain_map:
            continue

        for chain, seq_no in entity.chain_map.items():
            predicate = _chain_predicate(chain)
            seq = seq_by_no.get((entity.document_id, seq_no))
            if seq is None:
                # The chain references a sequence we do not have. Record the
                # unresolved reference; never substitute a sibling antibody's
                # sequence.
                unresolved.append(
                    SequenceReference(
                        document_id=entity.document_id,
                        seq_id_no=seq_no,
                        mention_raw=f"{entity.name_raw} {chain} chain (SEQ ID {seq_no})",
                        evidence_ids=[f"{entity.document_id}:unresolved:{entity.entity_id}:{chain}"],
                        resolved_sequence_id=None,
                    )
                )
                review_reasons.append(
                    f"{entity.name_raw} {chain} references missing SEQ ID {seq_no}"
                )
                continue

            relations.append(
                SequenceRelation(
                    relation_id=f"{entity.document_id}:rel:{entity.entity_id}:{predicate}:{seq_no}",
                    document_id=entity.document_id,
                    subject_id=entity.entity_id,
                    predicate=predicate,  # type: ignore[arg-type]
                    object_id=seq.sequence_id,
                    evidence_ids=[f"{entity.document_id}:rel:{entity.entity_id}:{chain}"],
                    validation_state="unchecked",
                    link_status="linked",
                    target_status=str(
                        (inventory_status_by_seq or {}).get(seq_no, "unknown")
                    ),  # type: ignore[arg-type]
                )
            )

    # Link measurements back to entities (assayed_entity) when the entity id
    # is already explicit.
    for measurement in measurements:
        if measurement.entity_id and measurement.entity_id in entity_by_id:
            relations.append(
                SequenceRelation(
                    relation_id=f"{measurement.document_id}:rel:assayed:{measurement.measurement_id}",
                    document_id=measurement.document_id,
                    subject_id=measurement.entity_id,
                    predicate="assayed_entity",
                    object_id=measurement.measurement_id,
                    evidence_ids=list(measurement.evidence_ids),
                    validation_state="unchecked",
                    link_status="linked",
                )
            )

    return RelationResult(
        relations=relations,
        unresolved_references=unresolved,
        review_reasons=review_reasons,
    )


def classify_evidence_kind(
    text: str,
    *,
    has_table_row: bool = False,
    has_method: bool = False,
) -> str:
    """Classify the evidence kind for a measurement mention.

    - ``claim_threshold`` — prose that states a *claim* (``claim … less than
      …``), not a measured table row.
    - ``measured`` — a concrete table row with an assay method present.
    - ``undetermined`` — anything else.

    Returns the string form of :data:`EvidenceKind`.
    """

    lowered = text.lower()
    if "claim" in lowered and any(w in lowered for w in ("less than", "greater than", "at least", "at most")):
        return "claim_threshold"
    if has_table_row and has_method:
        return "measured"
    return "undetermined"


__all__ = ["link_explicit_relations", "classify_evidence_kind"]