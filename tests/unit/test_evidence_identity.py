"""B02: source identity, evidence precision split and fusion dedup.

Counterexamples from the post-M1 review; each must FAIL before the fix:

- native block ids repeat across pages (native:rN),
- evidence ids omit the page, so identical offsets on two pages collide,
- one layout paragraph matching one native line lets the remaining
  overlapping native lines leak into extraction as duplicates,
- character precision was claimed from a paragraph-sized bbox,
- fake page-1 evidence was created for refs with no real location.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bio_pdf.contracts import (
    NormalizedDocument,
    NormalizedPage,
    TextBlock,
)
from bio_pdf.contracts.common import EvidenceSpan
from bio_pdf.extract.evidence import (
    BlockRef,
    EvidenceIndex,
    make_evidence_id,
    span_from_block_match,
)
from bio_pdf.parsers.fusion import fuse_documents


def _doc(pages: list[NormalizedPage]) -> NormalizedDocument:
    return NormalizedDocument(
        schema_version="1.1.0",
        document_id="sha256:x",
        parser_run_id="r1",
        pages=pages,
    )


def _page(number: int, blocks: list[TextBlock]) -> NormalizedPage:
    return NormalizedPage(page_number=number, width_pt=612.0, height_pt=792.0, blocks=blocks)


# ---- 1. native block ids must not repeat across pages ---------------------

def test_native_block_ids_are_page_scoped() -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    import tempfile
    from bio_pdf.pdf.native import read_native_pages

    tmp = Path(tempfile.mkdtemp())
    pdf = tmp / "two.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    for page_text in ("first page text", "second page text"):
        c.drawString(60, 700, page_text)
        c.showPage()
    c.save()

    doc = read_native_pages(pdf, (1, 2), parser_run_id="run1")
    ids = [b.block_id for p in doc.pages for b in p.blocks]
    assert len(ids) == len(set(ids)), f"duplicate block ids across pages: {ids}"


# ---- 2. evidence ids must include the page (no cross-page collision) ------

def test_evidence_id_differs_across_pages() -> None:
    a = make_evidence_id(
        document_id="sha256:x",
        parser_run_id="r1",
        source_locator="block-1",
        page_number=2,
        char_start=10,
        char_end=20,
    )
    b = make_evidence_id(
        document_id="sha256:x",
        parser_run_id="r1",
        source_locator="block-1",
        page_number=3,
        char_start=10,
        char_end=20,
    )
    assert a != b  # identical offsets on different pages must not collide


# ---- 3. precision split: a block bbox is never 'character' ----------------

def test_span_from_block_match_reports_block_geometry_not_character() -> None:
    span = span_from_block_match(
        BlockRef(
            document_id="sha256:x",
            parser_run_id="r1",
            block_id="layout:block-7",
            page_number=9,
            bbox=(305.0, 424.0, 550.0, 571.0),  # 695-char paragraph box
            source_parser="layout",
            source_artifact_id="jobs/job-1/stage_runs/run-1/docling.json",
        ),
        text_raw="RRRQRRKKRG",
        char_start=216,
        char_end=226,
        full_text="x" * 216 + "RRRQRRKKRG" + "y" * 500,
    )
    assert span.text_precision == "offset"
    assert span.geometry_precision == "block"  # NOT character
    # The fragment carries the loadable source artifact id for auditing.
    assert span.fragments[0].source_artifact_id is not None


def test_span_precision_never_claims_character_without_char_bbox() -> None:
    span = span_from_block_match(
        BlockRef(
            document_id="sha256:x",
            parser_run_id="r1",
            block_id="b",
            page_number=1,
            bbox=None,
            source_parser="native",
        ),
        text_raw="AGLK",
        char_start=0,
        char_end=4,
        full_text="AGLK",
        source_artifact_id="artifacts/native.json",
    )
    assert span.geometry_precision == "none"
    assert span.text_precision == "offset"


# ---- 4. fusion: one paragraph consumes an ORDERED SET of native lines -----

def _layout_block(block_id: str, bbox: tuple[float, float, float, float], text: str) -> TextBlock:
    return TextBlock(block_id=block_id, bbox=bbox, text_raw=text)


def test_fusion_consumes_all_overlapping_native_lines() -> None:
    """One 3-line paragraph vs three native line blocks: nothing duplicates."""

    layout = _doc(
        [
            _page(
                9,
                [
                    _layout_block(
                        "layout:p9",
                        (50.0, 100.0, 550.0, 130.0),
                        "we used the HIV TAT protein (RRRQRRKKRG) in this study.",
                    )
                ],
            )
        ]
    )
    native = _doc(
        [
            _page(
                9,
                [
                    _layout_block("native:p9:r1", (50.0, 100.0, 550.0, 109.0), "we used the HIV TAT"),
                    _layout_block("native:p9:r2", (50.0, 110.0, 550.0, 119.0), "protein (RRRQRRKKRG)"),
                    _layout_block("native:p9:r3", (50.0, 120.0, 550.0, 130.0), "in this study."),
                ],
            )
        ]
    )
    fused = fuse_documents(layout, native)
    page = fused.document.pages[0]
    # All three native lines are covered by the layout paragraph: none may be
    # appended as extra blocks.
    assert len(page.blocks) == 1
    # Their refs are recorded on the merged block.
    assert sum(1 for r in page.blocks[0].source_refs if r.startswith("native:")) == 3


def test_fusion_keeps_genuinely_uncovered_native_blocks() -> None:
    layout = _doc([_page(9, [_layout_block("layout:p9", (50.0, 100.0, 200.0, 110.0), "alpha")])])
    native = _doc(
        [
            _page(
                9,
                [
                    _layout_block("native:p9:r1", (50.0, 100.0, 200.0, 110.0), "alpha"),
                    _layout_block("native:p9:r9", (50.0, 400.0, 200.0, 410.0), "footer note"),
                ],
            )
        ]
    )
    fused = fuse_documents(layout, native)
    blocks = fused.document.pages[0].blocks
    assert len(blocks) == 2  # uncovered native keeps its own block
    assert any(b.block_id == "native:p9:r9" for b in blocks)


# ---- 5. no fabricated page-1 evidence -------------------------------------

def test_page_only_span_without_location_is_none_not_page1() -> None:
    from bio_pdf.extract.evidence import span_unlocated

    span = span_unlocated(
        document_id="sha256:x",
        parser_run_id="r1",
        raw_text="SEQ ID NO: 7",
        source_locator="ref:7",
    )
    assert span.page_number is None
    assert span.geometry_precision == "none"
    assert span.location_precision == "none"
    assert span.fragments == []  # no fabricated page-1 fragment


# ---- 6. SOURCE_CONFLICT keeps complete raws --------------------------------

def test_fusion_conflict_records_full_texts() -> None:
    long_layout = "A" * 250 + "AGLK" + "B" * 250
    long_native = "A" * 250 + "AG1K" + "B" * 250
    layout = _doc(
        [_page(1, [_layout_block("layout:p1", (10.0, 10.0, 300.0, 30.0), long_layout)])]
    )
    native = _doc(
        [_page(1, [_layout_block("native:p1:r1", (10.0, 10.0, 300.0, 30.0), long_native)])]
    )
    fused = fuse_documents(layout, native)
    assert fused.conflicts, "conflict must be recorded"
    conflict = fused.conflicts[0]
    # Full texts, not a 120-char prefix.
    assert len(conflict["primary_text"]) == len(long_layout)
    assert len(conflict["native_text"]) == len(long_native)
    assert conflict["block_id"] == "layout:p1"
