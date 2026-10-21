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
    strip_position_suffix,
)

#: Tag regexes. ``<400>`` must be matched before ``<4xx>`` fall-through.
_TAG_RE = re.compile(r"^\s*<(\d{3})>\s*(.*)$")
#: Any further tag embedded in the same line's remainder (C03 splitting).
_INLINE_TAG_RE = re.compile(r"<\s*(\d{3})\s*>")

#: G06/P06: a MARGIN line-number prefix directly before a tag. Real MinerU
#: output printed page-30 lines as ``50 <400> 1`` — the anchor at column 0
#: missed the tag and the sequence never entered residue state. The prefix
#: is stripped ONLY when a ST.25 tag immediately follows (digits + space +
#: ``<nnn>``); the raw line is preserved in the clean→raw mapping.
_MARGIN_TAG_RE = re.compile(r"^\s*(\d{1,4})[ \t]+(?=<\s*\d{3}\s*>)(.*)$")

_MOLECULE_TYPE: dict[str, MoleculeType] = {
    "PRT": "protein",
    "DNA": "DNA",
    "RNA": "RNA",
}

_POSITION_NUMBER_RE = re.compile(r"^\d+\s*$")

#: A line that DECLARES a sequence (optionally behind a page-margin line
#: number). Used to anchor a sequence's declaration page, which carries no
#: residues of its own and would otherwise have page-level evidence only.
_DECLARATION_LINE_RE = re.compile(r"^\s*(?:\d{1,4}[ \t]+)?<\s*(?:210|211|212|213)\s*>")

#: Lines that cannot be residue lines: prose sentences carry sentence
#: punctuation. A ST.25 residue line only ever contains residue tokens and
#: bare position numbers, so any other symbol ends the current residue block.
_NON_RESIDUE_RE = re.compile(r"[.,:;()\[\]<>\"'±%–—\-/\\|=+*?!&#$@^~`{}]")

#: Nucleic-acid letters for DNA/RNA chunk lines (IUPAC ambiguity included).
_NUCLEIC_CHARS = set("ACGTUNRYSWKMBDHVacgtunryswkmbdhv")


def _union_bbox(
    current: tuple[float, float, float, float] | None, box: Any
) -> tuple[float, float, float, float] | None:
    """Union two page boxes, ignoring anything that is not a 4-number box."""

    if box is None:
        return current
    try:
        x0, y0, x1, y1 = (float(box[i]) for i in range(4))
    except (TypeError, ValueError, IndexError):
        return current
    if current is None:
        return (x0, y0, x1, y1)
    return (
        min(current[0], x0),
        min(current[1], y0),
        max(current[2], x1),
        max(current[3], y1),
    )


def _contributes_residues(line: str) -> bool:
    """Whether a residue-block line carries a residue (not only counters).

    Bare integers are ST.25 position numbers — or the page's own footer —
    and must not extend an evidence region.
    """

    return any(not _POSITION_NUMBER_RE.match(token) for token in line.split())


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
    #: THIS sequence's residue lines per page (evidence excerpts). Tracking
    #: them per sequence — not per page — is what makes "jump to SEQ 2" land
    #: on SEQ 2's own rows instead of the whole page's residue union.
    page_fragments: dict[int, list[str]] = field(default_factory=dict)
    #: THIS sequence's residue REGION per page (union of its line boxes).
    page_bbox: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)
    #: The ``<210>/<211>/<212>`` lines that declare this sequence, and their
    #: box — the ONLY honest anchor on a declaration page that holds no
    #: residues of its own (live page 29).
    declaration_text: list[str] = field(default_factory=list)
    declaration_bbox: tuple[float, float, float, float] | None = None
    #: Whether a ``<210>`` line was already seen (its block is the anchor).
    declaration_has_210: bool = False


class _St25StateMachine:
    """Line-driven state machine shared by single-page and multi-page entry."""

    def __init__(self, *, document_id: str, default_page: int = 1) -> None:
        self.document_id = document_id
        self.default_page = default_page
        self.records: list[SequenceRecord] = []
        #: Pages each finalized record's residues appeared on (aligned).
        self.record_pages: list[list[int]] = []
        #: The declaration page of each finalized record (aligned with
        #: ``record_pages``): the page that carries ``<210>``, which is not
        #: necessarily a residue page.
        self.record_declaration_pages: list[int] = []
        #: Per-record evidence material (aligned with ``record_pages``).
        self.record_evidence: list[dict[str, Any]] = []
        #: Listings that closed with ZERO residues (B09): surfaced as
        #: unresolved issues instead of building an empty contract object.
        self.zero_residue_listings: list[tuple[str | None, int]] = []
        #: Declaration scan (C02): one entry per ``<210>`` block with what
        #: the listing pass observed — the sequence-inventory source. It is
        #: recorded even when residue recovery failed or never started.
        self.scan_entries: list[dict[str, Any]] = []
        self.current: _SequenceBuilder | None = None
        self.mapper = ResidueMapper()
        #: G06/P06 clean→raw mapping: every margin-stripped line records
        #: what was removed, so evidence/diagnostics can cite the ORIGINAL
        #: line while the lexer works on the cleaned one.
        self.lex_mappings: list[dict[str, Any]] = []
        #: Box of the block currently being fed (set by :meth:`feed_line`),
        #: so a residue row recovered from a ``<400>`` line keeps its region.
        self.current_line_bbox: Any = None

    def feed_line(
        self, line: str, *, page_number: int, bbox: Any = None
    ) -> None:
        """Consume one listing line.

        ``bbox`` is the box of the BLOCK the line came from: it is what makes
        the evidence anchor at the listing region instead of the page. It is
        optional — a caller without geometry (tests, plain text) still gets a
        correct parse, just page-level evidence.
        """

        cleaned_line = line
        self.current_line_bbox = bbox
        margin = _MARGIN_TAG_RE.match(line)
        if margin is not None:
            cleaned_line = margin.group(2)
            self.lex_mappings.append(
                {
                    "page_number": page_number,
                    "raw": line,
                    "clean": cleaned_line,
                    "stripped_margin": margin.group(1),
                }
            )
        match = _TAG_RE.match(cleaned_line)
        if not match:
            # Non-tag line: could be a continuation of the residue block.
            if self.current is not None and self.current.in_residues:
                if not line.strip() or _looks_like_residue_line(line):
                    self._note_residue(line, page_number, bbox)
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

        # K12: remember the DECLARATION lines of the sequence they open — a
        # declaration page (live page 29) carries no residues, so the <210>
        # block is the only region a click can land on.
        builder = self.current
        if builder is not None and _DECLARATION_LINE_RE.match(cleaned_line):
            stripped = cleaned_line.strip()
            if stripped not in builder.declaration_text:
                builder.declaration_text.append(stripped)
            # Anchor on the <210> block ONLY: the rest of a declaration can
            # sit in another block far away (live page 29 carries <210>/<211>/
            # <212> at the page bottom and <213> at the top), and unioning
            # them would highlight the whole page instead of the declaration.
            if "210" in _INLINE_TAG_RE.findall(cleaned_line):
                builder.declaration_bbox = _union_bbox(
                    builder.declaration_bbox if builder.declaration_has_210 else None,
                    bbox,
                )
                builder.declaration_has_210 = True
            elif not builder.declaration_has_210:
                builder.declaration_bbox = _union_bbox(builder.declaration_bbox, bbox)

    def _note_residue(self, line: str, page_number: int, bbox: Any) -> None:
        """Record a residue line on the CURRENT sequence, with its region.

        Only a line that actually carries a residue token contributes to the
        evidence region: bare position counters and the footer page number
        are residue-block CONTINUATION evidence but not an anchor — letting
        them widen the box dragged the page-31 highlight down to the footer.
        """

        builder = self.current
        if builder is None:
            return
        builder.residue_lines.append(line)
        builder.residue_entries.append((page_number, line))
        if not _contributes_residues(line):
            return
        builder.page_fragments.setdefault(page_number, []).append(line.rstrip())
        merged = _union_bbox(builder.page_bbox.get(page_number), bbox)
        if merged is not None:
            builder.page_bbox[page_number] = merged

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
            # The <400> line USUALLY carries only the sequence number, but
            # real listings print the first residue row on it too
            # ("<400> 2 catatgagac tagtagctca tcctgttcct gatgc 35").
            # Ignoring that rest silently lost every residue of the sequence
            # — live page-32 SEQ 2/3 came out as zero-residue "missing"
            # listings while their residues sat on the <400> line itself.
            inline = self._residues_after_400(rest)
            if inline:
                self._note_residue(inline, page_number, self.current_line_bbox)

    def _residues_after_400(self, rest: str) -> str:
        """The residue row printed ON a ``<400>`` line (after its seq number).

        The leading bare integer is the listing's own sequence number and is
        stripped ONLY when it is a standalone number equal to the declared
        SEQ ID: a residue token is never a bare number, so ``<400> Met Lys``
        keeps every residue.
        """

        tokens = rest.split()
        if not tokens:
            return ""
        if _POSITION_NUMBER_RE.match(tokens[0]):
            declared = (self.current.seq_id_no or "").strip() if self.current else ""
            if not declared or tokens[0] == declared:
                tokens = tokens[1:]
        candidate = " ".join(tokens).strip()
        if not candidate:
            return ""
        # A prose sentence that happens to follow <400> must not open a
        # residue block; no residue row carries sentence punctuation.
        return candidate if _looks_like_residue_line(candidate) else ""

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
        # The DECLARATION page belongs to the record too: "which page does
        # SEQ 1 live on" is answered wrongly when a declaration on page 29
        # and its residues on 30-32 only list 30-32 (the reviewer saw the
        # sequence missing from its own declaration page).
        if builder.declaration_page and builder.declaration_page not in pages:
            pages = [builder.declaration_page, *pages]

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
        self.record_declaration_pages.append(builder.declaration_page)
        # Material for the evidence pass, keyed by page so a region never has
        # to be re-derived (and never widened by another sequence's lines).
        self.record_evidence.append(
            {
                "declaration_text": _join_lines(builder.declaration_text) or None,
                "declaration_bbox": builder.declaration_bbox,
                "fragments": {
                    page: list(lines) for page, lines in builder.page_fragments.items()
                },
                "boxes": dict(builder.page_bbox),
            }
        )
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
        # A residue TABLE glues the position onto the code (``Glu180``): the
        # FORM must be decided on the letters, or the table's rows fall
        # through to the single-letter path and become gibberish.
        base_tokens = [strip_position_suffix(t) for t in non_number_tokens]

        three_letter_form = (
            len(base_tokens) >= 1
            and all(len(t) <= 3 and (t.isalpha() or not t.isalnum()) for t in base_tokens)
            and any(len(t) == 3 and t.isalpha() for t in base_tokens)
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

    for entry in blocks:
        text = entry.get("text")
        if not text:
            continue
        page_number = int(entry.get("page_number", 1))
        bbox = entry.get("bbox")
        for line in str(text).splitlines():
            if is_running_line(entry, line):
                continue
            machine.feed_line(line, page_number=page_number, bbox=bbox)
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

        for record, pages, declaration_page, material in zip(
            machine.records,
            machine.record_pages,
            machine.record_declaration_pages,
            machine.record_evidence,
        ):
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
                # THIS sequence's own rows on this page (never another
                # sequence's), and on a declaration-only page the <210> block
                # — the reviewer's first jump then lands on the sequence's own
                # declaration instead of an un-highlighted page.
                fragment_text = _join_lines(material["fragments"].get(page, []))[:400]
                bbox = material["boxes"].get(page)
                if page == declaration_page:
                    fragment_text = fragment_text or material["declaration_text"]
                    bbox = bbox if bbox is not None else material["declaration_bbox"]
                span = span_page_only(
                    document_id=document_id,
                    parser_run_id=parser_run_id,
                    page_number=page,
                    text_raw=fragment_text or None,
                    source_locator=f"st25:{record.seq_id_no or 'seq'}:p{page}",
                    source_parser="native",
                    bbox=bbox,
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