"""Common primitives shared by every contract module.

These types are deliberately tiny so they can be re-exported from
:mod:`bio_pdf.contracts` and reused in other packages without circular
imports.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

#: Current schema version. Bump whenever an exported JSON Schema changes in a
#: way that breaks compatibility with the previous revision.
SCHEMA_VERSION: Literal["v1"] = "v1"


class _StrictModel(BaseModel):
    """Base model that rejects unknown fields by default.

    The design forbids silently dropping typos (``record_kind`` vs.
    ``recrod_kind``). Use :class:`_StrictModel` for every contract class.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        ser_json_inf_nan="constants",
        validate_assignment=True,
    )


#: Non-empty, trimmed string. Used for ids, codes and human labels.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


#: Page numbers are 1-based, matching the contract design. Negative or zero
#: page numbers are rejected at the type boundary.
PageNumber = Annotated[int, Field(ge=1)]


#: Coordinates are stored as ``(x0, y0, x1, y1)`` in PDF user space, where
#: ``(0, 0)`` is the bottom-left corner of the page. The tuple shape lets us
#: export stable JSON arrays and consume them from Go/TS without losing
#: precision through Decimal.
BoundingBox = Annotated[
    tuple[float, float, float, float],
    Field(
        min_length=4,
        max_length=4,
        description=(
            "Axis-aligned box in PDF user space (points). Origin is "
            "bottom-left; x0 <= x1, y0 <= y1."
        ),
    ),
]


def validate_bbox(
    bbox: tuple[float, float, float, float],
    *,
    width_pt: float,
    height_pt: float,
) -> None:
    """Validate a :data:`BoundingBox` against page geometry.

    The function deliberately raises :class:`ValueError` (not a Pydantic
    validation error) so it can be reused in non-pydantic contexts such as
    parsing helpers and downstream Go/TS shims.

    Args:
        bbox: ``(x0, y0, x1, y1)`` in PDF points.
        width_pt: Page width in points.
        height_pt: Page height in points.

    Raises:
        ValueError: If any coordinate is non-finite, the axes are swapped,
            the box is inverted, or the box extends outside the page.
    """

    if len(bbox) != 4:
        raise ValueError(f"bbox must have 4 coordinates, got {len(bbox)}")

    x0, y0, x1, y1 = bbox

    for label, value in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
        if not math.isfinite(value):
            raise ValueError(f"{label}={value!r} is not finite")

    if x0 > x1:
        raise ValueError(f"x0 ({x0}) > x1 ({x1}); axes are swapped")
    if y0 > y1:
        raise ValueError(f"y0 ({y0}) > y1 ({y1}); axes are inverted")

    if width_pt <= 0 or height_pt <= 0:
        raise ValueError(
            f"page size must be positive, got width_pt={width_pt} height_pt={height_pt}"
        )

    if x0 < 0 or y0 < 0 or x1 > width_pt or y1 > height_pt:
        raise ValueError(
            f"bbox {bbox!r} is outside the page bounds "
            f"({width_pt} x {height_pt} pt)"
        )


class EvidenceSpan(_StrictModel):
    """A location reference for an extracted value or statement.

    The ``bbox`` field is optional because some upstream sources (e.g. an
    OCR-only PDF, or a continuation cell without its own coordinates) only
    know the page. The contract distinguishes ``bbox is None`` from
    ``bbox is not None but page is None``.
    """

    evidence_id: NonEmptyStr
    document_id: NonEmptyStr
    page_number: PageNumber | None = None
    bbox: BoundingBox | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    raw_text: NonEmptyStr | None = None

    def geometry_present(self) -> bool:
        """Return True if the span has at least one geometry anchor.

        Used by validators that want to warn when an extracted value has no
        page-level coordinate at all.
        """

        return self.page_number is not None or self.bbox is not None