"""Native text extraction through pypdfium2.

The strategy is:

1. Get the visible page geometry (already in the contract's top-left space).
2. Read each PDF text rect as a single block. ``PdfTextPage.get_rect`` returns
   one rect per visual line so we avoid per-character glue problems.
3. Group adjacent rects into blocks when they belong to the same line band.

We deliberately keep the surface tiny and avoid the per-character
``get_charbox`` API because its box-vs-indexed-char alignment is unreliable in
pypdfium2 4.30.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from bio_pdf.contracts import (
    BoundingBox,
    NormalizedDocument,
    NormalizedPage,
    Provenance,
    TextBlock,
)
from bio_pdf.errors import PageCoverageMismatchError


def _textpage_lines(
    textpage: Any, *, page_height_pt: float
) -> list[TextBlock]:
    """Return one :class:`TextBlock` per visual line on the page.

    Uses :meth:`PdfTextPage.get_rect` which yields one rect per visual line in
    bottom-left coordinates. We convert each rect to top-left here.
    """

    blocks: list[TextBlock] = []
    rect_count = textpage.count_rects()
    for idx in range(rect_count):
        rect = textpage.get_rect(idx)
        if rect is None or len(rect) != 4:
            continue
        x0, y0, x1, y1 = rect
        text = textpage.get_text_bounded(x0, y0, x1, y1) or ""
        text = text.strip()
        if not text:
            continue
        # Convert to top-left origin.
        top = page_height_pt - y1
        bottom = page_height_pt - y0
        bbox: BoundingBox = (float(x0), float(top), float(x1), float(bottom))
        blocks.append(
            TextBlock(
                block_id=f"native:r{idx}",
                kind="text",
                bbox=bbox,
                text_raw=text,
            )
        )
    return blocks


def read_native_pages(
    pdf_path: Path,
    pages: tuple[int, ...],
    *,
    parser_run_id: str,
) -> NormalizedDocument:
    """Read ``pages`` from ``pdf_path`` and produce a :class:`NormalizedDocument`.

    Page numbers in ``pages`` are 1-based. The result preserves reading order
    at the page level; per-page block order follows pypdfium2's rect order.
    """

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(pdf)
        page_numbers = list(pages)
        if page_count == 0:
            raise PageCoverageMismatchError(
                f"PDF has zero pages: {pdf_path}",
                details={"path": str(pdf_path)},
            )

        out_of_range = [p for p in page_numbers if p < 1 or p > page_count]
        if out_of_range:
            raise PageCoverageMismatchError(
                f"page numbers out of range {out_of_range} (have {page_count})",
                details={"pages": out_of_range, "page_count": page_count},
            )

        normalised_pages: list[NormalizedPage] = []
        document_id = f"sha256:{_compute_sha256(pdf_path)}"

        for page_number in page_numbers:
            page = pdf[page_number - 1]
            width_pt, height_pt = page.get_size()
            textpage = page.get_textpage()
            try:
                text_blocks = _textpage_lines(textpage, page_height_pt=float(height_pt))
            finally:
                textpage.close()

            normalised_pages.append(
                NormalizedPage(
                    page_number=page_number,
                    width_pt=float(width_pt),
                    height_pt=float(height_pt),
                    route="native",
                    blocks=text_blocks,
                )
            )

        # Attach provenance to each block.
        provenance = [
            Provenance(
                source_type="native_text",
                artifact_ref=f"runs/{parser_run_id}/native.jsonl",
                provider_locator=f"page_count={page_count}",
            )
        ]
        for page in normalised_pages:
            for block in page.blocks:
                block.provenance = list(provenance)

        return NormalizedDocument(
            schema_version="1.0.0",
            document_id=document_id,
            parser_run_id=parser_run_id,
            pages=normalised_pages,
        )
    finally:
        pdf.close()


def _compute_sha256(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["read_native_pages"]