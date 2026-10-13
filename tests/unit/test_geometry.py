"""Geometry transformations: bottom-left ↔ top-left, rotation, CropBox."""

from __future__ import annotations

import pytest

from bio_pdf.contracts import validate_bbox
from bio_pdf.pdf.geometry import (
    PageTransform,
    apply_rotation,
    build_crop_from_top_left,
    clip_bbox_to_page,
    identity_transform,
    map_pixel_to_page,
    normalize_pdfium_bbox,
    pdfium_bbox_to_top_left,
    top_left_to_pdfium_bbox,
)


def test_bottom_left_to_top_left_is_inverse() -> None:
    page_height = 792.0
    original = (50.0, 100.0, 200.0, 300.0)  # bottom-left
    top_left = pdfium_bbox_to_top_left(original, page_height_pt=page_height)
    assert top_left == (50.0, page_height - 300.0, 200.0, page_height - 100.0)
    round_trip = top_left_to_pdfium_bbox(top_left, page_height_pt=page_height)
    assert round_trip == original


def test_normalize_pdfium_bbox_round_trip() -> None:
    page_height = 800.0
    transform = PageTransform(
        scale_x=1.0, scale_y=1.0, offset_x=0.0, offset_y=0.0,
        width_pt=600.0, height_pt=page_height, rotation_deg=0,
    )
    bbox = (10.0, 20.0, 100.0, 50.0)
    normalized = normalize_pdfium_bbox(bbox, page_transform=transform, page_height_pt=page_height)
    assert normalized == (10.0, page_height - 50.0, 100.0, page_height - 20.0)


def test_build_crop_from_top_left_validates() -> None:
    bbox = (10, 20, 100, 200)
    assert build_crop_from_top_left(bbox, page_width_pt=200, page_height_pt=300) == bbox


def test_build_crop_from_top_left_rejects_oversize() -> None:
    with pytest.raises(ValueError):
        build_crop_from_top_left((10, 20, 200, 100), page_width_pt=100, page_height_pt=100)


def test_apply_rotation_90_swaps_dimensions() -> None:
    # A 100x200 page with a box at top-left becomes a 200x100 page after 90°.
    bbox = (0, 0, 50, 100)
    out = apply_rotation(bbox, rotation_deg=90, page_width_pt=100, page_height_pt=200)
    # The contract uses the *new* page dims (200x100).
    validate_bbox(out, width_pt=200, height_pt=100)


def test_apply_rotation_180_mirrors_box() -> None:
    bbox = (0, 0, 100, 100)
    out = apply_rotation(bbox, rotation_deg=180, page_width_pt=200, page_height_pt=200)
    # The box's top-left (0,0) becomes bottom-right (200,200).
    assert out == (100.0, 100.0, 200.0, 200.0)


def test_apply_rotation_270_swaps_dimensions() -> None:
    bbox = (0, 0, 100, 50)
    out = apply_rotation(bbox, rotation_deg=270, page_width_pt=200, page_height_pt=100)
    validate_bbox(out, width_pt=100, height_pt=200)


def test_apply_rotation_unsupported_angle() -> None:
    with pytest.raises(ValueError):
        apply_rotation((0, 0, 10, 10), rotation_deg=45, page_width_pt=100, page_height_pt=100)


def test_map_pixel_to_page_inverse_of_scale() -> None:
    transform = PageTransform(
        scale_x=2.0, scale_y=2.0, offset_x=10.0, offset_y=20.0,
        width_pt=600, height_pt=800,
    )
    assert map_pixel_to_page((30.0, 40.0), transform=transform) == (10.0, 10.0)


def test_map_pixel_to_page_rejects_zero_scale() -> None:
    transform = PageTransform(
        scale_x=0.0, scale_y=0.0, offset_x=0.0, offset_y=0.0,
        width_pt=600, height_pt=800,
    )
    with pytest.raises(ValueError):
        map_pixel_to_page((0, 0), transform=transform)


def test_clip_bbox_to_page_clamps_oversize() -> None:
    out = clip_bbox_to_page((-5, -10, 700, 900), width_pt=600, height_pt=800)
    assert out == (0, 0, 600, 800)


def test_clip_bbox_to_page_rejects_collapsing_box() -> None:
    with pytest.raises(ValueError):
        clip_bbox_to_page((700, 600, 800, 900), width_pt=600, height_pt=800)


def test_clip_bbox_to_page_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        clip_bbox_to_page((float("nan"), 0, 100, 100), width_pt=200, height_pt=200)


def test_identity_transform_is_1_to_1() -> None:
    t = identity_transform(600, 800)
    assert t.scale_x == 1.0 and t.scale_y == 1.0
    assert t.width_pt == 600 and t.height_pt == 800