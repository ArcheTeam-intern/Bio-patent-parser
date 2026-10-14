"""Evidence construction at extraction time (plan T06, hardened in B02).

Evidence ids are opaque, content-derived hashes:
``sha256(document_id | parser_run_id | source_locator | page | offset)[:20]``
— nothing ever parses a page number out of an id again, and identical
offsets in the same block id on two different pages do NOT collide.

Precision is split into two honest axes:

- ``text_precision``: ``offset`` (character interval into the block text),
  ``block`` (whole-block text), ``none``.
- ``geometry_precision``: how exactly the AREA is known. A block's bbox
  covers the whole block, so a match inside it reports ``block`` — never
  ``character``. Only a verified per-character mapping may upgrade.

``source_parser`` comes from the adapter metadata (BlockRef), never guessed
from the block-id string.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from bio_pdf.contracts import EvidenceFragment, EvidenceSpan

#: Presentation-only excerpt budget for UI blurbs; the span's raw_text is
#: never truncated.
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
    source_parser: str = "unknown"
    source_artifact_id: str | None = None


def make_evidence_id(
    *,
    document_id: str,
    parser_run_id: str,
    source_locator: str,
    page_number: int | None,
    char_start: int | None,
    char_end: int | None,
) -> str:
    """Stable opaque id; identical (locator, page, offset) → identical id."""

    page_tag = f"p{page_number}" if page_number is not None else "p-"
    start = char_start if char_start is not None else "-"
    end = char_end if char_end is not None else "-"
    payload = f"{document_id}|{parser_run_id}|{source_locator}|{page_tag}|{start}:{end}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"ev-{digest}"


def span_from_block_match(
    block: BlockRef,
    *,
    text_raw: str,
    char_start: int,
    char_end: int,
    full_text: str,
    source_artifact_id: str | None = None,
    alias_block_refs: list[str] | None = None,
) -> EvidenceSpan:
    """Build a text-offset span from a match inside one block.

    ``full_text[char_start:char_end] == text_raw`` is enforced — the span
    claims exactly the characters it was matched on. Geometry stays honest:
    a block bbox yields ``geometry_precision='block'`` ("段落区域+文本偏移"
    in the UI), never a character highlight.

    ``alias_block_refs`` records the *other channel's* blocks that carried
    the same text (plan B02: a fused layout paragraph lists its source
    native lines) — as additional fragments with the same page and matched
    text but no independent bbox.
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
        page_number=block.page_number,
        char_start=char_start,
        char_end=char_end,
    )
    artifact = source_artifact_id or block.source_artifact_id
    fragment = EvidenceFragment(
        page_number=block.page_number,
        bbox=block.bbox,
        block_id=block.block_id,
        table_id=block.table_id,
        cell_id=block.cell_id,
        char_start=char_start,
        char_end=char_end,
        raw_text=text_raw,
        source_artifact_id=artifact,
    )
    fragments = [fragment]
    for alias in alias_block_refs or []:
        fragments.append(
            EvidenceFragment(
                page_number=block.page_number,
                bbox=None,  # the alias line's own box is not recorded here
                block_id=alias,
                char_start=None,
                char_end=None,
                raw_text=text_raw,
                source_artifact_id=artifact,
            )
        )
    geometry = "block" if block.bbox is not None else "none"
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_id=block.document_id,
        page_number=block.page_number,
        bbox=block.bbox,
        char_start=char_start,
        char_end=char_end,
        raw_text=text_raw,
        source_parser=block.source_parser,
        source_locator=block.block_id,
        fragments=fragments,
        location_precision="block" if geometry == "block" else "none",
        text_precision="offset",
        geometry_precision=geometry,
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
    source_artifact_id: str | None = None,
) -> EvidenceSpan:
    """Build a cell-precision span (table values)."""

    evidence_id = make_evidence_id(
        document_id=document_id,
        parser_run_id=parser_run_id,
        source_locator=f"{table_id}/{cell_id}",
        page_number=page_number,
        char_start=None,
        char_end=None,
    )
    fragment = EvidenceFragment(
        page_number=page_number,
        bbox=bbox,
        table_id=table_id,
        cell_id=cell_id,
        raw_text=text_raw or None,
        source_artifact_id=source_artifact_id,
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
        text_precision="block" if text_raw else "none",
        geometry_precision="cell" if bbox is not None else "page",
    )


def span_page_only(
    *,
    document_id: str,
    parser_run_id: str,
    page_number: int,
    text_raw: str | None,
    source_locator: str,
    source_parser: str = "unknown",
) -> EvidenceSpan:
    """Page-anchored span (no bbox): honest 'page, no exact area'."""

    evidence_id = make_evidence_id(
        document_id=document_id,
        parser_run_id=parser_run_id,
        source_locator=source_locator,
        page_number=page_number,
        char_start=None,
        char_end=None,
    )
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_id=document_id,
        page_number=page_number,
        raw_text=text_raw or None,
        source_parser=source_parser,
        source_locator=source_locator,
        fragments=[
            EvidenceFragment(page_number=page_number, raw_text=text_raw or None)
        ],
        location_precision="page",
        text_precision="block" if text_raw else "none",
        geometry_precision="page",
    )


def span_unlocated(
    *,
    document_id: str,
    parser_run_id: str,
    raw_text: str | None,
    source_locator: str,
    source_parser: str = "unknown",
) -> EvidenceSpan:
    """A span with NO page and NO box — never fabricated as page 1.

    Used for mentions whose real location is unknown (plan B02: legacy
    migration must not default to page 1). Validation reports it as
    unknown; the UI shows '缺少位置证据'.
    """

    evidence_id = make_evidence_id(
        document_id=document_id,
        parser_run_id=parser_run_id,
        source_locator=source_locator,
        page_number=None,
        char_start=None,
        char_end=None,
    )
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_id=document_id,
        page_number=None,
        bbox=None,
        raw_text=raw_text or None,
        source_parser=source_parser,
        source_locator=source_locator,
        fragments=[],
        location_precision="none",
        text_precision="block" if raw_text else "none",
        geometry_precision="none",
    )


@dataclass
class EvidenceIndex:
    """Accumulates spans; identical id+payload merges, differing errors."""

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
        return [span.model_dump(mode="json") for span in self.spans.values()]


__all__ = [
    "BlockRef",
    "EvidenceIndex",
    "make_evidence_id",
    "span_from_block_match",
    "span_from_cell",
    "span_page_only",
    "span_unlocated",
    "MAX_EVIDENCE_EXCERPT",
]
