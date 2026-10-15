"""Native PDF parsing integration tests using ReportLab-generated fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from bio_pdf.errors import PageCoverageMismatchError
from bio_pdf.ingest.inspector import compute_sha256
from bio_pdf.pdf.native import read_native_pages


def _build_fixture_pdf(
    target: Path,
    *,
    page_specs: Iterable[tuple[str, list[tuple[float, float, str]]]],
) -> Path:
    """Create a fixture PDF where each page draws the supplied text strings.

    ``page_specs`` is an iterable of ``(page_label, [(x, y, text), …])``. Each
    tuple draws ``text`` at the bottom-left ``(x, y)``. We draw at the PDF
    origin to make bbox expectations unambiguous.
    """

    c = canvas.Canvas(str(target), pagesize=letter)
    for label, items in page_specs:
        for x, y, text in items:
            c.drawString(x, y, text)
        c.showPage()
    c.save()
    return target


@pytest.fixture()
def fixture_two_columns(tmp_path: Path) -> Path:
    """Two-column fixture: left column has SEQ 1 sequence, right column has SEQ 2.

    Page 1 also draws a stray number on its own row to ensure the line-block
    heuristic does not glue it to a sequence line. Page 2 has a single
    activity-value line.
    """

    page1 = [
        (50, 720, "SEQ ID NO: 1"),
        (50, 705, "Met Lys Thr Xaa Gly Ala"),
        (50, 690, "1 5"),
        (300, 720, "SEQ ID NO: 2"),
        (300, 705, "Arg Cys Asp Glu Phe Trp"),
        (300, 690, "1 5"),
        (50, 670, "1 5 10"),
    ]
    page2 = [
        (50, 720, "IC50 ≤ 1.2 nM"),
    ]
    target = tmp_path / "native_fixture.pdf"
    return _build_fixture_pdf(target, page_specs=[("page1", page1), ("page2", page2)])


def test_native_extraction_preserves_left_and_right_columns(fixture_two_columns: Path) -> None:
    document = read_native_pages(
        fixture_two_columns, pages=(1,), parser_run_id="test-run"
    )
    assert document.document_id == f"sha256:{compute_sha256(fixture_two_columns)}"
    page = document.pages[0]
    texts = [b.text_raw for b in page.blocks]

    # Both sequence lines must be in the page; the heuristic must not collapse
    # the right column into the left column.
    joined = "\n".join(texts)
    assert "Met Lys Thr Xaa Gly Ala" in joined
    assert "Arg Cys Asp Glu Phe Trp" in joined


def test_native_extraction_does_not_glue_numbers_into_sequences(
    fixture_two_columns: Path,
) -> None:
    document = read_native_pages(
        fixture_two_columns, pages=(1,), parser_run_id="test-run"
    )
    joined = "\n".join(b.text_raw for b in document.pages[0].blocks)
    # The row containing "1 5 10" must remain its own line; we don't expect
    # the digits to appear glued to a sequence line.
    assert "1 5 10" in joined
    # Make sure the heuristic didn't merge "1 5 10" into the sequence line.
    for block in document.pages[0].blocks:
        if "Met Lys" in block.text_raw:
            assert "1 5 10" not in block.text_raw


def test_native_extraction_keeps_glyphs_for_re_read(fixture_two_columns: Path) -> None:
    document = read_native_pages(
        fixture_two_columns, pages=(2,), parser_run_id="test-run"
    )
    page = document.pages[0]
    joined = "\n".join(b.text_raw for b in page.blocks)
    # pypdfium2 normalises ligatures differently; check we preserved at least
    # the numeric + unit portion of the activity line.
    assert "1.2 nM" in joined
    # The comparator may be rendered as "≤" or as the glyph "≤" depending on
    # the font; we only assert its presence in raw form is *not* required but
    # the digits are.


def test_source_page_seven_still_page_number_seven(tmp_path: Path) -> None:
    """A page range starting at 7 must keep the source page numbering."""

    target = tmp_path / "many_pages.pdf"
    pdf = pdfium.PdfDocument.new()
    for _ in range(10):
        pdf.new_page(width=595, height=842)
    pdf.save(str(target))
    pdf.close()

    document = read_native_pages(target, pages=(7,), parser_run_id="run-7")
    assert document.pages[0].page_number == 7


def test_page_out_of_range_raises(tmp_path: Path) -> None:
    target = tmp_path / "small.pdf"
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(width=200, height=200)
    pdf.save(str(target))
    pdf.close()

    with pytest.raises(PageCoverageMismatchError):
        read_native_pages(target, pages=(2,), parser_run_id="x")


def test_blocks_have_top_left_bbox(fixture_two_columns: Path) -> None:
    document = read_native_pages(
        fixture_two_columns, pages=(1,), parser_run_id="test-run"
    )
    page = document.pages[0]
    assert page.height_pt > 0
    assert page.width_pt > 0
    for block in page.blocks:
        assert block.bbox is not None
        # Top-left origin: y0 < y1.
        x0, y0, x1, y1 = block.bbox
        assert y0 <= y1


def test_render_evidence_crop_writes_a_png(tmp_path: Path) -> None:
    from bio_pdf.pdf.render import render_evidence_crop

    target = tmp_path / "two_pages.pdf"
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(width=400, height=400)
    pdf.save(str(target))
    pdf.close()

    out = tmp_path / "out.png"
    ref = render_evidence_crop(
        target,
        page_number=1,
        bbox=(10.0, 10.0, 100.0, 50.0),
        dpi=200,
        output_path=out,
        document_id="sha256:abc",
    )
    assert out.exists()
    assert ref.pixel_width > 0
    assert ref.pixel_height > 0
    assert ref.dpi == 200


def test_inventory_native_pdf_pages_match_inspector(fixture_two_columns: Path) -> None:
    document = read_native_pages(
        fixture_two_columns, pages=(1, 2), parser_run_id="run-all"
    )
    assert len(document.pages) == 2
    # Document id is the sha256 of the underlying bytes; verify it matches an
    # independent hash so downstream stages can rely on it.
    raw_sha = hashlib.sha256(fixture_two_columns.read_bytes()).hexdigest()
    assert document.document_id == f"sha256:{raw_sha}"

def test_concurrent_renders_are_serialized(tmp_path) -> None:
    """Regression: parallel figure-thumbnail requests used to corrupt the
    process-global PDFium state (every later open failed with 'Data format
    error'). Rendering must survive concurrent callers."""
    from concurrent.futures import ThreadPoolExecutor

    from bio_pdf.pdf.render import render_page_png

    source = _build_fixture_pdf(
        tmp_path / "doc.pdf",
        page_specs=[("a", [(72, 720, "Page one")]), ("b", [(72, 720, "Page two")])],
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(render_page_png, source, i % 2, dpi=100)
            for i in range(16)
        ]
        images = [f.result()[0] for f in futures]
    assert len(images) == 16
    assert all(img.width > 0 and img.height > 0 for img in images)
