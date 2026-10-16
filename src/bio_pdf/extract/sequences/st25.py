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
#: Any further tag embedded in the same line's remainder (C03 splitting).
_INLINE_TAG_RE = re.compile(r"<\s*(\d{3})\s*>")

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

#: Nucleic-acid letters for DNA/RNA chunk lines (IUPAC ambiguity included).
_NUCLEIC_CHARS = set("ACGTUNRYSWKMBDHVacgtunryswkmbdhv")


def _looks_like_residue_line(line: str) -> bool:
    """Heuristic: prose lines terminate the residue block.

    A residue line may contain residue tokens, position/count numbers and
    nucleic chunks (up to 60 chars per ST.25) only. Any sentence punctuation
    (``.``, ``:``, ``-`` …) marks prose. Long alphabetic tokens are accepted
    only when every character is a nucleic-acid letter — this admits the
    60-char DNA chunk rows while still rejecting prose words like
    "contains" (``o`` is not a nucleic letter).
    """

    if _NON_RESIDUE_RE.search(line):
        return False
    tokens = line.split()
    if not tokens:
        return False
    # A single long alphabetic token is a continuous residue run (ST.25
    # chunk length is up to 60) — kept from the original rule.
    if len(tokens) == 1 and tokens[0].isalpha() and len(tokens[0]) <= 60:
        return True
    for token in tokens:
        if len(token) <= 8:
            continue
        if len(token) > 60:
            return False
        if token.isalpha() and all(ch in _NUCLEIC_CHARS for ch in token):
            continue
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
    declaration_page: int = 1
    saw_400: bool = False
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
        #: Declaration scan (C02): one entry per ``<210>`` block with what
        #: the listing pass observed — the sequence-inventory source. It is
        #: recorded even when residue recovery failed or never started.
        self.scan_entries: list[dict[str, Any]] = []
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

        # C03: several ST.25 tags may sit on ONE line (real catalogs print
        # "<210> 1 <211> 4 <212> PRT" as a single block). Split the line at
        # every embedded tag so each tag receives only its own value —
        # otherwise the SEQ ID swallows "<211> 4 <212> PRT".
        tag, rest = match.group(1), match.group(2)
        while True:
            embedded = _INLINE_TAG_RE.search(rest)
            if embedded is None:
                self._apply_tag(tag, rest.strip(), page_number)
                break
            self._apply_tag(tag, rest[: embedded.start()].strip(), page_number)
            tag, rest = embedded.group(1), rest[embedded.end():]

    def _apply_tag(self, tag: str, rest: str, page_number: int) -> None:
        if tag == "210":
            self._close_current()
            self.current = _SequenceBuilder(
                seq_id_no=rest or None,
                residue_page=page_number,
                declaration_page=page_number,
            )
            return

        if self.current is None:
            # Metadata before any <210> is ignored (defensive).
            return

        if tag == "211":
            try:
                self.current.declared_length = int(rest)
            except ValueError:
                # "<211> 364 <212> DNA" fragment leakage is prevented by
                # the inline split above; a non-numeric rest is really the
                # declaration being absent.
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
            self.current.saw_400 = True
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
        # C02: every declaration leaves a scan entry, recovered or not —
        # this is the sequence-inventory source of truth.
        scan_entry: dict[str, Any] = {
            "seq_id_no": builder.seq_id_no,
            "declaration_page": builder.declaration_page,
            "declared_length": builder.declared_length,
            "molecule_type": builder.molecule_type,
            "organism_raw": builder.organism_raw,
            "saw_400": builder.saw_400,
            "residue_pages": [],
            "observed_length": None,
            "has_unresolved": False,
            "is_complete": None,
            "record_id": None,
        }
        raw = "\n".join(line.rstrip() for line in builder.residue_lines)
        if not raw.strip():
            # A declared listing with no residues: keep an explicit issue
            # (the record contract forbids empty sequences) instead of
            # crashing the whole job on a Zero-length NonEmptyStr.
            self.zero_residue_listings.append((builder.seq_id_no, builder.residue_page))
            self.scan_entries.append(scan_entry)
            return
        if builder.molecule_type in ("DNA", "RNA"):
            normalized, residues = self._normalize_nucleic(raw, builder.molecule_type)
            representation = "one_letter"
        else:
            normalized, residues = self._normalize_protein(raw)
            representation = self._protein_representation(normalized, residues)

        if not normalized.strip():
            # The region was reached but held nothing normalisable (OCR of
            # a line-number column, stray digits): same honesty rule as an
            # empty region — declared with no recoverable residues, never
            # a record with an empty sequence (the contract forbids it).
            self.zero_residue_listings.append((builder.seq_id_no, builder.residue_page))
            self.scan_entries.append(scan_entry)
            return

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

        scan_entry.update(
            {
                "residue_pages": pages,
                "observed_length": observed_length,
                "has_unresolved": has_unresolved,
                "is_complete": is_complete,
                "record_id": f"{self.document_id}:seq:{builder.seq_id_no or 'unnamed'}",
            }
        )
        self.scan_entries.append(scan_entry)

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

    records, _scan = parse_st25_blocks_with_scan(
        blocks,
        document_id,
        parser_run_id=parser_run_id,
        evidence_index=evidence_index,
        issues=issues,
    )
    return records


#: ST.25 tag lines are content, never running material — real catalogs
#: repeat e.g. "<212> DNA" or "<211> 364" across many pages (several
#: same-length sequences), and filtering those would silently drop the
#: declared length and molecule type of every one of them.
_ST25_TAG_LINE_RE = re.compile(r"^\s*<\s*\d{3}\s*>")


def _repeated_line_pages(blocks: list[dict[str, Any]]) -> dict[str, set[int]]:
    """How many distinct pages each non-tag LINE text appears on.

    Repetition alone only marks a text as a CANDIDATE running material —
    E6: the actual exclusion decision is made per occurrence using that
    occurrence's own geometry/role. A text that legitimately repeats as
    BODY residue content (different SEQ IDs with the same fragment) is
    never dropped.
    """

    pages_by_text: dict[str, set[int]] = {}
    for block in blocks:
        text = str(block.get("text") or "")
        if not text:
            continue
        page = int(block.get("page_number", 1))
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not _ST25_TAG_LINE_RE.match(stripped):
                pages_by_text.setdefault(stripped, set()).add(page)
    return pages_by_text


def _furniture_occurrence(block: dict[str, Any], line_text: str) -> bool:
    """E6: does THIS occurrence sit where running material lives?

    True only with concrete evidence on the block carrying the line:
    a header/footer semantic role, or a bbox inside the top/bottom page
    margins, or a narrow left-margin column (the line-number gutter).
    Blocks without geometry/role evidence are never furniture.
    """

    role = str(block.get("semantic_role") or "").lower()
    if role in ("header", "footer"):
        return True
    bbox = block.get("bbox")
    width = float(block.get("page_width") or 0.0)
    height = float(block.get("page_height") or 0.0)
    if bbox and len(bbox) >= 4 and width > 0 and height > 0:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        if y1 <= 0.08 * height or y0 >= 0.92 * height:
            return True
        if x1 <= 0.12 * width and len(line_text) <= 12:
            return True
    return False


def _running_line_filter(
    blocks: list[dict[str, Any]], *, min_pages: int = 3
):
    """Return a predicate ``skip(block, line)`` — per-occurrence (E6).

    A line is skipped ONLY when (1) its text repeats on >= min_pages
    pages, AND (2) THIS occurrence carries furniture evidence. One header
    occurrence can therefore never blacklist the same text's body uses —
    the D-round's global-string bug.
    """

    pages_by_text = _repeated_line_pages(blocks)

    def skip(block: dict[str, Any], line_text: str) -> bool:
        stripped = line_text.strip()
        if not stripped or _ST25_TAG_LINE_RE.match(stripped):
            return False
        if len(pages_by_text.get(stripped, ())) < min_pages:
            return False
        return _furniture_occurrence(block, stripped)

    return skip
def parse_st25_blocks_with_scan(
    blocks: list[dict[str, Any]],
    document_id: str,
    *,
    parser_run_id: str = "native",
    evidence_index: Any | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> tuple[list[SequenceRecord], list[dict[str, Any]]]:
    """Same as :func:`parse_st25_blocks`, plus the C02 declaration scan.

    Returns ``(records, scan_entries)`` where each scan entry describes one
    ``<210>`` declaration — including the ones that recovered no residues —
    with its page, declared metadata and recovery outcome. That list is the
    input of the sequence inventory (``build_sequence_inventory``).
    """

    machine = _St25StateMachine(document_id=document_id, default_page=1)
    # C03/E6: running headers/footers/line numbers never reach the state
    # machine (they fabricated "EPB1"-style residues) — but the decision is
    # made PER OCCURRENCE from that occurrence's own geometry/role, so a
    # header copy can never blacklist the same text used as body residues.
    is_running_line = _running_line_filter(blocks)
    # Per-page raw residue text, captured while feeding (plan B09): each
    # page's evidence carries ITS OWN fragment, not a copied full sequence.
    page_fragments: dict[int, list[str]] = {}
    for entry in blocks:
        text = entry.get("text")
        if not text:
            continue
        page_number = int(entry.get("page_number", 1))
        for line in str(text).splitlines():
            if is_running_line(entry, line):
                continue
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
    return machine.records, machine.scan_entries


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


__all__ = ["parse_st25_text", "parse_st25_blocks", "parse_st25_blocks_with_scan"]