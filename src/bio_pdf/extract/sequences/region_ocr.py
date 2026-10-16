"""Targeted page OCR for rendered ST.25 residue areas (C03).

When a listing page declares ``<400>`` blocks but the residue region carries
no text-layer entry (the residues are rendered as an image / outline block),
the layout engine can misclassify the whole area (e.g. as a table) and the
text never reaches extraction. This module renders the page at high DPI and
OCRs it, returning each line WITH its vertical position so the caller can
interleave it into the page's real reading order (appending a whole page's
OCR text after the page's tags mis-attributed DNA rows to the next SEQ ID).

A full-page render (validated on EP3027649B1 page 32: the rendered DNA rows
parse cleanly) is used instead of object-bbox crops — image-object
coordinates on these pages proved unreliable to map back to render space.

Bounded by the caller: only pages whose declarations are still missing after
the first pass, and at most the configured region budget.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Render DPI for the OCR pass — small listing fonts need the detail.
_REGION_DPI = 300

#: ST.25 tag lines are already in the native text layer; OCR copies would
#: duplicate declarations. Dropped from the OCR feed.
_TAG_LINE_RE = re.compile(r"^\s*<\s*\d{3}\s*>")

#: Bounded guard against a runaway OCR page (the listing pages in the corpus
#: produce a few hundred lines at most).
_MAX_OCR_LINES = 800


def ocr_residue_pages(
    pdf_path: Path,
    page_numbers: list[int],
) -> dict[int, list[tuple[float, float, str]]]:
    """OCR whole pages with RapidOCR.

    Returns ``page → [(y_top_pt, y_bottom_pt, text), ...]`` sorted
    top-to-bottom, where the y values are the line's top/bottom edges in PDF
    points with a top-left origin — the same space the block bboxes use, so
    callers can (a) interleave OCR lines with native blocks into true reading
    order and (b) compare positions to decide whether two texts describe the
    SAME physical region. Tag-like lines are dropped (the native layer
    already carries them).
    """

    import numpy as np
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    scale = _REGION_DPI / 72
    engine = RapidOCR()
    doc = pdfium.PdfDocument(str(pdf_path))
    out: dict[int, list[tuple[float, float, str]]] = {}
    try:
        for page_number in page_numbers:
            if page_number < 1 or page_number > len(doc):
                continue
            page = doc[page_number - 1]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
            result = engine(np.array(image))
            boxes = result.boxes if result.boxes is not None else []
            lines: list[tuple[float, float, str]] = []
            for box, text in zip(boxes, result.txts or ()):
                if not text or not text.strip() or _TAG_LINE_RE.match(text):
                    continue
                ys = [point[1] for point in box]
                # Plain Python floats: numpy scalars would poison JSON
                # payloads downstream (issue region coordinates).
                lines.append(
                    (float(min(ys)) / float(scale), float(max(ys)) / float(scale), text.strip())
                )
            lines.sort(key=lambda item: item[0])
            if lines:
                out[page_number] = lines[:_MAX_OCR_LINES]
            page.close()
    finally:
        doc.close()
    return out
