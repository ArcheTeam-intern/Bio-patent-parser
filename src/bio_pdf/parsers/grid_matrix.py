"""Deterministic grid reconstruction for parser-missed call matrices.

Some result tables — antibiotic-susceptibility heat matrices above all —
have no drawn grid lines, colour-coded cells and rotated headers, so
structure parsers (Docling's TableFormer included) do not emit a table
object for them. The TEXT of every cell is present in the glyph layer:
the R/S/I letters sit on a regular lattice, drug names line up left of
each row and strain/isolate labels under each column.

This module rebuilds the TABLE STRUCTURE purely from that geometry — no
OCR guessing, no invented content. A rebuilt table is explicitly marked
``source="geometry_reconstructed"`` and carries structure issues, so
downstream review treats it as a candidate structure needing human
confirmation, never as parser truth.

The rebuild follows two evidence sources, because a real page showed why
one is not enough (the paper's TABLE 2):

- **Seeds = individual glyphs.** The native glyph layer keeps every
  letter as its own line; the layout channel may merge adjacent letters
  into one "SSS"-style run. When native blocks are provided they are the
  seed source (with the fused block id kept for provenance when the same
  glyph exists there).
- **Rows = the label skeleton.** Drug-name labels in the left margin
  define ALL rows — including rows whose cells are all empty (not
  tested) — so labels never shift onto a neighbour's cells. Call cells
  snap to the nearest label row within a tolerance below half the label
  pitch. Coherent groups of unattached calls (>=2 cells on one band)
  become extra rows flagged ``ROW_LABEL_MISSING``; isolated stray glyphs
  are excluded and counted, never silently dropped.

Guardrails (all deliberate):
- anything inside a parser-produced table region is excluded (no double
  counting); a lattice must be regular (band pitch bounded) and
  substantial (min cells/rows/cols) or nothing is reconstructed;
- duplicate assignments into one (row, column) cell are counted as
  excluded, never overwritten silently.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import TableCell, Table

#: Cell texts that are susceptibility calls (same vocabulary as the
#: classifier/extractor).
_CALL_RE = re.compile(r"^\s*(S|R|I|SDD)\s*$")
#: A merged call run ("SSS", "S S") — layout-side artifact, never a seed.
_CALL_RUN_RE = re.compile(r"^[RSI][RSI\s]*$")
_CAPTION_RE = re.compile(
    r"\bTABLE\b.*\b(sensitiv|susceptib|antibiot|antibiogram|microbial|minimal)",
    re.IGNORECASE,
)
#: A susceptibility legend explains the category letters and colours with
#: the CLSI reading rules, e.g. "S (green), sensitive; R (orange),
#: resistant; empty cells were not tested". A block is treated as legend
#: when it hits at least two of these patterns.
_LEGEND_PATTERNS = (
    re.compile(r"\bS\b.{0,12}(green|sensitive|susceptible)", re.IGNORECASE),
    re.compile(r"\bR\b.{0,12}(orange|red|resistant)", re.IGNORECASE),
    re.compile(r"\bI\b.{0,12}(yellow|intermediate|partially)", re.IGNORECASE),
    re.compile(r"(empty|blank) cells.{0,30}not tested", re.IGNORECASE),
    re.compile(r"not tested", re.IGNORECASE),
    re.compile(r"(Kirby|Bauer|disk diffusion|CLSI|EUCAST)", re.IGNORECASE),
)

#: Minimum evidence before a lattice is believed at all.
_MIN_CELLS = 10
_MIN_ROWS = 3
_MIN_COLS = 2
#: Floor for cell-centre clustering. The actual tolerance follows the
#: median cell size at runtime: same-column glyphs share a centre, two
#: visually adjacent columns never do (real paper: 11.7pt pitch, 6.5pt
#: cells — a fixed span fused adjacent columns).
_TOLERANCE_FLOOR = 4.0
#: Upper bound on the spacing between adjacent column bands; beyond it
#: the cells are scatter on the page, not one lattice.
_MAX_PITCH = 160.0
#: How far from a row label a call glyph may sit (fraction of the
#: tightest label pitch — comfortably below half, so ambiguity is an
#: exclusion, not a guess).
_ROW_ATTACH_FRACTION = 0.45


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


def _inside(
    cell: tuple[float, float, float, float], region: tuple[float, float, float, float]
) -> bool:
    return (
        cell[0] >= region[0] - 2
        and cell[1] >= region[1] - 2
        and cell[2] <= region[2] + 2
        and cell[3] <= region[3] + 2
    )


def _collect_seeds(
    blocks: list[dict[str, Any]],
    fused_calls: list[tuple[dict[str, Any], tuple[float, float, float, float]]],
    occupied: list[tuple[float, float, float, float]],
    *,
    from_native: bool,
) -> tuple[list[tuple[dict[str, Any], tuple[float, float, float, float]]], int]:
    """Individual call glyphs, with overprinted duplicates collapsed.

    The same letter can exist twice at (nearly) the same position (bold
    overprint in the text layer); counting both would fabricate a 2-cell
    "row" that then poisons the column lattice. Two glyphs whose centres
    differ by <=2pt and whose text matches are one physical letter.
    """

    candidates: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for b in blocks:
        bbox = _bbox(b)
        text = str(b.get("text_raw") or "")
        if bbox is None or not _CALL_RE.match(text):
            continue
        if any(_inside(bbox, r) for r in occupied):
            continue
        block = b
        if from_native:
            # keep the fused block id when the same glyph exists there, so
            # evidence/provenance resolves against the document consumers see
            for fused, fbbox in fused_calls:
                if (
                    abs(_cx(fbbox) - _cx(bbox)) <= 2.0
                    and abs(_cy(fbbox) - _cy(bbox)) <= 2.0
                ):
                    block = {**b, "block_id": fused.get("block_id") or b.get("block_id")}
                    break
        candidates.append((block, bbox))

    candidates.sort(key=lambda e: (_cy(e[1]), _cx(e[1])))
    seeds: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    duplicates = 0
    for block, bbox in candidates:
        dup = False
        for kept, kbbox in seeds:
            if (
                str(kept.get("text_raw") or "") == str(block.get("text_raw") or "")
                and abs(_cx(kbbox) - _cx(bbox)) <= 2.0
                and abs(_cy(kbbox) - _cy(bbox)) <= 2.0
            ):
                dup = True
                break
        if dup:
            duplicates += 1
        else:
            seeds.append((block, bbox))
    return seeds, duplicates


def reconstruct_call_matrices(
    blocks: list[dict[str, Any]],
    *,
    page_number: int,
    occupied_regions: list[tuple[float, float, float, float]] | None = None,
    native_blocks: list[dict[str, Any]] | None = None,
) -> list[Table]:
    """Rebuild grid tables from positioned call-cell blocks of one page.

    ``blocks`` are the normalized (fused) page blocks — they supply the
    labels, caption and legend. ``native_blocks``, when given, are the
    native glyph-layer blocks of the same page and become the seed source
    (the layout channel may have merged adjacent letters). ``occupied_regions``
    are bboxes already covered by parser-detected tables — cells inside
    them are never re-used.
    """

    occupied = occupied_regions or []
    fused_calls: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for b in blocks:
        bbox = _bbox(b)
        if bbox is not None and _CALL_RE.match(str(b.get("text_raw") or "")):
            if not any(_inside(bbox, r) for r in occupied):
                fused_calls.append((b, bbox))

    seeds: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    duplicates = 0
    if native_blocks is not None:
        seeds, duplicates = _collect_seeds(
            native_blocks, fused_calls, occupied, from_native=True
        )
        if len(seeds) < _MIN_CELLS:
            seeds, duplicates = [], 0  # native unusable — fall back
    if not seeds:
        seeds, duplicates = _collect_seeds(blocks, fused_calls, occupied, from_native=False)
    if len(seeds) < _MIN_CELLS:
        return []

    others: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for b in blocks:
        bbox = _bbox(b)
        text = str(b.get("text_raw") or "")
        if bbox is None or not text.strip():
            continue
        if _CALL_RE.match(text) or _CALL_RUN_RE.match(text.strip()):
            continue  # seeds or merged runs — not a label
        others.append((b, bbox))

    widths = sorted(b2[2] - b2[0] for _b, b2 in seeds)
    heights = sorted(b2[3] - b2[1] for _b, b2 in seeds)
    med_w = widths[len(widths) // 2]
    med_h = heights[len(heights) // 2]

    grid_x0 = min(b2[0] for _b, b2 in seeds)
    grid_top = min(b2[1] for _b, b2 in seeds)
    grid_bottom = max(b2[3] for _b, b2 in seeds)

    # ---- row skeleton: left-margin labels define ALL rows ---------------
    anchors: list[dict[str, Any]] = []
    for b, bbox in others:
        if bbox[2] >= grid_x0 - 2:
            continue
        cy = _cy(bbox)
        if cy < grid_top - 8 or cy > grid_bottom + 8:
            continue
        text = str(b["text_raw"]).strip()
        if not (1 < len(text) <= 80):
            continue
        anchors.append({"cy": cy, "text": text, "block": b, "bbox": bbox})
    anchors.sort(key=lambda a: a["cy"])
    merged_anchors: list[dict[str, Any]] = []
    for a in anchors:
        if merged_anchors and abs(a["cy"] - merged_anchors[-1]["cy"]) <= max(3.0, med_h * 0.9):
            prev = merged_anchors[-1]
            joiner = "" if prev["text"].endswith("-") else " "
            prev["text"] = f"{prev['text']}{joiner}{a['text']}"
            prev["bbox"] = (
                min(prev["bbox"][0], a["bbox"][0]),
                min(prev["bbox"][1], a["bbox"][1]),
                max(prev["bbox"][2], a["bbox"][2]),
                max(prev["bbox"][3], a["bbox"][3]),
            )
            prev["cy"] = (prev["cy"] + a["cy"]) / 2.0
        else:
            merged_anchors.append(dict(a))
    anchors = merged_anchors

    if len(anchors) >= 2:
        gaps = [anchors[i + 1]["cy"] - anchors[i]["cy"] for i in range(len(anchors) - 1)]
        positive = [g for g in gaps if g > 0]
        min_gap = min(positive) if positive else med_h
    else:
        min_gap = med_h
    row_tol = max(med_h, min_gap * _ROW_ATTACH_FRACTION)

    attached: dict[int, list[tuple[dict[str, Any], tuple[float, float, float, float]]]] = {
        i: [] for i in range(len(anchors))
    }
    unattached: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for entry in seeds:
        if anchors:
            nearest = min(
                range(len(anchors)), key=lambda i: abs(_cy(entry[1]) - anchors[i]["cy"])
            )
            if abs(_cy(entry[1]) - anchors[nearest]["cy"]) <= row_tol:
                attached[nearest].append(entry)
                continue
        unattached.append(entry)

    excluded = 0
    rows: list[dict[str, Any]] = [
        {"cy": a["cy"], "label": a, "cells": attached[i]}
        for i, a in enumerate(anchors)
    ]
    if unattached:
        band_index = _cluster(
            [_cy(b2) for _b, b2 in unattached], max(_TOLERANCE_FLOOR, med_h)
        )
        for band in band_index:
            cluster_cells = [unattached[j] for j in band]
            xs = [_cx(b2) for _b, b2 in cluster_cells]
            # a real extra row needs cells in >=2 distinct columns; two
            # overprints stacked on one spot are one physical glyph
            if len(cluster_cells) >= 2 and max(xs) - min(xs) > med_w:
                rows.append(
                    {
                        "cy": sum(_cy(b2) for _b, b2 in cluster_cells) / len(cluster_cells),
                        "label": None,
                        "cells": cluster_cells,
                    }
                )
            else:
                excluded += len(cluster_cells)
    rows.sort(key=lambda r: r["cy"])
    excluded += duplicates  # overprinted duplicates are exclusions too
    total_cells = sum(len(r["cells"]) for r in rows)
    if len(rows) < _MIN_ROWS or total_cells < _MIN_CELLS:
        return []

    # ---- columns: the longest contiguous lattice ------------------------
    # Stray glyphs elsewhere on the page (already excluded above) plus any
    # that attached to a row must not deform the lattice: keep the longest
    # run of column centres with sane spacing, and only accept cells whose
    # x sits close to one of the kept centres.
    grid_entries = [entry for row in rows for entry in row["cells"]]
    grid_x0 = min(b2[0] for _b, b2 in grid_entries)
    grid_top = min(b2[1] for _b, b2 in grid_entries)
    grid_bottom = max(b2[3] for _b, b2 in grid_entries)
    col_bands = _cluster([_cx(b2) for _b, b2 in grid_entries], max(_TOLERANCE_FLOOR, med_w))
    if len(col_bands) < _MIN_COLS:
        return []
    seed_x = [_cx(b2) for _b, b2 in grid_entries]
    centers_all = [sum(seed_x[j] for j in band) / len(band) for band in col_bands]
    runs: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(centers_all) + 1):
        if i == len(centers_all) or centers_all[i] - centers_all[i - 1] > _MAX_PITCH:
            runs.append((start, i - 1))
            start = i
    best = max(runs, key=lambda r: r[1] - r[0])
    col_centers = centers_all[best[0] : best[1] + 1]
    if len(col_centers) < _MIN_COLS:
        return []
    gaps = [col_centers[i + 1] - col_centers[i] for i in range(len(col_centers) - 1)]
    x_guard = max(med_w * 2.0, (min(gaps) if gaps else med_w) * 0.6)

    def col_of(bbox: tuple[float, float, float, float]) -> int | None:
        k = min(range(len(col_centers)), key=lambda i: abs(_cx(bbox) - col_centers[i]))
        if abs(_cx(bbox) - col_centers[k]) > x_guard:
            return None  # too far from every kept column — not part of the matrix
        return k

    # duplicate (row, column) assignments are disqualifying evidence — count
    # them instead of silently overwriting
    seen: set[tuple[int, int]] = set()
    grid: dict[tuple[int, int], tuple[dict[str, Any], tuple[float, float, float, float]]] = {}
    for r_i, row in enumerate(rows):
        for entry in row["cells"]:
            band = col_of(entry[1])
            if band is None:
                excluded += 1
                continue
            key = (r_i, band)
            if key in seen:
                excluded += 1
                continue
            seen.add(key)
            grid[key] = entry
    # After exclusions the surviving lattice must still be substantial —
    # a scatter page whose longest run kept only a handful of cells is
    # not a matrix.
    if len(grid) < _MIN_CELLS:
        return []

    def col_label(band_c: int) -> tuple[dict[str, Any], tuple[float, float, float, float]] | None:
        center = col_centers[band_c]
        candidates = [
            (b, bbox)
            for b, bbox in others
            if abs(_cx(bbox) - center) <= max(med_w, 4.0)
            and (bbox[1] > grid_bottom + 2 or bbox[3] < grid_top - 2)
        ]
        if not candidates:
            return None
        below = [p for p in candidates if p[1][1] > grid_bottom]
        pool = below or candidates
        return min(
            pool, key=lambda p: min(abs(p[1][1] - grid_bottom), abs(grid_top - p[1][3]))
        )

    caption_block = next(
        (
            (b, bbox)
            for b, bbox in others
            if _CAPTION_RE.search(str(b.get("text_raw") or ""))
        ),
        None,
    )

    # The legend carries the reading rules for this exact table (CLSI
    # disk-diffusion convention): colour/category words and what empty
    # cells mean. Carry it as a footnote so reviewers see how the calls
    # were decided next to the structure they explain.
    def _legend_hits(text: str) -> int:
        return sum(1 for pat in _LEGEND_PATTERNS if pat.search(text))

    footnote_ids = [
        str(b.get("block_id"))
        for b, _bbox_ in others
        if b.get("block_id")
        and len(str(b.get("text_raw") or "")) > 30
        and _legend_hits(str(b.get("text_raw") or "")) >= 2
    ][:3]

    cells: list[TableCell] = []
    header_ids: list[str] = []
    issues: list[str] = [
        "GEOMETRY_RECONSTRUCTED: table structure rebuilt from call-cell positions; review required"
    ]
    if excluded:
        issues.append(
            f"OFF_LATTICE_CALLS_EXCLUDED count={excluded}: stray or duplicate call "
            "glyphs were left as text, not table cells"
        )
    for band_c in range(len(col_centers)):
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
    for r_i, row in enumerate(rows):
        row_index = r_i + 1
        label = row["label"]
        if label is None:
            issues.append(f"ROW_LABEL_MISSING row={r_i}")
        else:
            cid = f"{page_number}:grid:r{row_index}c0"
            cells.append(
                TableCell(
                    cell_id=cid,
                    row_index=row_index,
                    column_index=0,
                    text_raw=str(label["text"]),
                    header_role="row",
                    bbox=label["bbox"],
                    source_refs=[
                        str((label["block"] or {}).get("block_id") or cid)
                    ],
                )
            )
            header_ids.append(cid)
        for band_c in range(len(col_centers)):
            entry = grid.get((r_i, band_c))
            if entry is None:
                continue  # present-but-blank cells stay absent, never invented
            block, bbox = entry
            cell_id = f"{page_number}:grid:r{row_index}c{band_c + 1}"
            cells.append(
                TableCell(
                    cell_id=cell_id,
                    row_index=row_index,
                    column_index=band_c + 1,
                    text_raw=str(block["text_raw"]).strip(),
                    header_role="data",
                    bbox=bbox,
                    source_refs=[str(block.get("block_id") or cell_id)],
                )
            )

    boxed = [c.bbox for c in cells if c.bbox]
    return [
        Table(
            table_id=f"{page_number}:grid",
            page_number=page_number,
            row_count=len(rows) + 1,
            column_count=len(col_centers) + 1,
            cells=cells,
            header_cell_ids=header_ids,
            caption=str(caption_block[0]["text_raw"]).strip() if caption_block else None,
            caption_refs=[str(caption_block[0].get("block_id"))] if caption_block else [],
            footnote_refs=footnote_ids,
            bbox=(
                min(b[0] for b in boxed),
                min(b[1] for b in boxed),
                max(b[2] for b in boxed),
                max(b[3] for b in boxed),
            ),
            structure_issues=issues,
            source="geometry_reconstructed",
        )
    ]


__all__ = ["reconstruct_call_matrices"]
