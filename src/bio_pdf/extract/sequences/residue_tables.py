"""Recover residue rows that a layout parser emitted as a TABLE (plan K12).

ST.25 listings are prose lines for a text parser, but MinerU/Docling
sometimes classify a residue grid as a table — EP3423477B1 page 31 is a
16x16 grid of three-letter codes (``Glu``, ``Leu180`` …) and carried NO
text lines at all. ``_page_blocks`` only walks ``page.blocks``, so the whole
page of residues was invisible to the ST.25 parser: SEQ 1 stopped at 348 of
549 residues and the page's evidence anchored on the footer page number.

This module turns such a residue table back into reading-order TEXT LINES
(the same shape every other block has), so the parser consumes it with no
special case. Detection is deliberately strict — an activity/susceptibility
matrix must never be re-interpreted as sequence:

- at least :data:`_MIN_RESIDUE_CELLS` non-empty cells,
- at least :data:`_MIN_RESIDUE_SHARE` of all whitespace tokens are
  three-letter codes (optionally position-suffixed, e.g. ``Glu180``) or
  nucleic-acid runs,
- a row is emitted only when at least half of ITS tokens qualify, so header
  rows ("Position | Residue") and footnote rows are dropped instead of
  becoming ``?`` residues.

Geometry is NOT invented: every emitted line carries the TABLE's own bbox,
so evidence anchors at the table region (block precision) and never claims
per-row coordinates the parser did not provide.
"""

from __future__ import annotations

from typing import Any

from bio_pdf.contracts import NormalizedDocument, Table
from bio_pdf.extract.sequences.amino_acids import looks_like_residue_token

#: A table needs at least this many non-empty cells to be a residue listing.
_MIN_RESIDUE_CELLS = 8
#: Share of residue tokens among ALL tokens (not cells) of the table.
_MIN_RESIDUE_SHARE = 0.7
#: Share of residue tokens inside ONE row for the row to be emitted.
_MIN_ROW_RESIDUE_SHARE = 0.5


def _tokens(text: str) -> list[str]:
    return text.split()


def _residue_share(texts: list[str]) -> tuple[int, int]:
    """``(residue_tokens, all_tokens)`` across the given cell texts."""

    total = 0
    residue = 0
    for text in texts:
        for token in _tokens(text):
            total += 1
            if looks_like_residue_token(token):
                residue += 1
    return residue, total


def _cell_texts(table: Table) -> list[str]:
    return [
        cell.text_raw.strip()
        for cell in table.cells
        if cell.text_raw and cell.text_raw.strip()
    ]


def is_residue_table(table: Table) -> bool:
    """Whether this table is a residue LISTING, not a data matrix."""

    texts = _cell_texts(table)
    if len(texts) < _MIN_RESIDUE_CELLS:
        return False
    residue, total = _residue_share(texts)
    if total == 0:
        return False
    return residue / total >= _MIN_RESIDUE_SHARE


def _row_lines(table: Table) -> list[tuple[int, str, list[str]]]:
    """``(row_index, line_text, cell_ids)`` in reading order."""

    by_row: dict[int, list[Any]] = {}
    for cell in table.cells:
        if not cell.text_raw or not cell.text_raw.strip():
            continue
        by_row.setdefault(cell.row_index, []).append(cell)

    out: list[tuple[int, str, list[str]]] = []
    for row_index in sorted(by_row):
        cells = sorted(by_row[row_index], key=lambda c: c.column_index)
        texts = [str(c.text_raw).strip() for c in cells]
        residue, total = _residue_share(texts)
        if total == 0 or residue / total < _MIN_ROW_RESIDUE_SHARE:
            # A header ("Position | Residue") or footnote row: including it
            # would inject non-residue tokens into the listing.
            continue
        out.append((row_index, " ".join(texts), [str(c.cell_id) for c in cells]))
    return out


def residue_table_blocks(
    document: NormalizedDocument,
    *,
    parser: str,
    source_artifact_id: str | None = None,
) -> list[dict[str, Any]]:
    """Reading-order text blocks for every residue table in the document.

    The returned dicts match :func:`bio_pdf.workflow.handlers._page_blocks`
    so they can be interleaved into the same extraction feed.
    """

    blocks: list[dict[str, Any]] = []
    for page in document.pages:
        for table in page.tables:
            if not is_residue_table(table):
                continue
            bbox = list(table.bbox) if table.bbox else None
            for row_index, text, cell_ids in _row_lines(table):
                blocks.append(
                    {
                        "text": text,
                        "kind": "text",
                        "semantic_role": "unknown",
                        "page_number": int(table.page_number),
                        "bbox": list(bbox) if bbox else None,
                        "page_width": page.width_pt,
                        "page_height": page.height_pt,
                        # The row keeps the TABLE's box (the parser returned
                        # no per-cell geometry): block precision, no invention.
                        "block_id": f"{table.table_id}:r{row_index}",
                        "source_refs": list(cell_ids),
                        "source_aliases": [],
                        "source_parser": parser,
                        "source_artifact_id": source_artifact_id,
                        "bbox_verified": getattr(table, "bbox_verified", None),
                        "_residue_table_id": str(table.table_id),
                    }
                )
    return blocks


__all__ = ["is_residue_table", "residue_table_blocks"]
