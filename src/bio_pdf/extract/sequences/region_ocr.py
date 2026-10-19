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

from pathlib import Path

#: Render DPI for the OCR pass — small listing fonts need the detail.
_REGION_DPI = 300

#: ST.25 tag lines are KEPT in the OCR feed (K05 fix).
#:
#: The old rule dropped them because "they are already in the native text
#: layer" — but a listing page can carry a PARTIAL declaration (``<210> 1``
#: and ``<211> 549`` present, ``<212>`` missing) while the OCR pass reads
#: the complete line. Dropping the tag line made that recovery impossible:
#: the multi-line declaration merge (K05) could never fire in production,
#: only in tests that injected OCR lines directly.
#:
#: Duplicate safety lives DOWNSTREAM in ``_merge_ocr_page_lines``: an OCR
#: line whose fingerprint equals the native one is dropped, complementary
#: fields merge into ONE logical declaration, and a value disagreement is
#: recorded as ``content_conflict`` instead of silently picking a side.

#: Bounded guard against a runaway OCR page (the listing pages in the corpus
#: produce a few hundred lines at most).
_MAX_OCR_LINES = 800


def ocr_residue_pages(
    pdf_path: Path,
    page_numbers: list[int],
) -> dict[int, list[tuple[float, float, float, float, str]]]:
    """OCR whole pages with RapidOCR.

    Returns ``page → [(x0, y0, x1, y1, text), ...]`` sorted top-to-bottom,
    the FULL 2D box of each line in PDF points with a top-left origin — the
    same space the block bboxes use, so callers can (a) interleave OCR lines
    into reading order and (b) decide SAME-PHYSICAL-MENTION overlap on both
    axes (E5: a y-overlap alone confuses columns). ST.25 tag lines are KEPT
    so a partial native declaration can be completed (K05).
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
            lines: list[tuple[float, float, float, float, str]] = []
            for box, text in zip(boxes, result.txts or ()):
                if not text or not text.strip():
                    continue
                xs = [float(point[0]) / scale for point in box]
                ys = [float(point[1]) / scale for point in box]
                lines.append(
                    (min(xs), min(ys), max(xs), max(ys), text.strip())
                )
            lines.sort(key=lambda item: item[1])
            if lines:
                out[page_number] = lines[:_MAX_OCR_LINES]
            page.close()
    finally:
        doc.close()
    return out
