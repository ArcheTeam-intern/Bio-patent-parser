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

    Supports two real Docling shapes:

    - ``pages`` is a list of ``{"page": N, "width": w, "height": h, "texts": [...]}``
      (Docling < 2.x exports).
    - ``pages`` is a dict keyed by page number, with top-level ``texts`` /
      ``tables`` / ``groups`` lists (Docling >= 2.0 export).

    Args:
        payload: Parsed JSON dict (e.g. from ``json.loads``).
        parser_run_id: The parser run id to embed in the result.
        document_id: The canonical ``sha256:`` document id.

    Raises:
        NormalizationLossError: If a structural field is missing or malformed
            in a way that would silently drop data.
    """

    pages_raw = payload.get("pages") or payload.get("page_map")
    if pages_raw is None:
        raise NormalizationLossError(
            "docling payload has no 'pages' field",
            details={"keys": list(payload.keys())},
        )

    if isinstance(pages_raw, list):
        normalised_pages = [
            normalize_page(p, page_number=i + 1, parser_run_id=parser_run_id)
            for i, p in enumerate(pages_raw)
            if isinstance(p, dict)
        ]
    elif isinstance(pages_raw, dict):
        # Docling 2.x: per-page size under pages[i].size, and top-level
        # ``texts`` / ``tables`` share a ``prov`` array with ``page_no``.
        normalised_pages = _expand_top_level_pages(payload, parser_run_id=parser_run_id)
    else:
        raise NormalizationLossError(
            "docling 'pages' must be a list or dict",
            details={"type": type(pages_raw).__name__},
        )

    if not normalised_pages:
        raise NormalizationLossError(
            "docling payload has no usable pages",
            details={"page_count_raw": len(pages_raw) if hasattr(pages_raw, '__len__') else 'n/a'},
        )

    return NormalizedDocument(
        schema_version="1.0.0",
        document_id=document_id,
        parser_run_id=parser_run_id,
        pages=normalised_pages,
        errors=[],
    )


def _expand_top_level_pages(
    payload: dict[str, Any],
    *,
    parser_run_id: str,
) -> list[NormalizedPage]:
    """Build per-page :class:`NormalizedPage` for Docling 2.x exports.

    The 2.x format keeps texts/tables at the top level, each with a
    ``prov`` array containing ``page_no``. We regroup by page and use the
    per-page ``size`` (width/height) from ``pages[page_no].size``.
    """

    pages_dict: dict[str, dict[str, Any]] = {}
    for page_no_key, page_dict in payload["pages"].items():
        if not isinstance(page_dict, dict):
            continue
        size = page_dict.get("size") or {}
        try:
            width = float(size.get("width") or 0)
            height = float(size.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0.0, 0.0
        if width <= 0 or height <= 0:
            width, height = 612.0, 792.0
        try:
            page_no = int(page_no_key)
        except (TypeError, ValueError):
            page_no = len(pages_dict) + 1
        pages_dict[str(page_no)] = {
            "page_number": page_no,
            "width": width,
            "height": height,
            "blocks": [],
        }

    def _page_no_from_prov(item: dict[str, Any]) -> int:
        for prov in item.get("prov", []) or []:
            page_no = prov.get("page_no")
            if page_no is None:
                continue
            try:
                return int(page_no)
            except (TypeError, ValueError):
                continue
        return 1

    from bio_pdf.contracts import Provenance, TextBlock, Table  # local to avoid cycles

    provenance = Provenance(
        source_type="docling",
        artifact_ref=f"runs/{parser_run_id}/docling.json",
    )

    for text in payload.get("texts", []) or []:
        if not isinstance(text, dict):
            continue
        page_no = _page_no_from_prov(text)
        target = pages_dict.get(str(page_no))
        if target is None:
            continue
        try:
            bbox = _coerce_bbox(text.get("bbox"), page_height_pt=target["height"])
        except NormalizationLossError:
            bbox = None
        target["blocks"].append(
            TextBlock(
                block_id=_safe_block_id(parser_run_id, text.get("self_ref") or text.get("id"), len(target["blocks"])),
                kind=_coerce_kind(text.get("label") or text.get("content_layer")),
                bbox=bbox,
                text_raw=str(text.get("text") or ""),
                provenance=[provenance],
            )
        )

    tables_out: list[Table] = []
    for table in payload.get("tables", []) or []:
        if not isinstance(table, dict):
            continue
        prov_page = 1
        for prov in table.get("prov", []) or []:
            try:
                prov_page = int(prov.get("page_no", 1))
            except (TypeError, ValueError):
                prov_page = 1
            break
        target = pages_dict.get(str(prov_page))
        if target is None:
            continue
        cells_raw = table.get("data", {}).get("table_cells", []) if isinstance(table.get("data"), dict) else []
        # Some Docling 2.x exports put the rows directly under data; tolerate.
        if not cells_raw and isinstance(table.get("data"), list):
            cells_raw = table["data"]
        cells: list[TableCell] = []
        for c_idx, cell in enumerate(cells_raw or []):
            if not isinstance(cell, dict):
                continue
            row = int(cell.get("start_row_offset_idx", cell.get("row_index", 0)) or 0)
            col = int(cell.get("start_col_offset_idx", cell.get("column_index", 0)) or 0)
            rowspan = max(int(cell.get("row_span", cell.get("rowspan", 1)) or 1), 1)
            colspan = max(int(cell.get("col_span", cell.get("colspan", 1)) or 1), 1)
            text = str(cell.get("text", "") or "")
            try:
                bbox = _coerce_bbox(cell.get("bbox"), page_height_pt=target["height"])
            except NormalizationLossError:
                bbox = None
            cells.append(
                TableCell(
                    cell_id=f"{prov_page}:t:{c_idx}",
                    row_index=row,
                    column_index=col,
                    rowspan=rowspan,
                    colspan=colspan,
                    text_raw=text,
                    bbox=bbox,
                )
            )
        if not cells:
            continue
        try:
            table_bbox = _coerce_bbox(table.get("bbox"), page_height_pt=target["height"])
        except NormalizationLossError:
            table_bbox = None
        table_obj = Table(
            table_id=f"{prov_page}:t:{len(tables_out)}",
            page_number=prov_page,
            row_count=max(c.row_index + c.rowspan for c in cells),
            column_count=max(c.column_index + c.colspan for c in cells),
            cells=cells,
            header_cell_ids=[
                c.cell_id for c in cells if c.row_index == 0
            ],
            caption=str(table.get("caption") or "") or None,
            bbox=table_bbox,
        )
        tables_out.append(table_obj)
        target["blocks"]  # ensure key exists; tables attached after loop

    out: list[NormalizedPage] = []
    for key in sorted(pages_dict.keys(), key=lambda k: int(k)):
        meta = pages_dict[key]
        # Attach any tables that belong to this page.
        page_tables = [t for t in tables_out if t.page_number == meta["page_number"]]
        out.append(
            NormalizedPage(
                page_number=meta["page_number"],
                width_pt=meta["width"],
                height_pt=meta["height"],
                route="layout_native",
                blocks=meta["blocks"],
                tables=page_tables,
            )
        )
    return out


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