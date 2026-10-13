"""Variant relation candidates (e.g. A23G, Δ5-8).

A variant is only *rebuilt* into a concrete sequence when a reference
sequence and numbering scheme are explicit. Otherwise we emit a
``variant_of`` relation candidate so the reviewer can resolve it later —
we never invent a full sequence from a mutation shorthand.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import SequenceRelation

#: Single-residue substitution: A23G, D12E, K100R.
_SUBSTITUTION_RE = re.compile(r"\b([A-Za-z])([0-9]+)([A-Za-z])\b")

#: Deletion shorthand: Δ5-8, del5-8, Δ5..8.
_DELETION_RE = re.compile(r"(?:Δ|del)\s*([0-9]+)\s*[-–—.]+\s*([0-9]+)", re.IGNORECASE)


def _variant_token(text: str) -> str | None:
    """Return the first variant token found in ``text``, if any."""

    if (m := _SUBSTITUTION_RE.search(text)):
        return f"{m.group(1).upper()}{m.group(2)}{m.group(3).upper()}"
    if (m := _DELETION_RE.search(text)):
        return f"Δ{m.group(1)}-{m.group(2)}"
    return None


def extract_variant_relations(
    blocks: list[dict[str, Any]],
    entities: list[dict[str, Any]],
) -> list[SequenceRelation]:
    """Extract variant relation candidates from blocks.

    Args:
        blocks: List of dicts with at least ``text`` and optionally
            ``page_number``.
        entities: List of entity dicts with ``entity_id`` (or
            ``sequence_id``) and optionally ``name_raw``. When a reference
            sequence is present we link ``variant_of`` to it; otherwise we
            keep an ``unresolved_reference`` placeholder object.

    Returns:
        A list of :class:`SequenceRelation` candidates.
    """

    # Build a lookup of reference entity ids by name (best-effort).
    reference_by_name: dict[str, str] = {}
    reference_ids: list[str] = []
    for entity in entities:
        eid = entity.get("entity_id") or entity.get("sequence_id")
        if not eid:
            continue
        reference_ids.append(str(eid))
        name = entity.get("name_raw")
        if name:
            reference_by_name[str(name)] = str(eid)

    relations: list[SequenceRelation] = []
    for block in blocks:
        text = str(block.get("text") or "")
        if not text:
            continue
        token = _variant_token(text)
        if token is None:
            continue

        page_number = int(block.get("page_number", 1))
        document_id = str(block.get("document_id") or "")

        # If a reference is present, link to it; otherwise keep a placeholder
        # so the candidate survives for review.
        object_id = reference_ids[0] if reference_ids else "reference_unknown"

        relations.append(
            SequenceRelation(
                relation_id=f"{document_id}:variant:{token}:p{page_number}",
                document_id=document_id,
                subject_id=token,
                predicate="variant_of",
                object_id=object_id,
                evidence_ids=[f"{document_id}:p{page_number}:variant:{token}"],
                validation_state="unchecked",
            )
        )
    return relations


__all__ = ["extract_variant_relations"]