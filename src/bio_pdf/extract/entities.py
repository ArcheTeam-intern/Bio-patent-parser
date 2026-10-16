"""Explicit entity extraction and linking (plan B07 / C04).

Entities come ONLY from explicit surface forms:

- prose patterns: ``peptide A``, ``compound 3``, ``antibody X``, ``variant
  Y`` …;
- patent-style construction numbers (``#134``, ``#136``) as written in the
  ST.25 feature notes and body text;
- variant notation (``W317A``) — an entity, never an automatic rewrite of
  any sequence (coordinate system must be checked by a human);
- table row labels that name the same patterns.

Every entity's evidence is a REAL span registered in the evidence index
(block offsets), not a string that only looks like an id — C04 replaces the
old fabricated ``document:pN:entity:...`` strings that could not be
resolved by the evidence API.

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

#: Explicit entity surface forms (kind noun + identifier).
_PATTERNS = (
    re.compile(
        r"\b(peptide|compound|antibody|variant|construct|protein|strain|isolate|"
        r"mutant)\s+([A-Z][A-Za-z0-9\-]{0,14}|\d{1,4})\b"
    ),
)

#: Patent-style construction numbers: ``#134``, ``#136`` (ST.25 notes and
#: body text use them to name constructs).
_HASH_NUMBER_RE = re.compile(r"#(\d{1,4})\b")

#: Variant notation ``W317A`` — wildtype residue, position, replacement.
_VARIANT_RE = re.compile(r"\b([ACDEFGHIKLMNPQRSTVWY])(\d{1,4})([ACDEFGHIKLMNPQRSTVWY])\b")

#: Named constructs with a residue range, e.g. ``TAT-RasGAP317-326`` /
#: ``TAT-Ras-GAP317−326`` (title hyphen and body minus signs both occur).
#: Requires a capital start AND at least one lowercase letter in the token
#: so all-caps gene names with numbers (IL-17, COVID-19) are not swept in.
_CONSTRUCT_RANGE_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*(?:[-\u2013][A-Za-z0-9]+)*[\u2212\u2013-]\d{1,4}\b"
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


def _register_match_span(
    *,
    evidence_index: Any | None,
    block: dict[str, Any],
    match_text: str,
    char_start: int,
    char_end: int,
    document_id: str,
    parser_run_id: str,
    source_locator: str,
) -> str:
    """Register the match as a REAL evidence span; unlocated as fallback."""

    if evidence_index is None:
        return source_locator
    from bio_pdf.errors import WorkflowError
    from bio_pdf.extract.evidence import BlockRef, span_from_block_match, span_unlocated

    try:
        span = span_from_block_match(
            BlockRef(
                document_id=document_id,
                parser_run_id=parser_run_id,
                block_id=str(block.get("block_id") or "block:0"),
                page_number=int(block.get("page_number", 1)),
                bbox=tuple(block["bbox"]) if block.get("bbox") else None,
                source_parser=str(block.get("source_parser") or "unknown"),
                source_artifact_id=block.get("source_artifact_id"),
                # H07/P04: never drop the frame-trust flag on the way
                # into evidence construction.
                bbox_verified=block.get("bbox_verified"),
            ),
            text_raw=match_text,
            char_start=char_start,
            char_end=char_end,
            full_text=str(block.get("text") or ""),
        )
        evidence_index.add(span)
        return span.evidence_id
    except (WorkflowError, ValueError):
        span = span_unlocated(
            document_id=document_id,
            parser_run_id=parser_run_id,
            raw_text=match_text,
            source_locator=source_locator,
            source_parser=str(block.get("source_parser") or "unknown"),
        )
        evidence_index.add(span)
        return span.evidence_id


def extract_entities(
    blocks: list[dict[str, Any]],
    document_id: str,
    *,
    evidence_index: Any | None = None,
    parser_run_id: str = "native",
) -> list[Entity]:
    """Extract entities from explicit prose/table-label surface forms.

    When ``evidence_index`` is given, every entity's evidence id is a real
    registered span over the matched text (unlocated span as the honest
    fallback) — resolvable through the evidence API.
    """

    by_name: dict[str, Entity] = {}

    def remember(name: str, kind: str, block: dict[str, Any], start: int, end: int) -> None:
        locator = f"entity:{name}:{block.get('block_id', '?')}:{start}"
        evidence_id = _register_match_span(
            evidence_index=evidence_index,
            block=block,
            match_text=str(block.get("text") or "")[start:end],
            char_start=start,
            char_end=end,
            document_id=document_id,
            parser_run_id=parser_run_id,
            source_locator=locator,
        )
        existing = by_name.get(name)
        if existing is not None:
            if evidence_id not in existing.evidence_ids:
                existing.evidence_ids.append(evidence_id)
            return
        by_name[name] = Entity(
            entity_id=_entity_id(document_id, name),
            document_id=document_id,
            name_raw=name,
            kind=kind,  # type: ignore[arg-type]
            evidence_ids=[evidence_id],
        )

    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        for pattern in _PATTERNS:
            for match in pattern.finditer(text):
                kind_word, ident = match.group(1), match.group(2)
                # Single uppercase letters (A/B/C…) are standard entity
                # labels; the stoplist applies case-insensitively otherwise.
                is_single_letter_label = len(ident) == 1 and ident.isupper()
                if not is_single_letter_label and ident.lower() in _STOPWORDS:
                    continue
                name = f"{kind_word.lower()} {ident}"
                remember(
                    name,
                    _KIND_MAP.get(kind_word.lower(), "unknown"),
                    block,
                    match.start(),
                    match.end(),
                )

        for match in _HASH_NUMBER_RE.finditer(text):
            remember(f"#{match.group(1)}", "construct", block, match.start(), match.end())

        for match in _VARIANT_RE.finditer(text):
            # A variant token must not be part of a longer alphanumeric run
            # (lot numbers, accession fragments).
            before = text[match.start() - 1] if match.start() > 0 else " "
            after = text[match.end()] if match.end() < len(text) else " "
            if before.isalnum() or after.isalnum():
                continue
            remember(match.group(0), "variant", block, match.start(), match.end())

        for match in _CONSTRUCT_RANGE_RE.finditer(text):
            token = match.group(0)
            if not any(ch.islower() for ch in token):
                continue  # all-caps name + number (IL-17 …) is not this shape
            remember(token, "construct", block, match.start(), match.end())

    return sorted(by_name.values(), key=lambda e: e.name_raw)


def entities_from_measurement_labels(
    measurements: list[Measurement],
    document_id: str,
    *,
    evidence_index: Any | None = None,
    parser_run_id: str = "native",
) -> list[Entity]:
    """Non-sequence entities from explicit measurement labels (D04).

    Assay tables name their tested objects as row labels (antibiotics such
    as Vancomycin in a susceptibility matrix); those labels are explicit
    surface forms and get real entities — registering them is what removes
    the wall of "no linked entity" review reasons, WITHOUT demanding a
    sequence link: antibiotics have activity results and no sequence by
    design (plan §2.4). Evidence reuses the measurement's own entity_raw
    span when registered, else an honest unlocated span.
    """

    by_name: dict[str, Entity] = {}
    for measurement in measurements:
        entity_raw = (measurement.entity_raw or "").strip()
        if not entity_raw:
            continue
        if entity_raw in by_name:
            continue
        spanned = (measurement.field_evidence or {}).get("entity_raw") or []
        if spanned:
            evidence_ids = [spanned[0]]
        elif evidence_index is not None:
            from bio_pdf.extract.evidence import span_unlocated

            span = span_unlocated(
                document_id=document_id,
                parser_run_id=parser_run_id,
                raw_text=entity_raw,
                source_locator=f"measurement-label:{measurement.measurement_id}",
                source_parser="native",
            )
            evidence_index.add(span)
            evidence_ids = [span.evidence_id]
        else:
            evidence_ids = []
        by_name[entity_raw] = Entity(
            entity_id=_entity_id(document_id, entity_raw),
            document_id=document_id,
            name_raw=entity_raw,
            # Assay-row labels here are drug/compound names; the kind stays
            # "compound" so the UI never implies these are sequences.
            kind="compound",
            evidence_ids=evidence_ids,
        )
    return sorted(by_name.values(), key=lambda e: e.name_raw)


def entities_from_sequence_notes(
    sequences: list[Any],
    blocks: list[dict[str, Any]],
    document_id: str,
    *,
    evidence_index: Any | None = None,
    parser_run_id: str = "native",
) -> tuple[list[Entity], list[dict[str, Any]]]:
    """Entities + note-derived links from ST.25 feature notes (C04/D04).

    Notes like ``Synthetic sequence: #134 and 136 VH CDR 2 amino acid
    sequence`` name constructs (#134 AND #136 — each gets its own evidence
    link, never only the first), a chain (VH/VL) and a CDR number. Role
    separation (D04): the predicate follows WHAT THE TARGET RECORD IS —
    a DNA ''nucleotide sequence'' of a domain is ``encodes`` (coding
    sequence), a protein ''domain amino acid sequence'' is ``has_domain``,
    a CDR is ``has_cdr``, and only an explicitly named full chain gets
    ``heavy_chain``/``light_chain``. DNA is never presented as a chain
    itself.

    Returns ``(entities, note_links)`` where each link is
    ``{entity_name, entity_kind, seq_no, predicate, chain, cdr, note,
    evidence_id}`` — ONE link entry per named subject.
    """

    by_name: dict[str, Entity] = {}
    links: list[dict[str, Any]] = []

    note_index: dict[str, tuple[dict[str, Any], int, int]] = {}
    for block in blocks:
        text = str(block.get("text") or "")
        if text.startswith("<223>"):
            note = text[len("<223>") :].strip()
            if note:
                note_index.setdefault(note, (block, len("<223>"), len(text)))

    for sequence in sequences:
        seq_no = getattr(sequence, "seq_id_no", None)
        if seq_no is None:
            continue
        for note in getattr(sequence, "feature_notes", []) or []:
            note_clean = str(note).strip()
            if not note_clean:
                continue

            construct_names: list[str] = []
            hash_match = _HASH_NUMBER_RE.search(note_clean)
            if hash_match:
                construct_names.append(f"#{hash_match.group(1)}")
                # "…: #134 and 136 VH CDR…" — the bare second number belongs
                # to the same construction list.
                tail = note_clean[hash_match.end() :]
                extra = re.match(r"\s*(?:and|,)\s*#?(\d{1,4})\b", tail)
                if extra:
                    construct_names.append(f"#{extra.group(1)}")

            chain: str | None = None
            if re.search(r"\bVH\b", note_clean):
                chain = "VH"
            elif re.search(r"\bVL\b", note_clean):
                chain = "VL"
            cdr_match = re.search(r"\bCDR\s*(\d)?\b", note_clean)

            # D04 role separation: predicate follows the TARGET record's
            # molecule type and what the note actually names.
            target_type = str(getattr(sequence, "molecule_type", "unknown"))
            is_domain = bool(re.search(r"\bdomain\b", note_clean, re.IGNORECASE))
            is_nucleotide = bool(
                re.search(r"nucleotide|DNA|RNA", note_clean, re.IGNORECASE)
            )
            if target_type in ("DNA", "RNA") or is_nucleotide:
                # A coding sequence ENCODES the protein/domain — it is not
                # itself a heavy/light chain.
                predicate = "encodes"
            elif cdr_match:
                predicate = "has_cdr"
            elif is_domain:
                predicate = "has_domain"
            elif chain == "VH":
                predicate = "heavy_chain"
            elif chain == "VL":
                predicate = "light_chain"
            else:
                predicate = "has_sequence"

            if not construct_names and cdr_match is None and chain is None:
                continue

            located = note_index.get(note_clean)
            if located and evidence_index is not None:
                block, start, end = located
                evidence_id = _register_match_span(
                    evidence_index=evidence_index,
                    block=block,
                    match_text=note_clean,
                    char_start=start,
                    char_end=end,
                    document_id=document_id,
                    parser_run_id=parser_run_id,
                    source_locator=f"st25-note:{seq_no}",
                )
            elif evidence_index is not None:
                from bio_pdf.extract.evidence import span_unlocated

                span = span_unlocated(
                    document_id=document_id,
                    parser_run_id=parser_run_id,
                    raw_text=note_clean,
                    source_locator=f"st25-note:{seq_no}",
                    source_parser="native",
                )
                evidence_index.add(span)
                evidence_id = span.evidence_id
            else:
                evidence_id = f"st25-note:{seq_no}"

            names = construct_names or [f"SEQ ID {seq_no}"]
            for name in names:
                if name not in by_name:
                    by_name[name] = Entity(
                        entity_id=_entity_id(document_id, name),
                        document_id=document_id,
                        name_raw=name,
                        kind="construct" if name.startswith("#") else "unknown",
                        evidence_ids=[evidence_id],
                    )
                elif evidence_id not in by_name[name].evidence_ids:
                    by_name[name].evidence_ids.append(evidence_id)

            if chain:
                # Chain membership on EVERY named construct ("#134 and 136
                # VH CDR") — the chain map is what the relation linker
                # consumes, so dropping #136 here would lose its relation.
                for name in names:
                    by_name[name].chain_map.setdefault(chain, str(seq_no))

            # D04: ONE link entry per named subject — "#134 and 136" both
            # keep their explicit evidence relation.
            for name in names:
                links.append(
                    {
                        "entity_name": name,
                        "entity_kind": by_name[name].kind,
                        "seq_no": str(seq_no),
                        "predicate": predicate,
                        "chain": chain,
                        "cdr": cdr_match.group(1) if cdr_match else None,
                        "note": note_clean,
                        "evidence_id": evidence_id,
                    }
                )

    return sorted(by_name.values(), key=lambda e: e.name_raw), links


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


__all__ = [
    "extract_entities",
    "entities_from_sequence_notes",
    "link_measurements_to_entities",
]
