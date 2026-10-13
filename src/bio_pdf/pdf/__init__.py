"""Native PDF primitives: text, geometry, rendering, quality."""

from bio_pdf.pdf.geometry import (
    PageTransform,
    build_crop_from_top_left,
    identity_transform,
    normalize_pdfium_bbox,
    pdfium_bbox_to_top_left,
    top_left_to_pdfium_bbox,
)
from bio_pdf.pdf.quality import (
    RouteDecision,
    classify_page,
    inspect_page_signals,
)
from bio_pdf.pdf.render import render_evidence_crop, render_page_png
from bio_pdf.pdf.native import read_native_pages

__all__ = [
    "PageTransform",
    "build_crop_from_top_left",
    "identity_transform",
    "normalize_pdfium_bbox",
    "pdfium_bbox_to_top_left",
    "top_left_to_pdfium_bbox",
    "RouteDecision",
    "classify_page",
    "inspect_page_signals",
    "render_evidence_crop",
    "render_page_png",
    "read_native_pages",
]