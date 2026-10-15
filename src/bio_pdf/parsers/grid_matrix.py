"""Deterministic grid reconstruction for parser-missed call matrices.

Some result tables — antibiotic-susceptibility heat matrices above all —
have no drawn grid lines, colour-coded cells and rotated headers, so
structure parsers (Docling's TableFormer included) do not emit a table
object for them. The TEXT of every cell is present in the native layer,
just as loose positioned blocks: the R/S/I letters line up on a regular
row/column lattice, drug names sit left of each row and strain/isolate
labels under each column.

This module rebuilds the TABLE STRUCTURE purely from that geometry — no
OCR guessing, no invented content. A rebuilt table is explicitly marked
``source="geometry_reconstructed"`` and carries a structure issue, so
downstream review treats it as a candidate structure needing human
confirmation, never as parser truth.

Guardrails (all deliberate):
- only single-letter call cells (R/S/I/SDD) seed the grid; anything
  inside a parser-produced table region is excluded (no double counting);
- the lattice must be regular (row/column spacing within tolerance) and
  substantial (min cells/rows/cols) or nothing is reconstructed;
- a row/column without a readable label keeps its call cells out of the
  header mapping — the gap is reported in structure_issues instead of a
  guessed label.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import TableCell, Table

#: Cell texts that are susceptibility calls (same vocabulary as the
#: classifier/extractor).
_CALL_RE = re.compile(r"^\s*(S|R|I|SDD)\s*$")
_CAPTION_RE = re.compile(
    r"\bTABLE\b.*\b(sensitiv|susceptib|antibiot|antibiogram|microbial|minimal)",
    re.IGNORECASE,
)

#: Minimum evidence before a lattice is believed at all.
_MIN_CELLS = 10
_MIN_ROWS = 3
_MIN_COLS = 2
#: Cell-centre separation used to merge cells into a row/column band. Set
#: from the median cell size at runtime: same-column cells share a centre
#: (native glyphs land on identical coordinates), while two visually
#: adjacent columns never overlap. A fixed tolerance breaks on dense
#: matrices where the column pitch is barely larger than a cell.
_TOLERANCE_FLOOR = 4.0
#: Upper bound on the spacing between adjacent row/column bands; beyond it
#: the cells are scatter on the page, not one lattice.
_MAX_PITCH = 160.0


def _bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = block.get("bbox")
    if not raw or len(raw) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in raw)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _cx(b: tuple[float, float, float, float]) -> float:
    return (b[0] + b[2]) / 2


def _cy(b: tuple[float, float, float, float]) -> float:
    return (b[1] + b[3]) / 2


def _cluster(values: list[float], tolerance: float) -> list[list[int]]:
    """Indices of values grouped into bands (sorted by value)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    bands: list[list[int]] = []
    for idx in order:
        if bands and abs(values[idx] - values[bands[-1][-1]]) <= tolerance:
            bands[-1].append(idx)
        else:
            bands.append([idx])
    return bands


def _inside(cell: tuple[float, float, float, float], region: tuple[float, float, float, float]) -> bool:
    return (
        cell[0] >= region[0] - 2
        and cell[1] >= region[1] - 2
        and cell[2] <= region[2] + 2
        and cell[3] <= region[3] + 2
    )


def reconstruct_call_matrices(
    blocks: list[dict[str, Any]],
    *,
    page_number: int,
    occupied_regions: list[tuple[float, float, float, float]] | None = None,
) -> list[Table]:
    """Rebuild grid tables from positioned call-cell blocks of one page.

    ``blocks`` are normalized page blocks (dict form: block_id, kind,
    bbox, text_raw). ``occupied_regions`` are bboxes already covered by
    parser-detected tables — cells inside them are never re-used.
    """

    occupied = occupied_regions or []
    calls: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]] = []
    others: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]] = []
    for i, b in enumerate(blocks):
        bbox = _bbox(b)
        text = str(b.get("text_raw") or "")
        if bbox is None or not text.strip():
            continue
        if _CALL_RE.match(text) and not any(_inside(bbox, r) for r in occupied):
            calls.append((i, b, bbox))
        else:
            others.append((i, b, bbox))

    if len(calls) < _MIN_CELLS:
        return []

    widths = sorted(c[2][2] - c[2][0] for c in calls)
    heights = sorted(c[2][3] - c[2][1] for c in calls)
    med_w = widths[len(widths) // 2]
    med_h = heights[len(heights) // 2]
    # Merge cells whose centres are within one cell-size: same-column
    # glyphs coincide; visually adjacent columns never do. A fixed span
    # would fuse dense columns (real paper: 11.7pt pitch, 6.5pt cells).
    row_bands = _cluster([_cy(c[2]) for c in calls], max(_TOLERANCE_FLOOR, med_h))
    col_bands = _cluster([_cx(c[2]) for c in calls], max(_TOLERANCE_FLOOR, med_w))
    if len(row_bands) < _MIN_ROWS or len(col_bands) < _MIN_COLS:
        return []
    row_centers = [sum(_cy(calls[j][2]) for j in band) / len(band) for band in row_bands]
    col_centers = [sum(_cx(calls[j][2]) for j in band) / len(band) for band in col_bands]

    # Grid assignment.
    grid: dict[tuple[int, int], tuple[int, dict[str, Any], tuple[float, float, float, float]]] = {}
    for band_r, rband in enumerate(row_bands):
        for j in rband:
            band_c = min(
                range(len(col_bands)),
                key=lambda k: abs(_cx(calls[j][2]) - col_centers[k]),
            )
            grid[(band_r, band_c)] = calls[j]
    if len(grid) < _MIN_CELLS:
        return []
    # Keep only the coherent sub-lattice: drop row/column bands that carry a
    # single call (isolated R/S glyphs from captions or a second table), so a
    # noisy page yields a clean matrix instead of being rejected whole. The
    # dropped cells are NOT fabricated into the table — they stay ordinary
    # text blocks and the exclusion is reported on the table.
    excluded = 0
    while True:
        rows_with_calls = {r for (r, _c) in grid}
        cols_with_calls = {c for (_r, c) in grid}
        thin_rows = {r for r in rows_with_calls
                     if sum(1 for (rr, _c) in grid if rr == r) < 2}
        thin_cols = {c for c in cols_with_calls
                     if sum(1 for (_r, cc) in grid if cc == c) < 2}
        if not thin_rows and not thin_cols:
            break
        keep = {
            k: v for k, v in grid.items()
            if k[0] not in thin_rows and k[1] not in thin_cols
        }
        excluded += len(grid) - len(keep)
        grid = keep
        if not grid:
            return []
    rows_with_calls = {r for (r, _c) in grid}
    cols_with_calls = {c for (_r, c) in grid}
    if len(rows_with_calls) < _MIN_ROWS or len(cols_with_calls) < _MIN_COLS or len(grid) < _MIN_CELLS:
        return []
    kept_rows = sorted(rows_with_calls)
    kept_cols = sorted(cols_with_calls)
    row_seq = [row_centers[b] for b in kept_rows]
    col_seq = [col_centers[b] for b in kept_cols]
    # The kept bands must still be a regular lattice (adjacent band pitch
    # bounded): guards against cells that merely happened to pair up across
    # unrelated page regions.
    if any(row_seq[i + 1] - row_seq[i] > _MAX_PITCH for i in range(len(row_seq) - 1)) or any(
        col_seq[i + 1] - col_seq[i] > _MAX_PITCH for i in range(len(col_seq) - 1)
    ):
        return []
    row_align = max(med_h, min(
        (row_seq[i + 1] - row_seq[i] for i in range(len(row_seq) - 1)), default=med_h
    ) / 2)
    col_align = max(med_w, min(
        (col_seq[i + 1] - col_seq[i] for i in range(len(col_seq) - 1)), default=med_w
    ) / 2)

    grid_x0 = min(c[2][0] for c in grid.values())
    grid_top = min(c[2][1] for c in grid.values())
    grid_bottom = max(c[2][3] for c in grid.values())

    def row_label(band_r: int) -> tuple[dict[str, Any], tuple[float, float, float, float]] | None:
        center = row_centers[band_r]
        candidates = [
            (b, bbox)
            for _i, b, bbox in others
            if abs(_cy(bbox) - center) <= row_align
            and bbox[2] < grid_x0 - 2
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: grid_x0 - p[1][2])

    def col_label(band_c: int) -> tuple[dict[str, Any], tuple[float, float, float, float]] | None:
        center = col_centers[band_c]
        candidates = [
            (b, bbox)
            for _i, b, bbox in others
            if abs(_cx(bbox) - center) <= col_align
            and (bbox[1] > grid_bottom + 2 or bbox[3] < grid_top - 2)
        ]
        if not candidates:
            return None
        below = [p for p in candidates if p[1][1] > grid_bottom]
        pool = below or candidates
        return min(pool, key=lambda p: min(abs(p[1][1] - grid_bottom), abs(grid_top - p[1][3])))

    caption_block = next(
        (
            (b, bbox)
            for _i, b, bbox in others
            if _CAPTION_RE.search(str(b.get("text_raw") or ""))
        ),
        None,
    )

    cells: list[TableCell] = []
    header_ids: list[str] = []
    issues: list[str] = [
        "GEOMETRY_RECONSTRUCTED: table structure rebuilt from call-cell positions; review required"
    ]
    if excluded:
        issues.append(
            f"OFF_LATTICE_CALLS_EXCLUDED count={excluded}: single-cell rows/columns "
            "were treated as noise and left as text, not table cells"
        )
    for band_c in sorted(cols_with_calls):
        lab = col_label(band_c)
        if lab is None:
            issues.append(f"COL_LABEL_MISSING column={band_c}")
            continue
        block, bbox = lab
        cid = f"{page_number}:grid:r0c{band_c + 1}"
        cells.append(
            TableCell(
                cell_id=cid,
                row_index=0,
                column_index=band_c + 1,
                text_raw=str(block["text_raw"]).strip(),
                header_role="column",
                bbox=bbox,
                source_refs=[str(block.get("block_id") or cid)],
            )
        )
        header_ids.append(cid)
    for band_r in sorted(rows_with_calls):
        lab = row_label(band_r)
        if lab is None:
            issues.append(f"ROW_LABEL_MISSING row={band_r}")
        else:
            block, bbox = lab
            cid = f"{page_number}:grid:r{band_r + 1}c0"
            cells.append(
                TableCell(
                    cell_id=cid,
                    row_index=band_r + 1,
                    column_index=0,
                    text_raw=str(block["text_raw"]).strip(),
                    header_role="row",
                    bbox=bbox,
                    source_refs=[str(block.get("block_id") or cid)],
                )
            )
            header_ids.append(cid)
        for band_c in sorted(cols_with_calls):
            entry = grid.get((band_r, band_c))
            if entry is None:
                continue  # present-but-blank cells stay absent, never invented
            _i, block, bbox = entry
            cells.append(
                TableCell(
                    cell_id=f"{page_number}:grid:r{band_r + 1}c{band_c + 1}",
                    row_index=band_r + 1,
                    column_index=band_c + 1,
                    text_raw=str(block["text_raw"]).strip(),
                    header_role="data",
                    bbox=bbox,
                    source_refs=[str(block.get("block_id") or f"{page_number}:grid:r{band_r + 1}c{band_c + 1}")],
                )
            )

    table_id = f"{page_number}:grid"
    return [
        Table(
            table_id=table_id,
            page_number=page_number,
            row_count=max(r for r, _c in grid) + 2,
            column_count=max(c for _r, c in grid) + 2,
            cells=cells,
            header_cell_ids=header_ids,
            caption=str(caption_block[0]["text_raw"]).strip() if caption_block else None,
            caption_refs=[str(caption_block[0].get("block_id"))] if caption_block else [],
            bbox=(
                min(c.bbox[0] for c in cells if c.bbox),
                min(c.bbox[1] for c in cells if c.bbox),
                max(c.bbox[2] for c in cells if c.bbox),
                max(c.bbox[3] for c in cells if c.bbox),
            ),
            structure_issues=issues,
            source="geometry_reconstructed",
        )
    ]


__all__ = ["reconstruct_call_matrices"]
