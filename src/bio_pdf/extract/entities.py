"""Explicit entity extraction and linking (plan B07).

Entities come ONLY from explicit surface forms:

- prose patterns: ``peptide A``, ``compound 3``, ``antibody X``,
  ``variant Y``, ``strain Z`` …;
- table row labels that name the same patterns.

An Entity keeps the original name (``name_raw``) and a document-namespaced
stable id derived from the name — never from position or guesswork.
Linking is exact-name matching (case-insensitive): a measurement whose
``entity_raw`` has no matching entity keeps ``entity_id=None`` and gets a
review reason; the resolver never picks a "closest" entity.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from bio_pdf.contracts import Entity, Measurement

#: Explicit entity surface forms.
_PATTERNS = (
    re.compile(
        r"\b(peptide|compound|antibody|variant|construct|protein|strain|isolate|"
        r"mutant)\s+([A-Z][A-Za-z0-9\-]{0,14}|\d{1,4})\b"
    ),
)

#: Capitalised words that follow a kind noun at a sentence/line boundary
#: (line-fused text) — they are not identifiers.
_STOPWORDS = {
    "all", "the", "this", "these", "those", "we", "it", "a", "an", "both",
    "however", "furthermore", "moreover", "therefore", "thus", "when",
    "while", "after", "before", "in", "on", "for", "and", "or", "but",
    "figure", "table", "supplementary", "data", "was", "were", "is", "are",
}

_KIND_MAP = {
    "peptide": "peptide",
    "compound": "compound",
    "antibody": "antibody",
    "variant": "variant",
    "construct": "variant",
    "protein": "protein",
    "strain": "unknown",
    "isolate": "unknown",
    "mutant": "variant",
}


def _entity_id(document_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{document_id}|{name.lower()}".encode("utf-8")).hexdigest()[:16]
    return f"ent:{digest}"


def extract_entities(blocks: list[dict[str, Any]], document_id: str) -> list[Entity]:
    """Extract entities from explicit prose/table-label surface forms."""

    by_name: dict[str, Entity] = {}
    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        page = int(block.get("page_number", 1))
        block_id = str(block.get("block_id") or f"block:{idx}")
        for pattern in _PATTERNS:
            for match in pattern.finditer(text):
                kind_word, ident = match.group(1), match.group(2)
                # Single uppercase letters (A/B/C…) are standard entity
                # labels; the stoplist applies case-insensitively otherwise.
                is_single_letter_label = len(ident) == 1 and ident.isupper()
                if not is_single_letter_label and ident.lower() in _STOPWORDS:
                    continue
                name = f"{kind_word.lower()} {ident}"
                evidence_id = f"{document_id}:p{page}:entity:{block_id}:{match.start()}"
                existing = by_name.get(name)
                if existing is not None:
                    if evidence_id not in existing.evidence_ids:
                        existing.evidence_ids.append(evidence_id)
                    continue
                by_name[name] = Entity(
                    entity_id=_entity_id(document_id, name),
                    document_id=document_id,
                    name_raw=name,
                    kind=_KIND_MAP.get(kind_word.lower(), "unknown"),  # type: ignore[arg-type]
                    evidence_ids=[evidence_id],
                )
    return sorted(by_name.values(), key=lambda e: e.name_raw)


def link_measurements_to_entities(
    measurements: list[Measurement], entities: list[Entity]
) -> tuple[list[Measurement], list[str]]:
    """Set entity_id where ``entity_raw`` matches an entity exactly.

    Returns ``(updated_measurements, review_reasons)``. Non-matching names
    stay unlinked with a reason — no closest-match guessing.
    """

    by_name = {e.name_raw.strip().lower(): e for e in entities}
    reasons: list[str] = []
    updated: list[Measurement] = []
    for measurement in measurements:
        entity_raw = (measurement.entity_raw or "").strip()
        if not entity_raw:
            updated.append(measurement)
            continue
        entity = by_name.get(entity_raw.lower())
        if entity is None:
            reasons.append(
                f"measurement {measurement.measurement_id}: no explicit entity matches {entity_raw!r}"
            )
            updated.append(measurement)
            continue
        updated.append(
            measurement.model_copy(
                update={
                    "entity_id": entity.entity_id,
                    "evidence_ids": list(
                        dict.fromkeys(
                            list(measurement.evidence_ids) + list(entity.evidence_ids[:1])
                        )
                    ),
                }
            )
        )
    return updated, reasons


__all__ = ["extract_entities", "link_measurements_to_entities"]
