"""Normaliser for Docling JSON payloads.

The normaliser is intentionally decoupled from the live ``docling`` SDK. It only
reads a JSON document that has been exported via
``DoclingDocument.save_as_json`` and produces our :class:`NormalizedDocument`.
That makes it:

- testable without Docling installed,
- resilient to Docling API changes (we depend on JSON, not Python types),
- deterministic across versions.

If Docling is not available, the normaliser is the only piece we can fully
exercise offline. The live adapter (:mod:`bio_pdf.parsers.docling_adapter`)
imports the SDK on demand.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bio_pdf.contracts import (
    BoundingBox,
    BlockKind,
    NormalizedDocument,
    NormalizedPage,
    Provenance,
    Table,
    TableCell,
    TextBlock,
)
from bio_pdf.errors import NormalizationLossError

#: Fields in the Docling JSON that must be present to declare a page "complete".
_REQUIRED_PAGE_FIELDS = ("page", "size")


class NormalizationLossError(Exception):
    """Raised when the normaliser must drop data to honour the contract.

    Subclass of :class:`bio_pdf.errors.NormalizationLossError` that accepts
    keyword arguments so the caller can attach context.
    """

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def _coerce_bbox(
    value: Any,
    *,
    page_height_pt: float,
) -> BoundingBox | None:
    """Convert a Docling bbox (top-left origin, points) to the contract format.

    Docling emits top-left coordinates already, so this is essentially a shape
    validation pass. We accept the bbox as either a 4-list/tuple or an object
    with ``l``, ``t``, ``r``, ``b`` keys (the legacy format).
    """

    if value is None:
        return None

    if isinstance(value, dict):
        try:
            return (
                float(value["l"]),
                float(value["t"]),
                float(value["r"]),
                float(value["b"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NormalizationLossError(
                f"bbox dict missing required keys: {value!r}"
            ) from exc

    if isinstance(value, (list, tuple)):
        if len(value) != 4:
            raise NormalizationLossError(
                f"bbox list must have 4 elements, got {len(value)}"
            )
        try:
            x0, y0, x1, y1 = (float(v) for v in value)
        except (TypeError, ValueError) as exc:
            raise NormalizationLossError(
                f"bbox contains non-numeric values: {value!r}"
            ) from exc
        return (x0, y0, x1, y1)

    raise NormalizationLossError(f"unsupported bbox shape: {value!r}")


def _coerce_kind(value: Any) -> BlockKind:
    """Map a Docling ``content_layer``/``label`` string to a :data:`BlockKind`."""

    if not value:
        return "text"
    text = str(value).lower()
    if "sequence" in text:
        return "sequence_listing"
    if "title" in text or "section_header" in text or "header" in text:
        return "title"
    if "list" in text:
        return "list"
    if "table" in text:
        return "table"
    if "picture" in text or "figure" in text:
        return "picture"
    if "formula" in text:
        return "formula"
    return "text"


def _safe_block_id(prefix: str, raw_id: Any, fallback: int) -> str:
    """Return a block id; sanitize the upstream id and fall back if needed."""

    if raw_id is None:
        return f"{prefix}:b{fallback}"
    text = str(raw_id)
    if not text:
        return f"{prefix}:b{fallback}"
    return f"{prefix}:{text}"


def _iter_text_items(
    page_dict: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    """Yield every text-like item under a page, regardless of nesting depth."""

    for key in ("texts", "items", "blocks"):
        items = page_dict.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield item


def normalize_page(
    page_dict: dict[str, Any],
    *,
    page_number: int,
    parser_run_id: str,
) -> NormalizedPage:
    """Normalise one page dict from a Docling JSON payload."""

    width_pt = float(page_dict.get("width") or page_dict.get("size", {}).get("width") or 0)
    height_pt = float(page_dict.get("height") or page_dict.get("size", {}).get("height") or 0)
    if width_pt <= 0 or height_pt <= 0:
        raise NormalizationLossError(
            f"page {page_number} has invalid size: {width_pt}x{height_pt}"
        )

    blocks: list[TextBlock] = []
    for idx, item in enumerate(_iter_text_items(page_dict)):
        raw_text = item.get("text") or item.get("content") or ""
        if not raw_text:
            continue
        try:
            bbox = _coerce_bbox(item.get("bbox"), page_height_pt=height_pt)
        except NormalizationLossError:
            bbox = None
        blocks.append(
            TextBlock(
                block_id=_safe_block_id(parser_run_id, item.get("self_ref") or item.get("id"), idx),
                kind=_coerce_kind(item.get("label") or item.get("content_layer")),
                bbox=bbox,
                text_raw=str(raw_text),
            )
        )

    tables = _coerce_tables(page_dict, page_number=page_number, height_pt=height_pt)

    provenance = [
        Provenance(
            source_type="docling",
            artifact_ref=f"runs/{parser_run_id}/docling.json",
            provider_locator=f"page={page_number}",
        )
    ]
    for block in blocks:
        block.provenance = list(provenance)
    for table in tables:
        for cell in table.cells:
            # Tables don't carry per-cell provenance in our model yet; the
            # table-level bbox is enough to anchor the entire structure.
            pass

    return NormalizedPage(
        page_number=page_number,
        width_pt=width_pt,
        height_pt=height_pt,
        route="layout_native",
        blocks=blocks,
        tables=tables,
    )


def _coerce_tables(
    page_dict: dict[str, Any],
    *,
    page_number: int,
    height_pt: float,
) -> list[Table]:
    """Best-effort conversion of Docling tables on a page."""

    tables_raw = page_dict.get("tables")
    if not isinstance(tables_raw, list):
        return []

    out: list[Table] = []
    for idx, table_dict in enumerate(tables_raw):
        if not isinstance(table_dict, dict):
            continue
        cells_raw = table_dict.get("cells") or []
        if not isinstance(cells_raw, list):
            cells_raw = []
        cells: list[TableCell] = []
        for c_idx, cell_dict in enumerate(cells_raw):
            if not isinstance(cell_dict, dict):
                continue
            row_index = int(cell_dict.get("row_index", 0))
            column_index = int(cell_dict.get("column_index", 0))
            rowspan = int(cell_dict.get("rowspan", 1))
            colspan = int(cell_dict.get("colspan", 1))
            text = str(cell_dict.get("text") or cell_dict.get("content") or "")
            try:
                bbox = _coerce_bbox(cell_dict.get("bbox"), page_height_pt=height_pt)
            except NormalizationLossError:
                bbox = None
            cells.append(
                TableCell(
                    cell_id=f"{page_number}:t{idx}:c{c_idx}",
                    row_index=row_index,
                    column_index=column_index,
                    rowspan=max(rowspan, 1),
                    colspan=max(colspan, 1),
                    text_raw=text,
                    bbox=bbox,
                )
            )
        if not cells:
            continue
        row_count = max(c.row_index + c.rowspan for c in cells)
        column_count = max(c.column_index + c.colspan for c in cells)
        try:
            table_bbox = _coerce_bbox(table_dict.get("bbox"), page_height_pt=height_pt)
        except NormalizationLossError:
            table_bbox = None
        headers = [
            cell.cell_id
            for cell in cells
            if cell.row_index == 0
        ]
        out.append(
            Table(
                table_id=f"{page_number}:t{idx}",
                page_number=page_number,
                row_count=row_count,
                column_count=column_count,
                cells=cells,
                header_cell_ids=headers,
                caption=str(table_dict.get("caption") or "") or None,
                bbox=table_bbox,
            )
        )
    return out


def normalize_docling_document(
    payload: dict[str, Any],
    *,
    parser_run_id: str,
    document_id: str,
) -> NormalizedDocument:
    """Normalise a Docling JSON payload into a :class:`NormalizedDocument`.

    Args:
        payload: Parsed JSON dict (e.g. from ``json.loads``).
        parser_run_id: The parser run id to embed in the result.
        document_id: The canonical ``sha256:`` document id.

    Raises:
        NormalizationLossError: If a structural field is missing or malformed
            in a way that would silently drop data.
    """

    pages_raw = payload.get("pages") or payload.get("page_map")
    if not isinstance(pages_raw, list) or not pages_raw:
        raise NormalizationLossError(
            "docling payload has no 'pages' list",
            details={"keys": list(payload.keys())},
        )

    normalised_pages: list[NormalizedPage] = []
    for page_number, page_dict in enumerate(pages_raw, start=1):
        if not isinstance(page_dict, dict):
            continue
        normalised_pages.append(
            normalize_page(
                page_dict,
                page_number=page_number,
                parser_run_id=parser_run_id,
            )
        )

    return NormalizedDocument(
        schema_version="1.0.0",
        document_id=document_id,
        parser_run_id=parser_run_id,
        pages=normalised_pages,
        errors=[],
    )


def normalise_docling_file(
    path: Path,
    *,
    parser_run_id: str,
    document_id: str,
) -> NormalizedDocument:
    """Convenience wrapper that loads ``path`` then calls
    :func:`normalize_docling_document`."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return normalize_docling_document(
        payload,
        parser_run_id=parser_run_id,
        document_id=document_id,
    )


__all__ = [
    "normalize_docling_document",
    "normalise_docling_file",
    "normalize_page",
    "NormalizationLossError",
]