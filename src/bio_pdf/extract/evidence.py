"""Evidence construction at extraction time (plan T06).

Evidence ids are opaque, content-derived hashes:
``sha256(document_id | parser_run_id | source_locator | span offset)[:20]``
— nothing ever parses a page number out of an id again.

Spans are produced by the extractors from the *actual match offsets* on
the immutable block text: ``block.text_raw[char_start:char_end]`` is the
matched expression, verified at construction. Multi-line / multi-page
matches become multiple fragments in reading order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from bio_pdf.contracts import EvidenceFragment, EvidenceSpan

#: Matches longer than this keep the full raw_text (T06: no 200-char cut);
#: the UI excerpt is a separate, presentation-only concern.
MAX_EVIDENCE_EXCERPT = 400


@dataclass
class BlockRef:
    """Where a span's characters live (immutable inputs to the id hash)."""

    document_id: str
    parser_run_id: str
    block_id: str
    page_number: int
    bbox: tuple[float, float, float, float] | None = None
    table_id: str | None = None
    cell_id: str | None = None


def make_evidence_id(
    *,
    document_id: str,
    parser_run_id: str,
    source_locator: str,
    char_start: int | None,
    char_end: int | None,
) -> str:
    """Stable opaque id; identical (locator, offset) → identical id."""

    payload = (
        f"{document_id}|{parser_run_id}|{source_locator}|"
        f"{char_start if char_start is not None else '-'}:{char_end if char_end is not None else '-'}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"ev-{digest}"


def span_from_block_match(
    block: BlockRef,
    *,
    text_raw: str,
    char_start: int,
    char_end: int,
    full_text: str,
) -> EvidenceSpan:
    """Build a character-precise span from a match inside one block.

    ``full_text[char_start:char_end] == text_raw`` is enforced — the span
    claims exactly the characters it was matched on.
    """

    actual = full_text[char_start:char_end]
    if actual != text_raw:
        from bio_pdf.errors import WorkflowError

        raise WorkflowError(
            "evidence construction mismatch: match offsets do not reproduce text",
            details={
                "expected": text_raw[:80],
                "actual": actual[:80],
                "char_start": char_start,
                "char_end": char_end,
            },
        )
    evidence_id = make_evidence_id(
        document_id=block.document_id,
        parser_run_id=block.parser_run_id,
        source_locator=block.block_id,
        char_start=char_start,
        char_end=char_end,
    )
    fragment = EvidenceFragment(
        page_number=block.page_number,
        bbox=block.bbox,
        block_id=block.block_id,
        table_id=block.table_id,
        cell_id=block.cell_id,
        char_start=char_start,
        char_end=char_end,
        raw_text=text_raw,
    )
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_id=block.document_id,
        page_number=block.page_number,
        bbox=block.bbox,
        char_start=char_start,
        char_end=char_end,
        raw_text=text_raw,
        source_parser="native" if "native" in block.block_id else "layout",
        source_locator=block.block_id,
        fragments=[fragment],
        location_precision="character" if block.bbox is not None else "block",
    )


def span_from_cell(
    *,
    document_id: str,
    parser_run_id: str,
    page_number: int,
    table_id: str,
    cell_id: str,
    text_raw: str,
    bbox: tuple[float, float, float, float] | None,
) -> EvidenceSpan:
    """Build a cell-precision span (table values)."""

    evidence_id = make_evidence_id(
        document_id=document_id,
        parser_run_id=parser_run_id,
        source_locator=f"{table_id}/{cell_id}",
        char_start=None,
        char_end=None,
    )
    fragment = EvidenceFragment(
        page_number=page_number,
        bbox=bbox,
        table_id=table_id,
        cell_id=cell_id,
        raw_text=text_raw or None,
    )
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_id=document_id,
        page_number=page_number,
        bbox=bbox,
        raw_text=text_raw or None,
        source_parser="layout",
        source_locator=f"{table_id}/{cell_id}",
        fragments=[fragment],
        location_precision="cell" if bbox is not None else "page",
    )


def span_page_only(
    *,
    document_id: str,
    parser_run_id: str,
    page_number: int,
    text_raw: str | None,
    source_locator: str,
) -> EvidenceSpan:
    """Page-anchored span (no bbox): honest 'page, no exact area'."""

    evidence_id = make_evidence_id(
        document_id=document_id,
        parser_run_id=parser_run_id,
        source_locator=source_locator,
        char_start=None,
        char_end=None,
    )
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_id=document_id,
        page_number=page_number,
        raw_text=text_raw or None,
        source_parser="native",
        source_locator=source_locator,
        fragments=[
            EvidenceFragment(page_number=page_number, raw_text=text_raw or None)
        ],
        location_precision="page",
    )


@dataclass
class EvidenceIndex:
    """Accumulates spans; identical id+payload merges, differing paylods error."""

    spans: dict[str, EvidenceSpan] = field(default_factory=dict)

    def add(self, span: EvidenceSpan) -> None:
        existing = self.spans.get(span.evidence_id)
        if existing is None:
            self.spans[span.evidence_id] = span
            return
        if existing.model_dump(mode="json") == span.model_dump(mode="json"):
            return  # identical — merge silently
        from bio_pdf.errors import WorkflowError

        raise WorkflowError(
            f"evidence id collision with different payload: {span.evidence_id}",
            details={"evidence_id": span.evidence_id},
        )

    def to_payload(self) -> list[dict[str, Any]]:
        return [
            span.model_dump(mode="json") for span in self.spans.values()
        ]


__all__ = [
    "BlockRef",
    "EvidenceIndex",
    "make_evidence_id",
    "span_from_block_match",
    "span_from_cell",
    "span_page_only",
    "MAX_EVIDENCE_EXCERPT",
]
