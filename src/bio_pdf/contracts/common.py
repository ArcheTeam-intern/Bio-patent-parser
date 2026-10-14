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

#: Domain result schema version carried by NormalizedDocument / ExtractionBatch
#: / exports. `1.1.0` adds semantic roles, evidence fragments and source
#: provenance; readers accept `1.0.0` via bio_pdf.contracts.compat.
DOMAIN_SCHEMA_VERSION = "1.1.0"


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


#: Coordinates are stored as ``(x0, y0, x1, y1)`` in PDF points relative to
#: the *visible* page, origin at the TOP-LEFT corner (the convention every
#: parser adapter normalises into — see :mod:`bio_pdf.pdf.geometry`).
#: The tuple shape lets us export stable JSON arrays and consume them from
#: Go/TS without losing precision through Decimal.
BoundingBox = Annotated[
    tuple[float, float, float, float],
    Field(
        min_length=4,
        max_length=4,
        description=(
            "Axis-aligned box in PDF points. Origin is the visible page's "
            "top-left corner; x0 <= x1, y0 <= y1."
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


class EvidenceFragment(_StrictModel):
    """One located piece of a (possibly multi-page / multi-box) evidence span.

    Fragments are the unit of precise location: a sequence that continues
    across pages has one fragment per continuation, a table-anchored value
    points at its cell. ``char_start``/``char_end`` are a Python/Unicode
    half-open interval into ``raw_text`` of the referenced block.
    """

    page_number: PageNumber
    bbox: BoundingBox | None = None
    block_id: NonEmptyStr | None = None
    table_id: NonEmptyStr | None = None
    cell_id: NonEmptyStr | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    raw_text: str | None = None
    source_artifact_id: NonEmptyStr | None = None


class EvidenceSpan(_StrictModel):
    """A location reference for an extracted value or statement.

    The ``bbox`` field is optional because some upstream sources (e.g. an
    OCR-only PDF, or a continuation cell without its own coordinates) only
    know the page. The contract distinguishes ``bbox is None`` from
    ``bbox is not None but page is None``.

    Multi-page / multi-box evidence is expressed through ``fragments``;
    legacy single-box payloads keep using ``page_number`` + ``bbox`` and
    ``bio_pdf.contracts.compat`` maps them to a one-fragment span.

    Page numbers are NEVER recovered from the evidence id: the id is an
    opaque content-derived string (see extract/evidence.py).
    """

    evidence_id: NonEmptyStr
    document_id: NonEmptyStr
    page_number: PageNumber | None = None
    bbox: BoundingBox | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    raw_text: NonEmptyStr | None = None

    source_parser: NonEmptyStr | None = None
    source_locator: NonEmptyStr | None = None
    fragments: list[EvidenceFragment] = Field(default_factory=list)
    location_precision: Literal["character", "cell", "block", "page", "none"] = "none"

    def geometry_present(self) -> bool:
        """Return True if the span has at least one geometry anchor.

        Used by validators that want to warn when an extracted value has no
        page-level coordinate at all.
        """

        if self.page_number is not None or self.bbox is not None:
            return True
        return any(f.page_number >= 1 for f in self.fragments)