"""Geometry primitives for native PDF parsing.

The contract uses a top-left-origin convention (``(x0, y0, x1, y1)`` measured
from the visible page top-left). pypdfium2 emits coordinates in a
bottom-left-origin space and may rotate the page. This module is the single
place that knows about those differences so the rest of the code can rely on
the contract coordinates.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypeAlias

from bio_pdf.contracts import (
    BoundingBox,
    validate_bbox,
)

#: Affine transform as a 6-tuple ``(a, b, c, d, e, f)`` mapping PDF points to
#: image pixels (PDFium-style). Identity is ``(1, 0, 0, 1, 0, 0)``.
Transform6: TypeAlias = tuple[float, float, float, float, float, float]


class PageTransform:
    """Describe how to go from PDF (top-left) coordinates to image pixels.

    ``scale_x``/``scale_y`` capture the DPI-driven scale; ``offset_x``/
    ``offset_y`` is the top-left origin of the rendered region inside the
    page image. ``width_pt``/``height_pt`` are the visible page dimensions
    that produced this transform.
    """

    __slots__ = ("scale_x", "scale_y", "offset_x", "offset_y", "width_pt", "height_pt", "rotation_deg")

    def __init__(
        self,
        *,
        scale_x: float,
        scale_y: float,
        offset_x: float,
        offset_y: float,
        width_pt: float,
        height_pt: float,
        rotation_deg: int = 0,
    ) -> None:
        self.scale_x = scale_x
        self.scale_y = scale_y
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.width_pt = width_pt
        self.height_pt = height_pt
        self.rotation_deg = rotation_deg

    def to_matrix(self) -> Transform6:
        """Return the affine as ``(a, b, c, d, e, f)``."""

        return (self.scale_x, 0.0, 0.0, self.scale_y, self.offset_x, self.offset_y)


def identity_transform(width_pt: float, height_pt: float, *, rotation_deg: int = 0) -> PageTransform:
    """Return a 1:1 transform that maps page points to page points."""

    return PageTransform(
        scale_x=1.0,
        scale_y=1.0,
        offset_x=0.0,
        offset_y=0.0,
        width_pt=width_pt,
        height_pt=height_pt,
        rotation_deg=rotation_deg,
    )


def pdfium_bbox_to_top_left(
    bbox: Sequence[float],
    *,
    page_height_pt: float,
) -> BoundingBox:
    """Convert a pypdfium2 bbox (bottom-left origin) to top-left origin.

    pypdfium2 returns bboxes in PDF user space with ``(0, 0)`` at the
    bottom-left of the visible page. The contract uses ``(0, 0)`` at the
    top-left, so we flip the Y axis here.
    """

    if len(bbox) != 4:
        raise ValueError(f"bbox must have 4 coordinates, got {len(bbox)}")
    x0, y0, x1, y1 = bbox
    top = page_height_pt - y1
    bottom = page_height_pt - y0
    return (float(x0), float(top), float(x1), float(bottom))


def top_left_to_pdfium_bbox(
    bbox: Sequence[float],
    *,
    page_height_pt: float,
) -> BoundingBox:
    """Inverse of :func:`pdfium_bbox_to_top_left`."""

    if len(bbox) != 4:
        raise ValueError(f"bbox must have 4 coordinates, got {len(bbox)}")
    x0, y0, x1, y1 = bbox
    return (float(x0), float(page_height_pt - y1), float(x1), float(page_height_pt - y0))


def normalize_pdfium_bbox(
    bbox: Sequence[float],
    *,
    page_transform: PageTransform,
    page_height_pt: float,
) -> BoundingBox:
    """Convert a pypdfium2 bbox to a top-left bbox in the *current* page space.

    ``page_transform`` carries the visible page dimensions; we only need
    ``height_pt`` here. The function is intentionally simple so callers can
    pass either rotated or unrotated pages without surprises.
    """

    return pdfium_bbox_to_top_left(bbox, page_height_pt=page_height_pt)


def build_crop_from_top_left(
    bbox: BoundingBox,
    *,
    page_width_pt: float,
    page_height_pt: float,
) -> tuple[float, float, float, float]:
    """Validate ``bbox`` and return the same coordinates.

    This helper exists so that a future migration to a different top-left
    origin (e.g. renderer offsets) can be done in one place.
    """

    validate_bbox(bbox, width_pt=page_width_pt, height_pt=page_height_pt)
    return bbox


def apply_rotation(
    bbox: BoundingBox,
    *,
    rotation_deg: int,
    page_width_pt: float,
    page_height_pt: float,
) -> BoundingBox:
    """Rotate ``bbox`` by ``rotation_deg`` clockwise.

    The result is expressed in the *new* visible page space (post-rotation).
    """

    rotation = rotation_deg % 360
    if rotation == 0:
        return (bbox[0], bbox[1], bbox[2], bbox[3])
    if rotation not in {90, 180, 270}:
        raise ValueError(f"unsupported rotation: {rotation_deg}")

    x0, y0, x1, y1 = bbox
    if rotation == 90:
        # Rotate 90° clockwise. New page width/height swap.
        new_w, new_h = page_height_pt, page_width_pt
        return (
            float(y0),
            float(new_h - x1),
            float(y1),
            float(new_h - x0),
        )
    if rotation == 180:
        return (
            float(page_width_pt - x1),
            float(page_height_pt - y1),
            float(page_width_pt - x0),
            float(page_height_pt - y0),
        )
    # 270
    new_w, new_h = page_height_pt, page_width_pt
    return (
        float(new_w - y1),
        float(x0),
        float(new_w - y0),
        float(x1),
    )


def map_pixel_to_page(
    pixel_xy: tuple[float, float],
    *,
    transform: PageTransform,
) -> tuple[float, float]:
    """Map a pixel (x, y) inside a rendered image back to PDF page points.

    The inverse of the renderer's scale-and-translate is just
    ``(px - offset) / scale``.
    """

    px, py = pixel_xy
    if transform.scale_x == 0 or transform.scale_y == 0:
        raise ValueError("transform has zero scale")
    return (
        (px - transform.offset_x) / transform.scale_x,
        (py - transform.offset_y) / transform.scale_y,
    )


def clip_bbox_to_page(
    bbox: BoundingBox,
    *,
    width_pt: float,
    height_pt: float,
) -> BoundingBox:
    """Return ``bbox`` clipped to the visible page bounds.

    Used when an upstream source (e.g. an OCR crop) hands us coordinates
    that extend slightly past the page edge.
    """

    x0, y0, x1, y1 = bbox
    if not (math.isfinite(x0) and math.isfinite(y0) and math.isfinite(x1) and math.isfinite(y1)):
        raise ValueError(f"bbox has non-finite values: {bbox!r}")
    x0c = max(0.0, min(x0, width_pt))
    x1c = max(0.0, min(x1, width_pt))
    y0c = max(0.0, min(y0, height_pt))
    y1c = max(0.0, min(y1, height_pt))
    if x0c >= x1c or y0c >= y1c:
        raise ValueError(f"bbox collapsed after clipping: {bbox!r} in {width_pt}x{height_pt}")
    return (x0c, y0c, x1c, y1c)