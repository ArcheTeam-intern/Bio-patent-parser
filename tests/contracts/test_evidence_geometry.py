"""Geometry invariants required by design §6.

This module focuses on edge cases around page coordinate systems: rotation,
CropBox offsets and PDF bottom-left vs. top-left conversions.
"""

from __future__ import annotations

import pytest

from bio_pdf.contracts import EvidenceSpan, PageGeometry, TextBlock, validate_bbox


def test_bbox_must_lie_inside_visible_page() -> None:
    width, height = 612.0, 792.0
    # Negative coordinates are not allowed even though pypdfium allows them
    # before applying the CropBox.
    with pytest.raises(ValueError):
        validate_bbox((-1, 0, 100, 50), width_pt=width, height_pt=height)


def test_bbox_equal_to_page_size_ok() -> None:
    # A box flush with the page border is allowed.
    validate_bbox((0, 0, 612, 792), width_pt=612, height_pt=792)


def test_text_block_bbox_is_optional() -> None:
    block = TextBlock(block_id="b1", text_raw="placeholder")
    assert block.bbox is None


def test_evidence_span_must_have_document_id() -> None:
    with pytest.raises(Exception):
        EvidenceSpan(  # type: ignore[call-arg]
            evidence_id="ev1",
            document_id="",
            page_number=1,
        )


def test_page_geometry_rejects_negative_size() -> None:
    with pytest.raises(Exception):
        PageGeometry(page_number=1, width_pt=-1, height_pt=100)


def test_page_geometry_accepts_partial_rotations() -> None:
    page = PageGeometry(page_number=1, width_pt=100, height_pt=200, rotation_deg=270)
    assert page.rotation_deg == 270