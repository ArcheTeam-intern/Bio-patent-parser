"""Inline sequence candidates from prose / table blocks (plan T07).

Three entry classes, in order of confidence:

1. **Explicit listing/table fields** — handled by st25.py / table scanning.
2. **Prose with sequence context** — a hint phrase (``amino-acid sequence``,
   ``protein``, ``nucleotide sequence``, ``DNA``/``RNA``, ``CDR``,
   ``residues``, ``SEQ ID NO``) plus a nearby residue run in the SAME
   sentence/parenthesised window. Short peptides (2+ residues) are allowed
   here — no global 15-char threshold.
3. **Context-free long letter runs** — recorded as raw mentions /
   diagnostics only, never promoted to formal candidates.

Alphabet checks are necessary but never sufficient: "Christian Widmann"
fits the protein alphabet and is still a name. Role gating excludes
author/reference/header/footer/caption blocks; table cells that are not
sequence-like (drug names, strains) do not match the residue-run shapes.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import SequenceRecord
from bio_pdf.extract.sequences.amino_acids import ALLOWED_ALPHABETS

#: Context-free runs must be at least this long to even be worth recording
#: as a raw mention (still not a formal candidate).
_MENTION_MIN_RUN = 8

#: Detector A: a parenthesised run right after a hint —
#: "protein (RRRQRRKKRG)", "sequence (acgtacgtac)".
_PAREN_RUN_RE = re.compile(r"\(\s*([A-Za-z0-9×?*]{2,}(?:\s+[A-Za-z0-9×?*]{1,})*)\s*\)")

#: Detector B: an uppercase residue-like token run (>= 4 letters so common
#: abbreviations like HIV/DNA/TAT do not match) anywhere in a hint sentence.
_CAPS_RUN_RE = re.compile(r"\b([A-Z]{4,}(?:\s+[A-Z]{2,})*)\b")

#: Detector C: a lowercase run (>= 6 letters) ONLY in a DNA/RNA hint window.
_LOWER_RUN_RE = re.compile(r"\b([acgtun]{6,})\b", re.IGNORECASE)

#: Words that disqualify a *candidate* if they appear INSIDE the matched run
#: (they make it a word, not residues). Whole-block veto words removed.
_NON_RESIDUE_WORDS = {
    "the", "and", "for", "with", "was", "were", "from", "that", "this",
    "protein", "peptide", "sequence", "residues", "activity", "against",
    "table", "figure", "fig", "et", "al", "doi", "http", "www",
}

#: Common all-caps abbreviations that fit the residue alphabet but are not
#: sequences.
_CAPS_ABBREVIATIONS = {
    "DNA", "RNA", "MRNA", "CDNA", "PCR", "ELISA", "MIC", "MBC", "FBS",
    "PBS", "SDS", "TFA", "HPLC", "NMR", "ATP", "GTP", "CTP", "TTP", "AMP",
    "FDA", "EMA", "WHO", "NIH", "USA", "EU", "N/A",
}

#: STRONG hints: phrases that explicitly name a sequence. A run in the same
#: sentence as a strong hint is a candidate.
_STRONG_PROTEIN_HINTS = (
    "amino acid sequence",
    "amino-acid sequence",
    "protein sequence",
    "peptide sequence",
    "sequence of the protein",
    "sequence of the peptide",
    "sequence residues",
    "residues",
    "cdr",
    "sequence listing",
    "set forth in seq id",
)
_STRONG_DNA_HINTS = ("nucleotide sequence", "dna sequence", "oligonucleotide sequence")
_STRONG_RNA_HINTS = ("rna sequence", "mrna sequence")

#: WEAK hints: bare molecule nouns. They count only for a run that is in
#: parentheses immediately after the noun ("protein (RRRQRRKKRG)") — a bare
#: "protein" anywhere in the sentence produced name false-positives like
#: RASGAP/FIGURE/BMAP (live-run finding).
_WEAK_PROTEIN_HINTS = ("protein", "peptide", "polypeptide", "antibody", "moiety")
_WEAK_DNA_HINTS = ("dna",)
_WEAK_RNA_HINTS = ("rna", "mrna")

#: Blocks whose role excludes them from sequence scanning entirely.
_EXCLUDED_ROLES = {"author", "reference", "header", "footer", "title", "caption"}

#: Sentence splitter for the local context window.
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+")


def _is_excluded_block(block: dict[str, Any]) -> bool:
    role = str(block.get("semantic_role") or "unknown")
    if role in _EXCLUDED_ROLES:
        return True
    kind = str(block.get("kind") or "text").lower()
    return kind in {"title", "picture", "formula"}


def _sentence_window(text: str, start: int, end: int) -> tuple[str, int, int]:
    """Return (sentence_text, abs_start, abs_end) containing the match."""

    boundaries = [0]
    for m in _SENTENCE_SPLIT.finditer(text):
        boundaries.append(m.end())
    boundaries.append(len(text))
    for i in range(len(boundaries) - 1):
        if boundaries[i] <= start < boundaries[i + 1]:
            return text[boundaries[i]:boundaries[i + 1]], boundaries[i], boundaries[i + 1]
    return text, 0, len(text)


def _hint_in_window(window: str, *, strong_only: bool) -> str | None:
    """Detect a sequence hint in the sentence and suggest the molecule type."""

    lowered = window.lower()
    dna = _STRONG_DNA_HINTS
    rna = _STRONG_RNA_HINTS
    protein = _STRONG_PROTEIN_HINTS
    if not strong_only:
        dna = dna + _WEAK_DNA_HINTS
        rna = rna + _WEAK_RNA_HINTS
        protein = protein + _WEAK_PROTEIN_HINTS
    if any(h in lowered for h in dna):
        return "DNA"
    if any(h in lowered for h in rna):
        return "RNA"
    if any(h in lowered for h in protein):
        return "protein"
    return None


def _paren_adjacent_hint(text: str, run_start: int) -> str | None:
    """Weak hint IMMEDIATELY before a parenthesised run ("peptide (XXXX)").

    The hint must end within 30 characters of the opening paren — a bare
    molecule noun elsewhere in the sentence does not qualify.
    """

    window = text[max(0, run_start - 34):run_start].lower()
    if any(h in window for h in _WEAK_DNA_HINTS):
        return "DNA"
    if any(h in window for h in _WEAK_RNA_HINTS):
        return "RNA"
    if any(h in window for h in _WEAK_PROTEIN_HINTS):
        return "protein"
    return None


def _tokenize_run(run: str) -> tuple[str, list[dict[str, str]], bool]:
    """Normalize a residue run keeping unknown positions.

    Returns ``(normalized, residues, has_unknown)``. Digits/×/?/*/- occupy a
    real position and become ``?`` (unresolved); they are never dropped or
    guessed to ``I``/``L``.
    """

    letters: list[str] = []
    residues: list[dict[str, str]] = []
    has_unknown = False
    for ch in run:
        if ch.isspace():
            continue
        upper = ch.upper()
        if ch in "×?*" or ch.isdigit() or ch == "-":
            # Unknown position: occupies one slot as "?"; the original
            # character stays in the residue record (never dropped, never
            # guessed to I/L).
            has_unknown = True
            letters.append("?")
            residues.append(
                {
                    "position": str(len(letters)),
                    "token": ch,
                    "one_letter": "?",
                    "resolved": "false",
                }
            )
            continue
        letters.append(upper)
    return "".join(letters), residues, has_unknown


def _check_alphabet(sequence: str, molecule_type: str) -> bool:
    alphabet = ALLOWED_ALPHABETS.get(molecule_type, frozenset())
    if not alphabet:
        return True  # unknown molecule type: alphabet check not applicable
    return all(c in alphabet or c == "?" for c in sequence)


def _candidate_matches(text: str) -> list[tuple[int, int, str, bool]]:
    """Detect candidate runs: (start, end, raw_run, is_parenthesised)."""

    found: list[tuple[int, int, str, bool]] = []
    seen: set[tuple[int, int]] = set()

    def add(m_start: int, m_end: int, raw: str, paren: bool) -> None:
        if (m_start, m_end) in seen:
            return
        seen.add((m_start, m_end))
        found.append((m_start, m_end, raw, paren))

    for m in _PAREN_RUN_RE.finditer(text):
        add(m.start(1), m.end(1), m.group(1), True)
    for m in _CAPS_RUN_RE.finditer(text):
        token = m.group(1)
        if any(t in _CAPS_ABBREVIATIONS for t in token.split()):
            continue
        add(m.start(1), m.end(1), token, False)
    for m in _LOWER_RUN_RE.finditer(text):
        add(m.start(1), m.end(1), m.group(1), False)
    return sorted(found)


def extract_inline_sequences(
    blocks: list[dict[str, Any]],
    document_id: str,
    *,
    parser_run_id: str = "native",
    evidence_index: Any | None = None,
    diagnostics: list[dict[str, str]] | None = None,
    raw_mentions: list[dict[str, Any]] | None = None,
) -> list[SequenceRecord]:
    """Extract sequence candidates following the T07 three-entry rules.

    Context-free long runs are appended to ``diagnostics``/``raw_mentions``
    (when provided) instead of becoming formal candidates.
    """

    records: list[SequenceRecord] = []
    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        if not text.strip():
            continue
        page_number = int(block.get("page_number", 1))
        block_id = str(block.get("block_id") or f"{document_id}:inline:{idx}")

        if _is_excluded_block(block):
            if _looks_like_long_letter_run(text) and diagnostics is not None:
                diagnostics.append(
                    {
                        "code": "NON_SEQUENCE_ROLE",
                        "block_id": block_id,
                        "page": str(page_number),
                        "detail": f"skipped by role={block.get('semantic_role')}",
                    }
                )
            continue

        for m_start, m_end, run, is_paren in _candidate_matches(text):
            compact = "".join(ch for ch in run if not ch.isspace())
            if len(compact) < 2:
                continue
            # Structural name guard (not a word blacklist): a parenthesised
            # mixed-case multi-letter token is an abbreviation of a name —
            # "protein (RasGAP)", "called TAT-RasGAP317-326" — not a residue
            # listing. Genuine paren runs are all-caps one-letter,
            # all-lowercase nucleic, or three-letter residue tokens.
            if is_paren and not run.isupper() and not run.islower():
                paren_tokens = run.split()
                three_letter_form = all(len(t) <= 3 for t in paren_tokens) and any(
                    len(t) == 3 for t in paren_tokens
                )
                if not three_letter_form:
                    continue
            # Runs dominated by digits/unknowns are notation (IC50, OD600,
            # W317A...), not residues.
            non_letter = sum(1 for ch in compact if not ch.isalpha())
            if non_letter / len(compact) > 0.4:
                continue
            # Prose-shaped runs: function words or several long words are a
            # sentence, not residues ("later identified by sequencing as ...").
            tokens = [t for t in run.split() if t]
            lowered_tokens = {t.lower() for t in tokens}
            if lowered_tokens & _NON_RESIDUE_WORDS:
                continue
            if len(tokens) > 1 and any(len(t) > 12 for t in tokens):
                continue

            window, _w_start, _w_end = _sentence_window(text, m_start, m_end)
            # Strong hints count anywhere in the sentence; weak hints only for
            # a parenthesised run IMMEDIATELY after the hint word. A bare
            # "protein" in the sentence promoted names like "(WT)"/"(IC50)"
            # (live-run finding).
            hint = _hint_in_window(window, strong_only=True)
            if is_paren and hint is None:
                hint = _paren_adjacent_hint(text, m_start)

            # Lowercase runs only qualify under an explicit DNA/RNA hint.
            if run.islower():
                if hint not in ("DNA", "RNA"):
                    if len(compact) >= _MENTION_MIN_RUN:
                        if diagnostics is not None:
                            diagnostics.append(
                                {
                                    "code": "NO_NUCLEIC_CONTEXT",
                                    "block_id": block_id,
                                    "page": str(page_number),
                                    "detail": compact[:80],
                                }
                            )
                        if raw_mentions is not None:
                            raw_mentions.append(
                                _raw_letter_mention(
                                    run=run,
                                    document_id=document_id,
                                    parser_run_id=parser_run_id,
                                    block=block,
                                    block_id=block_id,
                                    page_number=page_number,
                                    m_start=m_start,
                                    m_end=m_end,
                                    text=text,
                                    code="NO_NUCLEIC_CONTEXT",
                                    evidence_index=evidence_index,
                                )
                            )
                    continue
            elif hint is None:
                if len(compact) >= _MENTION_MIN_RUN:
                    if diagnostics is not None:
                        diagnostics.append(
                            {
                                "code": "NO_SEQUENCE_CONTEXT",
                                "block_id": block_id,
                                "page": str(page_number),
                                "detail": compact[:80],
                            }
                        )
                    if raw_mentions is not None:
                        raw_mentions.append(
                            _raw_letter_mention(
                                run=run,
                                document_id=document_id,
                                parser_run_id=parser_run_id,
                                block=block,
                                block_id=block_id,
                                page_number=page_number,
                                m_start=m_start,
                                m_end=m_end,
                                text=text,
                                code="NO_SEQUENCE_CONTEXT",
                                evidence_index=evidence_index,
                            )
                        )
                continue

            molecule_type = hint
            normalized, residues, has_unknown = _tokenize_run(run)
            if not _check_alphabet(normalized, molecule_type):
                continue
            observed = len(normalized)
            if observed < 2:
                continue

            evidence_ids = [f"{document_id}:p{page_number}:inline:{idx}:{m_start}"]
            if evidence_index is not None:
                from bio_pdf.extract.evidence import BlockRef, span_from_block_match

                span = span_from_block_match(
                    BlockRef(
                        document_id=document_id,
                        parser_run_id=parser_run_id,
                        block_id=block_id,
                        page_number=page_number,
                        bbox=tuple(block["bbox"]) if block["bbox"] else None,
                        source_parser=str(block.get("source_parser") or "unknown"),
                        source_artifact_id=block.get("source_artifact_id"),
                    ),
                    alias_block_refs=[
                        a["block_id"]
                        for a in (block.get("source_aliases") or [])
                        if isinstance(a, dict)
                        and a.get("block_id")
                        and (run if "run" in dir() else m.group(0)) in (a.get("text") or "")
                    ],
                    text_raw=run,
                    char_start=m_start,
                    char_end=m_end,
                    full_text=text,
                )
                evidence_index.add(span)
                evidence_ids = [span.evidence_id]

            # D/L and modification annotations stay as notes.
            annotations = []
            if re.search(r"\b[Dd]-[A-Za-z]{3}\b", run):
                annotations.append({"kind": "d_amino_acid", "raw": run})
            records.append(
                SequenceRecord(
                    sequence_id=f"{document_id}:inline:{idx}:{m_start}",
                    document_id=document_id,
                    seq_id_no=None,
                    molecule_type=molecule_type,  # type: ignore[arg-type]
                    representation="unresolved" if has_unknown else "one_letter",
                    source_kind="inline_fragment",
                    sequence_scope="fragment",
                    residue_annotations=annotations,
                    declared_length=None,
                    observed_length=observed,
                    raw_sequence=run.strip(),
                    normalized_sequence=normalized,
                    residues=residues,
                    # A hinted, explicitly displayed run IS an explicit
                    # sequence (of fragment scope); completeness as a full
                    # listing is not claimed.
                    is_complete=False,
                    is_explicit_sequence=True,
                    evidence_ids=evidence_ids,
                    checks=[],
                    validation_state="unchecked",
                    acceptance_state="candidate",
                )
            )
    return records


def _raw_letter_mention(
    *,
    run: str,
    document_id: str,
    parser_run_id: str,
    block: dict[str, Any],
    block_id: str,
    page_number: int,
    m_start: int,
    m_end: int,
    text: str,
    code: str,
    evidence_index: Any | None,
) -> Any:
    """Typed RawMention with real evidence (plan B05 — not a bare dict)."""

    import hashlib

    from bio_pdf.contracts.extraction import RawMention

    evidence_id = f"{document_id}:p{page_number}:inline:{block_id}:{m_start}"
    if evidence_index is not None:
        from bio_pdf.extract.evidence import BlockRef, span_from_block_match

        try:
            span = span_from_block_match(
                BlockRef(
                    document_id=document_id,
                    parser_run_id=parser_run_id,
                    block_id=block_id,
                    page_number=page_number,
                    bbox=tuple(block["bbox"]) if block.get("bbox") else None,
                    source_parser=str(block.get("source_parser") or "unknown"),
                    source_artifact_id=block.get("source_artifact_id"),
                ),
                text_raw=run,
                char_start=m_start,
                char_end=m_end,
                full_text=text,
            )
            evidence_index.add(span)
            evidence_id = span.evidence_id
        except Exception:  # noqa: BLE001 - keep the mention with its locator id
            pass

    digest = hashlib.sha256(f"{document_id}|{evidence_id}".encode("utf-8")).hexdigest()[:16]
    return RawMention(
        mention_id=f"rm-{digest}",
        text=run,
        role="unclassified_letters",
        evidence_ids=[evidence_id],
        reason_codes=[code],
    )


def _looks_like_long_letter_run(text: str) -> bool:
    compact = "".join(ch for ch in text if not ch.isspace())
    return len(compact) >= _MENTION_MIN_RUN and compact.isalpha()


__all__ = ["extract_inline_sequences"]
