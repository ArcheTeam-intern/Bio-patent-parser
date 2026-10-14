"""ST.25 sequence-listing parser.

The parser is a small state machine, not a greedy regex. It walks the listing
line by line:

- ``<210>`` starts a new sequence (closing any in-progress one).
- ``<211>/<212>/<213>/<220>/<223>`` carry metadata.
- ``<400>`` opens the residue block.
- The next ``<210>`` or end-of-input closes the current sequence.

Residue handling is molecule-type aware: protein blocks use three-letter
tokens (mapped via :class:`ResidueMapper`), nucleic-acid blocks are
single-letter. Position numbers (e.g. ``1 5``) are removed, not treated as
residues. Unknown three-letter tokens are preserved as ``unresolved`` instead
of being dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bio_pdf.contracts import (
    AcceptanceState,
    CheckResult,
    MoleculeType,
    SequenceRecord,
    ValidationState,
)
from bio_pdf.extract.sequences.amino_acids import (
    ALLOWED_ALPHABETS,
    AMINO_ACID_THREE_TO_ONE,
    ResidueMapper,
)

#: Tag regexes. ``<400>`` must be matched before ``<4xx>`` fall-through.
_TAG_RE = re.compile(r"^\s*<(\d{3})>\s*(.*)$")

_MOLECULE_TYPE: dict[str, MoleculeType] = {
    "PRT": "protein",
    "DNA": "DNA",
    "RNA": "RNA",
}

_POSITION_NUMBER_RE = re.compile(r"^\d+\s*$")

#: Lines that cannot be residue lines: prose sentences carry sentence
#: punctuation. A ST.25 residue line only ever contains residue tokens and
#: bare position numbers, so any other symbol ends the current residue block.
_NON_RESIDUE_RE = re.compile(r"[.,:;()\[\]<>\"'±%–—\-/\\|=+*?!&#$@^~`{}]")


def _looks_like_residue_line(line: str) -> bool:
    """Heuristic: prose lines terminate the residue block.

    A residue line may contain residue tokens and position numbers only.
    Any sentence punctuation (``.``, ``:``, ``-`` …) or a word longer than
    eight letters marks prose (ST.25 residue tokens are ≤3 letters, DNA
    runs are chunked ≤60 chars).
    """

    if _NON_RESIDUE_RE.search(line):
        return False
    tokens = line.split()
    if any(len(t) > 8 for t in tokens):
        # A single very long all-alphabetic token is a continuous nucleic
        # line (ST.25 chunk length is up to 60); prose has punctuation or
        # mixed words, already caught above.
        if len(tokens) == 1 and tokens[0].isalpha() and len(tokens[0]) <= 60:
            return True
        return False
    return True


@dataclass
class _SequenceBuilder:
    """Mutable accumulation for a single sequence during parsing."""

    seq_id_no: str | None = None
    declared_length: int | None = None
    molecule_type: MoleculeType = "unknown"
    organism_raw: str | None = None
    feature_notes: list[str] = field(default_factory=list)
    residue_lines: list[str] = field(default_factory=list)
    # (page_number, line) pairs: pure continuation pages without tags still
    # record their real page (plan B09/B02 — no reverse-engineered pages).
    residue_entries: list[tuple[int, str]] = field(default_factory=list)
    residue_page: int = 1
    in_residues: bool = False
    raw_sequence: str = ""
    normalized_sequence: str = ""
    residues: list[dict[str, str]] = field(default_factory=list)
    representation: str = "one_letter"


class _St25StateMachine:
    """Line-driven state machine shared by single-page and multi-page entry."""

    def __init__(self, *, document_id: str, default_page: int = 1) -> None:
        self.document_id = document_id
        self.default_page = default_page
        self.records: list[SequenceRecord] = []
        #: Pages each finalized record's residues appeared on (aligned).
        self.record_pages: list[list[int]] = []
        #: Listings that closed with ZERO residues (B09): surfaced as
        #: unresolved issues instead of building an empty contract object.
        self.zero_residue_listings: list[tuple[str | None, int]] = []
        self.current: _SequenceBuilder | None = None
        self.mapper = ResidueMapper()

    def feed_line(self, line: str, *, page_number: int) -> None:
        match = _TAG_RE.match(line)
        if not match:
            # Non-tag line: could be a continuation of the residue block.
            if self.current is not None and self.current.in_residues:
                if not line.strip() or _looks_like_residue_line(line):
                    self.current.residue_lines.append(line)
                    self.current.residue_entries.append((page_number, line))
                else:
                    # Prose after the listing ends the residue block; the
                    # state machine stays open until the next <210> but no
                    # longer swallows body text as residues.
                    self.current.in_residues = False
            return

        tag, rest = match.group(1), match.group(2).strip()

        if tag == "210":
            self._close_current()
            self.current = _SequenceBuilder(seq_id_no=rest or None, residue_page=page_number)
            return

        if self.current is None:
            # Metadata before any <210> is ignored (defensive).
            return

        if tag == "211":
            try:
                self.current.declared_length = int(rest)
            except ValueError:
                self.current.declared_length = None
        elif tag == "212":
            self.current.molecule_type = _MOLECULE_TYPE.get(rest.upper(), "unknown")
        elif tag == "213":
            self.current.organism_raw = rest or None
        elif tag == "220":
            # Feature block; subsequent <221>/<222>/<223> are collected.
            pass
        elif tag == "221":
            pass  # feature key — not modelled in first pass
        elif tag == "222":
            pass  # feature location — not modelled in first pass
        elif tag == "223":
            if rest:
                self.current.feature_notes.append(rest)
        elif tag == "400":
            self.current.in_residues = True
            self.current.residue_page = page_number
            # The <400> line carries the sequence number, not a residue.
            if rest:
                # Ignore the trailing seq id (redundant with <210>).
                pass

    def _close_current(self) -> None:
        if self.current is None:
            return
        self._finalize(self.current)
        self.current = None

    def finish(self) -> None:
        self._close_current()

    # ---- finalization ----------------------------------------------------

    def _finalize(self, builder: _SequenceBuilder) -> None:
        raw = "\n".join(line.rstrip() for line in builder.residue_lines)
        if not raw.strip():
            # A declared listing with no residues: keep an explicit issue
            # (the record contract forbids empty sequences) instead of
            # crashing the whole job on a Zero-length NonEmptyStr.
            self.zero_residue_listings.append((builder.seq_id_no, builder.residue_page))
            return
        if builder.molecule_type in ("DNA", "RNA"):
            normalized, residues = self._normalize_nucleic(raw, builder.molecule_type)
            representation = "one_letter"
        else:
            normalized, residues = self._normalize_protein(raw)
            representation = self._protein_representation(normalized, residues)

        builder.raw_sequence = raw.strip()
        builder.normalized_sequence = normalized
        builder.residues = residues
        builder.representation = representation

        observed_length = len(normalized)
        has_unresolved = any(r.get("resolved") == "false" for r in residues)
        if has_unresolved and representation == "one_letter":
            representation = "unresolved"

        evidence_id = (
            f"{self.document_id}:p{builder.residue_page}:st25:{builder.seq_id_no or 'seq'}"
        )

        # A sequence is complete only when the observed residue count agrees
        # with the declared length *and* every position resolved. Unknown
        # symbols occupy real positions; filtering them out may make a
        # truncated listing look complete by accident.
        is_complete = (
            builder.declared_length is None or observed_length == builder.declared_length
        ) and not has_unresolved

        # The pages this sequence's residues actually appeared on (a pure
        # continuation page without tags still records its real page).
        pages: list[int] = []
        for page, _line in builder.residue_entries:
            if page not in pages:
                pages.append(page)
        if not pages and builder.residue_page:
            pages = [builder.residue_page]

        self.record_pages.append(pages)
        record = SequenceRecord(
            sequence_id=f"{self.document_id}:seq:{builder.seq_id_no or 'unnamed'}",
            document_id=self.document_id,
            seq_id_no=builder.seq_id_no,
            molecule_type=builder.molecule_type,
            representation=representation,  # type: ignore[arg-type]
            declared_length=builder.declared_length,
            observed_length=observed_length,
            raw_sequence=raw.strip(),
            normalized_sequence=normalized,
            residues=residues,
            organism_raw=builder.organism_raw,
            feature_notes=list(builder.feature_notes),
            is_complete=is_complete,
            is_explicit_sequence=True,
            evidence_ids=[evidence_id],
            checks=[],
            validation_state="unchecked",
            acceptance_state="candidate",
        )
        self.records.append(record)

    def _normalize_protein(self, raw: str) -> tuple[str, list[dict[str, str]]]:
        """Normalise a protein block (three-letter or one-letter).

        Three-letter form is detected when tokens are short (≤3 letters) and
        at least one token has the classic three-letter length. Unknown
        three-letter tokens become ``?`` (length preserved) rather than
        being mis-read as single letters.

        The single-letter fallback keeps every non-whitespace symbol: a
        character outside the alphabet (``?``, ``*``, ``-``) occupies a real
        position and is recorded as an unresolved residue, never dropped.
        """

        tokens = raw.split()
        non_number_tokens = [t for t in tokens if not _POSITION_NUMBER_RE.match(t)]

        three_letter_form = (
            len(non_number_tokens) >= 1
            and all(len(t) <= 3 and (t.isalpha() or not t.isalnum()) for t in non_number_tokens)
            and any(len(t) == 3 and t.isalpha() for t in non_number_tokens)
        )

        if three_letter_form:
            out: list[str] = []
            residues: list[dict[str, str]] = []
            for pos, token in enumerate(non_number_tokens, start=1):
                mapped = self.mapper.map_token(token)
                out.append(mapped.one_letter or "?")
                residues.append(
                    {
                        "position": str(pos),
                        "token": token,
                        "one_letter": mapped.one_letter or "?",
                        "resolved": "true" if mapped.resolved else "false",
                    }
                )
            return "".join(out), residues

        # Single-letter run (or a noisy run): strip whitespace and bare
        # position numbers, keep every other symbol in order.
        alphabet = ALLOWED_ALPHABETS.get("protein", frozenset())
        out_chars: list[str] = []
        residues = []
        for ch in "".join(non_number_tokens):
            if ch.isspace():
                continue
            upper = ch.upper()
            out_chars.append(upper)
            if upper not in alphabet:
                residues.append(
                    {
                        "position": str(len(out_chars)),
                        "token": ch,
                        "one_letter": upper,
                        "resolved": "false",
                    }
                )
        return "".join(out_chars), residues

    def _normalize_nucleic(
        self, raw: str, molecule_type: str
    ) -> tuple[str, list[dict[str, str]]]:
        """Normalise a nucleic block keeping unknown symbols in place.

        Position numbers (bare integers, ST.25 style) are removed; every
        other character occupies one position. Characters outside the
        molecule's alphabet are kept verbatim and recorded as unresolved so
        the observed length stays honest.
        """

        alphabet = ALLOWED_ALPHABETS.get(molecule_type, frozenset())
        tokens = [t for t in raw.split() if not _POSITION_NUMBER_RE.match(t)]
        out_chars: list[str] = []
        residues: list[dict[str, str]] = []
        for ch in "".join(tokens):
            if ch.isspace():
                continue
            upper = ch.upper()
            out_chars.append(upper)
            if upper not in alphabet:
                residues.append(
                    {
                        "position": str(len(out_chars)),
                        "token": ch,
                        "one_letter": upper,
                        "resolved": "false",
                    }
                )
        return "".join(out_chars), residues

    def _protein_representation(
        self, normalized: str, residues: list[dict[str, str]]
    ) -> str:
        if residues and any(r.get("resolved") == "false" for r in residues):
            return "unresolved"
        return "three_letter" if residues else "one_letter"


def _build_records(
    lines: list[str],
    *,
    document_id: str,
    page_number: int,
) -> list[SequenceRecord]:
    machine = _St25StateMachine(document_id=document_id, default_page=page_number)
    for line in lines:
        machine.feed_line(line, page_number=page_number)
    machine.finish()
    return machine.records


def parse_st25_text(
    text: str,
    *,
    document_id: str,
    page_number: int = 1,
) -> list[SequenceRecord]:
    """Parse a single page's ST.25 listing text.

    Args:
        text: The raw listing text (multi-line).
        document_id: Canonical ``sha256:`` document id.
        page_number: 1-based source page (default 1).

    Returns:
        A list of :class:`SequenceRecord` (usually one per ``<210>`` block).
    """

    lines = text.splitlines()
    return _build_records(lines, document_id=document_id, page_number=page_number)


def parse_st25_blocks(
    blocks: list[dict[str, Any]],
    document_id: str,
    *,
    parser_run_id: str = "native",
    evidence_index: Any | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> list[SequenceRecord]:
    """Parse ST.25 listing across multiple blocks/pages.

    Args:
        blocks: List of ``{"text": str, "page_number": int}`` dicts in
            reading order. A block with ``text=None`` is skipped.
        document_id: Canonical document id.
        parser_run_id: Parser run id for evidence derivation.
        evidence_index: Optional :class:`~bio_pdf.extract.evidence.EvidenceIndex`;
            when given, each sequence's residue listing registers page-anchored
            evidence per source block it spanned (multi-page fragments).

    Returns:
        Sequence records. A sequence that spans blocks continues to
        accumulate residues until the next ``<210>`` (no duplication).
    """

    machine = _St25StateMachine(document_id=document_id, default_page=1)
    # Per-page raw residue text, captured while feeding (plan B09): each
    # page's evidence carries ITS OWN fragment, not a copied full sequence.
    page_fragments: dict[int, list[str]] = {}
    for entry in blocks:
        text = entry.get("text")
        if not text:
            continue
        page_number = int(entry.get("page_number", 1))
        for line in str(text).splitlines():
            machine.feed_line(line, page_number=page_number)
            if machine.current is not None and machine.current.in_residues:
                if line.strip():
                    page_fragments.setdefault(page_number, []).append(line.rstrip())
    machine.finish()

    if issues is not None:
        for seq_id, page in machine.zero_residue_listings:
            issues.append(
                {
                    "scope": "document",
                    "reason_code": "ZERO_RESIDUE_LISTING",
                    "source_ids": [f"st25:{seq_id or 'unnamed'}"],
                    "detail": f"listing for SEQ ID {seq_id or '?'} closed without residues (page {page})",
                }
            )

    if evidence_index is not None:
        from bio_pdf.extract.evidence import span_page_only, span_unlocated

        for record, pages in zip(machine.records, machine.record_pages):
            new_ids: list[str] = []
            if not pages:
                # No page is known for this record: honest "unlocated",
                # never a fabricated page 1.
                span = span_unlocated(
                    document_id=document_id,
                    parser_run_id=parser_run_id,
                    raw_text=record.raw_sequence[:400] or None,
                    source_locator=f"st25:{record.seq_id_no or 'seq'}",
                    source_parser="native",
                )
                evidence_index.add(span)
                new_ids.append(span.evidence_id)
            for page in pages:
                fragment_text = _join_lines(page_fragments.get(page, []))[:400] or None
                span = span_page_only(
                    document_id=document_id,
                    parser_run_id=parser_run_id,
                    page_number=page,
                    text_raw=fragment_text,
                    source_locator=f"st25:{record.seq_id_no or 'seq'}:p{page}",
                    source_parser="native",
                )
                evidence_index.add(span)
                new_ids.append(span.evidence_id)
            # Assign once: the strict contract forbids an empty intermediate.
            record.evidence_ids = new_ids
    return machine.records


def _join_lines(lines: list[str]) -> str:
    """Join residue lines with a newline character."""

    return chr(10).join(lines)


def _record_page(record: SequenceRecord) -> int:
    if record.evidence_ids and ":p" in record.evidence_ids[0]:
        try:
            return int(record.evidence_ids[0].split(":p")[1].split(":")[0])
        except (IndexError, ValueError):
            return 1
    return 1


__all__ = ["parse_st25_text", "parse_st25_blocks"]