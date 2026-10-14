"""Patent page discovery (plan B09).

Scans a PDF's extractable text to find candidate pages BEFORE any engine
runs: sequence-listing tags, SEQ ID mentions, claims, exampled activity
and table-like pages. Low-text pages are flagged as needing render/OCR —
never assumed to "have no sequences".

The output is a *candidate* map for humans and for the region-reparse
budget; it never fabricates page numbers or claims a listing was parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: ST.25 / ST.26 listing markers.
_ST25_TAG_RE = re.compile(r"<\s*(210|211|212|400)\s*>")
_SEQ_REF_RE = re.compile(r"SEQ\s+ID\s+NOS?\s*[.:]?", re.IGNORECASE)
_SEQUENCE_LISTING_RE = re.compile(r"sequence\s+listing", re.IGNORECASE)
_CLAIMS_RE = re.compile(r"(\bwhat\s+is\s+claimed\b|\bclaims?\b\s*[:.]?\s*$)", re.IGNORECASE | re.MULTILINE)
_EXAMPLE_RE = re.compile(r"\b(example|实施例)\s*\d", re.IGNORECASE)
_NUMERIC_TABLE_RE = re.compile(r"(?:\S+\s+\d+(?:\.\d+)?\s+){4,}")

#: Below this many non-space chars a page is render/OCR-suspect.
_LOW_TEXT_CHARS = 120


@dataclass
class PageCandidate:
    page_number: int
    text_chars: int
    candidates: list[str] = field(default_factory=list)
    needs_render: bool = False


def discover_candidate_pages(pdf_path: Path, *, max_pages: int | None = None) -> dict[str, Any]:
    """Scan text-extractable content and classify candidate pages."""

    import pypdfium2 as pdfium

    from bio_pdf.ingest.inspector import inspect_pdf

    inspection = inspect_pdf(pdf_path)
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages: list[PageCandidate] = []
    try:
        limit = min(len(pdf), max_pages or len(pdf))
        for index in range(limit):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded() or ""
            finally:
                textpage.close()
            chars = sum(1 for c in text if not c.isspace())
            candidates: list[str] = []
            if _ST25_TAG_RE.search(text):
                candidates.append("st25_listing")
            if _SEQUENCE_LISTING_RE.search(text):
                candidates.append("sequence_listing_heading")
            if _SEQ_REF_RE.search(text):
                candidates.append("seq_id_reference")
            if _CLAIMS_RE.search(text):
                candidates.append("claims")
            if _EXAMPLE_RE.search(text):
                candidates.append("example")
            if _NUMERIC_TABLE_RE.search(text):
                candidates.append("numeric_table")
            pages.append(
                PageCandidate(
                    page_number=index + 1,
                    text_chars=chars,
                    candidates=candidates,
                    needs_render=chars < _LOW_TEXT_CHARS,
                )
            )
    finally:
        pdf.close()

    return {
        "path": str(pdf_path),
        "document_id": inspection.document_id,
        "page_count": inspection.page_count,
        "scanned_pages": len(pages),
        "pages": [
            {
                "page_number": p.page_number,
                "text_chars": p.text_chars,
                "candidates": p.candidates,
                "needs_render": p.needs_render,
            }
            for p in pages
        ],
        "low_text_pages": [p.page_number for p in pages if p.needs_render],
    }


__all__ = ["discover_candidate_pages", "PageCandidate"]
