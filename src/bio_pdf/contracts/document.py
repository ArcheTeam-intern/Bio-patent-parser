"""Document-level contracts: ingestion inspection, normalised output, quality."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from bio_pdf.contracts.common import (
    BoundingBox,
    NonEmptyStr,
    PageNumber,
    _StrictModel,
)

RouteLabel = Literal[
    "native",
    "layout_native",
    "layout_ocr",
    "scan_suspect",
    "text_suspect",
    "mixed",
    "blank",
    "unknown",
]

BlockKind = Literal[
    "text",
    "title",
    "list",
    "table",
    "picture",
    "formula",
    "sequence_listing",
    "unknown",
]

#: Semantic role of a block: what the text *is*, decided from layout/section
#: evidence. Role drives extraction gating (authors/references are never
#: sequence candidates). Uncertain → "unknown"; never guess from blacklists
#: of person names.
SemanticRole = Literal[
    "body",
    "title",
    "caption",
    "author",
    "header",
    "footer",
    "footnote",
    "reference",
    "claims",
    "methods",
    "sequence_listing",
    "unknown",
]

#: Role of a cell inside the table's header structure.
CellHeaderRole = Literal["column", "row", "both", "data", "unknown"]


class PageGeometry(_StrictModel):
    """Page dimensions in PDF points after rotation/CropBox has been applied.

    The width/height are expressed in the user-facing (visible) coordinate
    system. Downstream geometry maths MUST rely on these values, never on
    the raw MediaBox.
    """

    page_number: PageNumber
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)
    rotation_deg: int = Field(default=0, ge=0, le=359)


class DocumentInspection(_StrictModel):
    """The result of the ingestion inspector.

    Carries the SHA-256 of the original bytes (used as ``document_id``),
    the page geometry list and encryption flags. Parsers must never see
    raw bytes they have not first verified.
    """

    document_id: NonEmptyStr
    sha256: NonEmptyStr
    bytes: int = Field(ge=0)
    page_count: int = Field(ge=0)
    is_encrypted: bool = False
    pages: list[PageGeometry] = Field(default_factory=list)


class QualityConfig(_StrictModel):
    """Thresholds used by the page-quality classifier.

    The defaults mirror ``configs/demo.toml`` and are deliberately
    conservative. Tune after we have real-sample coverage.
    """

    scan_image_coverage: float = Field(default=0.8, ge=0, le=1)
    low_text_char_count: int = Field(default=20, ge=0)
    replacement_ratio: float = Field(default=0.02, ge=0, le=1)
    text_suspect_garbled_ratio: float = Field(default=0.4, ge=0, le=1)
    blank_max_chars: int = Field(default=5, ge=0)


class PageQuality(_StrictModel):
    """Per-page quality signals.

    ``image_coverage`` is the union of image area over the visible page
    area; the design forbids naïve sum-based coverage that can exceed 1.0.
    """

    page_number: PageNumber
    nonspace_chars: int = Field(ge=0)
    printable_ratio: float = Field(ge=0, le=1)
    replacement_ratio: float = Field(ge=0, le=1)
    image_coverage: float = Field(ge=0, le=1)
    repeated_char_ratio: float = Field(default=0.0, ge=0, le=1)
    hidden_text_flag: bool = False
    warnings: list[NonEmptyStr] = Field(default_factory=list)


class RouteDecision(_StrictModel):
    """The routing decision for a page.

    ``reasons`` carries the human-readable rationale (referencing the
    signal values) and ``rule_version`` pins the classifier so reviewers
    know which version of the rule produced the decision.
    """

    route: RouteLabel
    reasons: list[NonEmptyStr] = Field(default_factory=list)
    rule_version: NonEmptyStr = "v0"


# ---------------------------------------------------------------------------
# Normalised document blocks / tables
# ---------------------------------------------------------------------------


class Provenance(_StrictModel):
    """One source-of-truth link back to an upstream artifact."""

    source_type: NonEmptyStr
    artifact_ref: NonEmptyStr
    provider_locator: NonEmptyStr | None = None


class TextBlock(_StrictModel):
    """A single block on a normalised page.

    Blocks are the atom the domain rules consume. ``bbox`` may be ``None``
    if the upstream source could not produce coordinates (e.g. an OCR page
    that returned text only). ``kind`` separates sequence listings from
    regular prose so downstream extractors can dispatch cheaply.

    ``ordinal`` is the block's 0-based position in the provider's reading
    order for the page — stable evidence addressing that survives block-id
    formatting changes. ``source_refs`` links to upstream provider items
    (e.g. Docling ``#/texts/42``) so evidence can be re-derived.
    """

    block_id: NonEmptyStr
    kind: BlockKind = "text"
    semantic_role: SemanticRole = "unknown"
    bbox: BoundingBox | None = None
    text_raw: NonEmptyStr
    table_id: NonEmptyStr | None = None
    ordinal: int | None = Field(default=None, ge=0)
    provenance: list[Provenance] = Field(default_factory=list)
    source_refs: list[NonEmptyStr] = Field(default_factory=list)
    #: Consumed other-channel blocks (id + text) recorded by fusion, so the
    #: extractor can attach alias evidence ONLY to the lines that actually
    #: contain a matched expression (plan B02 — no blanket claims).
    source_aliases: list[dict[str, str]] = Field(default_factory=list)


class TableCell(_StrictModel):
    """A cell inside a normalised table.

    ``row_index``/``column_index`` are zero-based. ``rowspan``/``colspan``
    follow the HTML convention (both default to 1).

    ``text_raw`` may be an empty string: a *present but empty* cell
    (placeholder under a merged header) is different from a *missing* cell
    (absent from ``cells``). ``header_role`` records whether the cell acts
    as a column header, row header, both, or plain data.
    """

    cell_id: NonEmptyStr
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    rowspan: int = Field(default=1, ge=1)
    colspan: int = Field(default=1, ge=1)
    text_raw: str
    header_role: CellHeaderRole = "unknown"
    bbox: BoundingBox | None = None
    source_refs: list[NonEmptyStr] = Field(default_factory=list)


class Table(_StrictModel):
    """A normalised table.

    ``header_cell_ids`` keeps the linkage to the cells acting as the
    column/row headers so evidence can travel with each measurement.
    ``footnote_refs``/``caption_refs`` point at the block ids carrying the
    table's caption and footnotes (units and legend text often live there).

    ``structure_issues`` lists detected structural problems (span overlap,
    ragged rows, missing grid positions); a table with issues must not
    silently pass downstream checks as a healthy matrix.
    """

    table_id: NonEmptyStr
    page_number: PageNumber
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    cells: list[TableCell]
    header_cell_ids: list[NonEmptyStr] = Field(default_factory=list)
    caption: NonEmptyStr | None = None
    caption_refs: list[NonEmptyStr] = Field(default_factory=list)
    footnote_refs: list[NonEmptyStr] = Field(default_factory=list)
    bbox: BoundingBox | None = None
    continuation_group: NonEmptyStr | None = None
    structure_issues: list[NonEmptyStr] = Field(default_factory=list)
    #: How the table structure was obtained. ``geometry_reconstructed``
    #: means the parser produced no table object for this region and the
    #: grid was rebuilt from text-block geometry — downstream consumers
    #: must treat such tables as review-required, never parser truth.
    source: Literal["parser", "geometry_reconstructed"] = "parser"


class Figure(_StrictModel):
    """A non-text visual region (chart / plot / diagram / photo).

    ``kind`` classifies what the region most likely is (from the parser
    label + nearby keywords). The bbox is the crop region used to export
    the figure as a standalone image; ``caption`` carries the resolved
    figure caption so the image travels with its title.
    """

    figure_id: NonEmptyStr
    page_number: PageNumber
    bbox: BoundingBox
    kind: Literal["chart", "plot", "diagram", "photo", "picture"] = "picture"
    caption: NonEmptyStr | None = None
    caption_refs: list[NonEmptyStr] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)


class NormalizedPage(_StrictModel):
    """A page in a :class:`NormalizedDocument`."""

    page_number: PageNumber
    printed_page_label: NonEmptyStr | None = None
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)
    route: RouteLabel = "unknown"
    blocks: list[TextBlock] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    #: Visual regions (charts / plots / diagrams) detected by the layout
    #: parser; each can be exported as a standalone image via its bbox.
    figures: list[Figure] = Field(default_factory=list)


class NormalizedDocument(_StrictModel):
    """The unified parser output.

    Produced by every adapter (native, Docling, MinerU). Domain code never
    sees the SDK-specific objects directly.
    """

    schema_version: NonEmptyStr
    document_id: NonEmptyStr
    parser_run_id: NonEmptyStr
    pages: list[NormalizedPage]
    errors: list[NonEmptyStr] = Field(default_factory=list)


class EvidenceImage(_StrictModel):
    """A rendered PNG tied back to the page coordinates it was clipped from.

    ``transform_to_page`` describes the affine used to go from image pixels
    back to PDF points; it is stored separately from ``bbox`` because the
    rendered image may have been clipped, scaled or de-skewed.
    """

    artifact_ref: NonEmptyStr
    document_id: NonEmptyStr
    page_number: PageNumber
    bbox: BoundingBox
    dpi: int = Field(ge=72)
    pixel_width: int = Field(ge=1)
    pixel_height: int = Field(ge=1)
    transform_to_page: list[float] = Field(min_length=6, max_length=6)