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
    Figure,
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
    """Convert a Docling bbox to the contract format (top-left origin).

    Docling bboxes may be dicts with ``l``/``t``/``r``/``b`` keys plus an
    optional ``coord_origin`` (``TOPLEFT`` or ``BOTTOMLEFT``), or a plain
    4-list ``(l, t, r, b)`` in top-left order. ``BOTTOMLEFT`` boxes are
    flipped into the contract's top-left frame; the flip needs the page
    height, which is why it happens here and not at the call sites.
    """

    if value is None:
        return None

    if isinstance(value, dict):
        try:
            l = float(value["l"])
            t = float(value["t"])
            r = float(value["r"])
            b = float(value["b"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NormalizationLossError(
                f"bbox dict missing required keys: {value!r}"
            ) from exc
        origin = str(value.get("coord_origin", "TOPLEFT")).upper()
        if origin == "BOTTOMLEFT":
            t, b = page_height_pt - b, page_height_pt - t
        return (l, min(t, b), r, max(t, b))

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
        return (x0, min(y0, y1), x1, max(y0, y1))

    raise NormalizationLossError(f"unsupported bbox shape: {value!r}")


def _bbox_from_prov(
    item: dict[str, Any],
    *,
    page_height_pt: float,
) -> tuple[BoundingBox | None, list[str]]:
    """Read the bbox of every provenance span on an item.

    Real Docling 2.x exports keep coordinates in ``prov[].bbox``, not on the
    item itself. Returns ``(first_bbox, locators)``: the first resolvable
    bbox anchors the block/cell, and every span — including pages beyond the
    first — is encoded in ``locators`` as ``page=N;bbox=l,t,r,b`` so
    multi-page/multi-box evidence survives normalisation.
    """

    locators: list[str] = []
    first_bbox: BoundingBox | None = None

    spans: list[dict[str, Any]] = []
    direct = item.get("bbox")
    if direct is not None:
        # Legacy/flat payloads carry the box on the item itself.
        try:
            first_bbox = _coerce_bbox(direct, page_height_pt=page_height_pt)
        except NormalizationLossError:
            first_bbox = None

    prov_raw = item.get("prov")
    if isinstance(prov_raw, list):
        spans = [p for p in prov_raw if isinstance(p, dict)]

    for span in spans:
        page_no = span.get("page_no")
        try:
            page_no_int = int(page_no) if page_no is not None else None
        except (TypeError, ValueError):
            page_no_int = None
        bbox = None
        if span.get("bbox") is not None:
            try:
                bbox = _coerce_bbox(span["bbox"], page_height_pt=page_height_pt)
            except NormalizationLossError:
                bbox = None
        if first_bbox is None and bbox is not None:
            first_bbox = bbox
        if bbox is not None:
            locators.append(
                f"page={page_no_int if page_no_int is not None else '?'}"
                f";bbox={bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f}"
            )
        elif page_no_int is not None:
            locators.append(f"page={page_no_int}")

    return first_bbox, locators


def _coerce_semantic_role(label: Any, kind: BlockKind) -> str:
    """Map a Docling label to our SemanticRole (plan T05/T07).

    Roles gate extraction: authors, references, headers/footers and captions
    are never scanned for sequences. Uncertain labels stay "unknown" —
    never guessed from name blacklists.
    """

    text = str(label or "").lower()
    if "page_header" in text or text in ("header", "running_head"):
        return "header"
    if "page_footer" in text or text == "footer":
        return "footer"
    if "caption" in text:
        return "caption"
    if "reference" in text or "bibliograph" in text or "citation" in text:
        return "reference"
    if "author" in text or "affiliation" in text:
        return "author"
    if "footnote" in text:
        return "footnote"
    if "section_header" in text or "title" in text or kind == "title":
        return "title"
    if "sequence" in text and "listing" in text:
        return "sequence_listing"
    if text in ("text", "paragraph", "body", "", "None"):
        return "body"
    return "unknown"


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


def _classify_figure(label: str, caption: str | None) -> str:
    """Bucket a visual region for display grouping.

    Docling only labels pictures ``chart`` or ``picture``; the caption often
    names the plot type ("growth curve", "bar chart"), so a cheap keyword
    pass over both gives ``chart``/``plot``/``diagram``/``photo`` with a
    safe ``picture`` fallback. Classification is cosmetic — the crop is the
    real product either way.
    """

    text = f"{label} {caption or ''}".lower()
    # Specific caption words win over Docling's generic ``chart`` label: a
    # "Growth curve" figure is a line plot even when the label says chart.
    if any(w in text for w in ("curve", "growth", "survival", "kinetic", "time-course", "time course", "plot", "graph")):
        return "plot"
    if any(w in text for w in ("chart", "bar", "histogram", "column plot")):
        return "chart"
    if any(w in text for w in ("diagram", "scheme", "pathway", "structure", "model")):
        return "diagram"
    if any(w in text for w in ("micrograph", "photo", "stain", "image of", "colony")):
        return "photo"
    return "picture"


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
        # NonEmptyStr strips whitespace, so a whitespace-only group would
        # fail validation — and it carries no evidence anyway. Skip it
        # rather than fabricate content (same rule as the top-level path).
        if not str(raw_text).strip():
            continue
        # Coordinates: item-level bbox first, then every prov span.
        try:
            bbox = _coerce_bbox(item.get("bbox"), page_height_pt=height_pt)
        except NormalizationLossError:
            bbox = None
        prov_bbox, locators = _bbox_from_prov(item, page_height_pt=height_pt)
        if bbox is None:
            bbox = prov_bbox
        block_kind = _coerce_kind(item.get("label") or item.get("content_layer"))
        blocks.append(
            TextBlock(
                block_id=_safe_block_id(parser_run_id, item.get("self_ref") or item.get("id"), idx),
                kind=block_kind,
                semantic_role=_coerce_semantic_role(  # type: ignore[arg-type]
                    item.get("label") or item.get("content_layer"), block_kind
                ),
                bbox=bbox,
                text_raw=str(raw_text),
                provenance=[
                    Provenance(
                        source_type="docling",
                        artifact_ref=f"runs/{parser_run_id}/docling.json",
                        provider_locator=locator,
                    )
                    for locator in locators
                ]
                or [
                    Provenance(
                        source_type="docling",
                        artifact_ref=f"runs/{parser_run_id}/docling.json",
                        provider_locator=f"page={page_number}",
                    )
                ],
            )
        )

    tables = _coerce_tables(page_dict, page_number=page_number, height_pt=height_pt)

    page_text_ref = {
        str(it.get("self_ref")): str(it.get("text") or "")
        for it in _iter_text_items(page_dict)
        if isinstance(it, dict) and it.get("self_ref")
    }
    figures: list[Figure] = []
    for pic in page_dict.get("pictures", []) or []:
        fig = _build_figure(
            pic,
            parser_run_id=parser_run_id,
            page_number=page_number,
            page_height_pt=height_pt,
            text_ref=page_text_ref,
            index=len(figures),
        )
        if fig is not None:
            figures.append(fig)

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
        figures=figures,
    )


def _build_figure(
    pic: Any,
    *,
    parser_run_id: str,
    page_number: int,
    page_height_pt: float,
    text_ref: dict[str, str],
    index: int,
) -> Figure | None:
    """One Docling picture item → :class:`Figure` (None if not crop-able)."""

    if not isinstance(pic, dict):
        return None
    bbox, locators = _bbox_from_prov(pic, page_height_pt=page_height_pt)
    if bbox is None:
        return None  # a region we cannot crop is not exportable
    caption_parts: list[str] = []
    caption_ids: list[str] = []
    for cap in pic.get("captions", []) or []:
        ref = (
            str(cap.get("$ref") or cap.get("ref") or cap)
            if isinstance(cap, dict)
            else str(cap)
        )
        if not ref:
            continue
        caption_ids.append(ref)
        content = text_ref.get(ref, "").strip()
        if content:
            caption_parts.append(content)
    caption = " ".join(caption_parts)[:400] or None
    provenance = [
        Provenance(
            source_type="docling",
            artifact_ref=f"runs/{parser_run_id}/docling.json",
            provider_locator=loc,
        )
        for loc in locators
    ] or [
        Provenance(
            source_type="docling",
            artifact_ref=f"runs/{parser_run_id}/docling.json",
            provider_locator=f"page={page_number}",
        )
    ]
    return Figure(
        figure_id=f"{page_number}:fig{index}",
        page_number=page_number,
        bbox=bbox,
        kind=_classify_figure(str(pic.get("label") or ""), caption),
        caption=caption,
        caption_refs=caption_ids,
        provenance=provenance,
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
        if not cells_raw:
            # Docling 2.x keeps cells under data.table_cells with different
            # row/col key names; accept that shape here too so a projected
            # table never silently disappears.
            data = table_dict.get("data")
            if isinstance(data, dict) and isinstance(data.get("table_cells"), list):
                cells_raw = data["table_cells"]
            elif isinstance(data, list):
                cells_raw = data
        cells: list[TableCell] = []
        for c_idx, cell_dict in enumerate(cells_raw):
            if not isinstance(cell_dict, dict):
                continue
            row_index = int(cell_dict.get("row_index", cell_dict.get("start_row_offset_idx", 0)) or 0)
            column_index = int(cell_dict.get("column_index", cell_dict.get("start_col_offset_idx", 0)) or 0)
            rowspan = max(int(cell_dict.get("rowspan", cell_dict.get("row_span", 1)) or 1), 1)
            colspan = max(int(cell_dict.get("colspan", cell_dict.get("col_span", 1)) or 1), 1)
            text = str(cell_dict.get("text") or cell_dict.get("content") or "")
            try:
                bbox = _coerce_bbox(cell_dict.get("bbox"), page_height_pt=height_pt)
            except NormalizationLossError:
                bbox = None
            if bbox is None:
                prov_bbox, _locators = _bbox_from_prov(cell_dict, page_height_pt=height_pt)
                bbox = prov_bbox
            cells.append(
                TableCell(
                    cell_id=f"{page_number}:t{idx}:c{c_idx}",
                    row_index=row_index,
                    column_index=column_index,
                    rowspan=rowspan,
                    colspan=colspan,
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
        if table_bbox is None:
            prov_bbox, _locators = _bbox_from_prov(table_dict, page_height_pt=height_pt)
            table_bbox = prov_bbox
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


def _structure_issues(cells: list[TableCell]) -> list[str]:
    """Detect span overlap / ragged structure via an occupancy grid.

    A table whose merged cells overlap cannot be trusted for header chains;
    the issue is recorded so downstream validation never marks a broken
    matrix as healthy (plan T05).
    """

    issues: list[str] = []
    occupied: dict[tuple[int, int], str] = {}
    for cell in cells:
        for r in range(cell.row_index, cell.row_index + cell.rowspan):
            for c in range(cell.column_index, cell.column_index + cell.colspan):
                if (r, c) in occupied:
                    issues.append(
                        f"TABLE_GRID_OVERLAP at r{r}c{c}: {occupied[(r, c)]} vs {cell.cell_id}"
                    )
                else:
                    occupied[(r, c)] = cell.cell_id
    if len(issues) > 4:
        issues = issues[:4] + [f"... {len(issues) - 4} more overlaps"]
    return issues


def _validate_id_uniqueness(document: NormalizedDocument) -> list[str]:
    """Same id with different content/location is a hard normalization error."""

    from bio_pdf.errors import WorkflowError

    seen: dict[str, tuple] = {}
    errors: list[str] = []
    for page in document.pages:
        for block in page.blocks:
            key = block.block_id
            payload = (page.page_number, block.bbox, block.text_raw)
            if key in seen and seen[key] != payload:
                errors.append(f"NORMALIZATION_ID_COLLISION block {key}")
            seen[key] = payload
        for table in page.tables:
            seen.setdefault(table.table_id, (page.page_number,))
            for cell in table.cells:
                key = cell.cell_id
                payload = (table.table_id, cell.row_index, cell.column_index, cell.text_raw)
                if key in seen and seen[key] != payload:
                    errors.append(f"NORMALIZATION_ID_COLLISION cell {key}")
                seen[key] = payload
    if errors:
        raise WorkflowError(
            "; ".join(errors[:5]),
            details={"code": "NORMALIZATION_ID_COLLISION", "count": len(errors)},
        )
    return []


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

    geometry_warnings: list[str] = []
    if isinstance(pages_raw, list):
        normalised_pages = []
        for i, p in enumerate(pages_raw):
            if not isinstance(p, dict):
                continue
            # Honour the payload's own page number when present; array order
            # is only a fallback. Sparse page sets (e.g. [6]) must keep the
            # original source page number.
            page_number = i + 1
            raw_page_no = p.get("page")
            if raw_page_no is not None:
                try:
                    page_number = int(raw_page_no)
                except (TypeError, ValueError):
                    pass
            normalised_pages.append(
                normalize_page(p, page_number=page_number, parser_run_id=parser_run_id)
            )
        normalised_pages.sort(key=lambda page: page.page_number)
    elif isinstance(pages_raw, dict):
        # Docling 2.x: per-page size under pages[i].size, and top-level
        # ``texts`` / ``tables`` share a ``prov`` array with ``page_no``.
        normalised_pages, geometry_warnings = _expand_top_level_pages(
            payload, parser_run_id=parser_run_id
        )
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

    document = NormalizedDocument(
        schema_version="1.1.0",
        document_id=document_id,
        parser_run_id=parser_run_id,
        pages=normalised_pages,
        errors=list(geometry_warnings),
    )
    _validate_id_uniqueness(document)
    return document


def _expand_top_level_pages(
    payload: dict[str, Any],
    *,
    parser_run_id: str,
) -> tuple[list[NormalizedPage], list[str]]:
    """Build per-page :class:`NormalizedPage` for Docling 2.x exports.

    The 2.x format keeps texts/tables at the top level, each with a
    ``prov`` array containing ``page_no``. We regroup by page and use the
    per-page ``size`` (width/height) from ``pages[page_no].size``.
    """

    pages_dict: dict[str, dict[str, Any]] = {}
    geometry_warnings: list[str] = []
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
            # Never invent 612x792: a fabricated size silently corrupts every
            # bbox. Skip the page and record why — the coverage check then
            # fails loudly instead of passing with wrong geometry.
            geometry_warnings.append(
                f"page {page_no_key}: missing page size; page skipped (geometry unknown)"
            )
            continue
        try:
            page_no = int(page_no_key)
        except (TypeError, ValueError):
            page_no = len(pages_dict) + 1
            geometry_warnings.append(
                f"page key {page_no_key!r} is not an integer; order position used"
            )
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

    for text in payload.get("texts", []) or []:
        if not isinstance(text, dict):
            continue
        raw_text = str(text.get("text") or "")
        if not raw_text.strip():
            # Docling emits empty text groups (picture captions without OCR
            # text, whitespace groups). A block without text carries no
            # evidence — skip it instead of fabricating a placeholder that
            # would violate the TextBlock contract (real crash on the
            # OCR'd EP patent: every layout attempt died here).
            continue
        page_no = _page_no_from_prov(text)
        target = pages_dict.get(str(page_no))
        if target is None:
            continue
        bbox, locators = _bbox_from_prov(text, page_height_pt=target["height"])
        provenance = [
            Provenance(
                source_type="docling",
                artifact_ref=f"runs/{parser_run_id}/docling.json",
                provider_locator=locator,
            )
            for locator in locators
        ] or [
            Provenance(
                source_type="docling",
                artifact_ref=f"runs/{parser_run_id}/docling.json",
                provider_locator=f"page={page_no}",
            )
        ]
        block_kind = _coerce_kind(text.get("label") or text.get("content_layer"))
        target["blocks"].append(
            TextBlock(
                block_id=_safe_block_id(parser_run_id, text.get("self_ref") or text.get("id"), len(target["blocks"])),
                kind=block_kind,
                semantic_role=_coerce_semantic_role(  # type: ignore[arg-type]
                    text.get("label") or text.get("content_layer"), block_kind
                ),
                bbox=bbox,
                text_raw=raw_text,
                provenance=provenance,
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
        for c_idx, cell in enumerate(cells_raw):
            if not isinstance(cell, dict):
                continue
            row = int(cell.get("start_row_offset_idx", cell.get("row_index", 0)) or 0)
            col = int(cell.get("start_col_offset_idx", cell.get("column_index", 0)) or 0)
            rowspan = max(int(cell.get("row_span", cell.get("rowspan", 1)) or 1), 1)
            colspan = max(int(cell.get("col_span", cell.get("colspan", 1)) or 1), 1)
            text = str(cell.get("text", "") or "")
            # Docling marks header cells explicitly; honour the real flags
            # instead of assuming row 0 is all headers (plan T05).
            is_col_header = bool(cell.get("column_header"))
            is_row_header = bool(cell.get("row_header"))
            if is_col_header and is_row_header:
                header_role = "both"
            elif is_col_header:
                header_role = "column"
            elif is_row_header:
                header_role = "row"
            else:
                header_role = "data"
            # Cell coordinates live in the cell's own prov spans.
            cell_bbox, _cell_locators = _bbox_from_prov(cell, page_height_pt=target["height"])
            cells.append(
                TableCell(
                    # Include the table index: two tables on one page must
                    # not produce colliding cell ids (plan T05).
                    cell_id=f"{prov_page}:t{len(tables_out)}:c{c_idx}",
                    row_index=row,
                    column_index=col,
                    rowspan=rowspan,
                    colspan=colspan,
                    text_raw=text,
                    header_role=header_role,
                    bbox=cell_bbox,
                )
            )
        if not cells:
            continue
        table_bbox, _table_locators = _bbox_from_prov(table, page_height_pt=target["height"])
        issues = _structure_issues(cells)
        header_ids = [c.cell_id for c in cells if c.header_role in ("column", "row", "both")]
        if not header_ids:
            # No explicit flags (older Docling exports): fall back to row 0.
            header_ids = [c.cell_id for c in cells if c.row_index == 0]
        caption_raw = table.get("caption")
        if isinstance(caption_raw, list):
            caption_raw = " ".join(str(x) for x in caption_raw)
        table_obj = Table(
            table_id=f"{prov_page}:t{len(tables_out)}",
            page_number=prov_page,
            row_count=max(c.row_index + c.rowspan for c in cells),
            column_count=max(c.column_index + c.colspan for c in cells),
            cells=cells,
            header_cell_ids=header_ids,
            caption=str(caption_raw or "") or None,
            bbox=table_bbox,
            structure_issues=issues,
        )
        tables_out.append(table_obj)

    # --- figures (charts / plots / diagrams) ------------------------------
    # Docling reports every visual region as a ``picture`` with a bbox and a
    # caption $ref. We keep them so result charts (growth curves, bar/line
    # plots) can be exported as standalone images and listed for review.
    text_ref = {
        str(t.get("self_ref")): str(t.get("text") or "")
        for t in payload.get("texts", []) or []
        if isinstance(t, dict) and t.get("self_ref")
    }
    figures_out: list[Figure] = []
    for pic in payload.get("pictures", []) or payload.get("figures", []) or []:
        if not isinstance(pic, dict):
            continue
        prov_page = 1
        for prov in pic.get("prov", []) or []:
            if isinstance(prov, dict) and prov.get("page_no") is not None:
                try:
                    prov_page = int(prov["page_no"])
                except (TypeError, ValueError):
                    prov_page = 1
                break
        target = pages_dict.get(str(prov_page))
        if target is None:
            continue
        fig = _build_figure(
            pic,
            parser_run_id=parser_run_id,
            page_number=prov_page,
            page_height_pt=target["height"],
            text_ref=text_ref,
            index=len(figures_out),
        )
        if fig is not None:
            figures_out.append(fig)

    out: list[NormalizedPage] = []
    for key in sorted(pages_dict.keys(), key=lambda k: int(k)):
        meta = pages_dict[key]
        # Attach any tables that belong to this page.
        page_tables = [t for t in tables_out if t.page_number == meta["page_number"]]
        page_figures = [f for f in figures_out if f.page_number == meta["page_number"]]
        out.append(
            NormalizedPage(
                page_number=meta["page_number"],
                width_pt=meta["width"],
                height_pt=meta["height"],
                route="layout_native",
                blocks=meta["blocks"],
                tables=page_tables,
                figures=page_figures,
            )
        )
    return out, geometry_warnings


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