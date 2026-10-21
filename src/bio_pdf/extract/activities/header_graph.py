"""Hierarchical header graph for normalised tables (plan B06).

Builds, for every data cell, the full header PATH from the cells marked
``header_role`` (column/row/both) honouring rowspan/colspan:

- column path: every header cell above the data cell whose column span
  covers the data cell's column (parent row first → "E. coli" → "MIC (µM)");
- row path: every header cell to the left whose row span covers the data
  cell's row ("Peptide" → row label).

The graph answers "what context does this value carry" without guessing
from cell text — merged parents become the TARGET, child headers the
METRIC, and the row label the entity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bio_pdf.contracts import Table, TableCell


@dataclass
class HeaderPath:
    """Structured context for one data cell."""

    data_cell: TableCell
    column_headers: list[TableCell] = field(default_factory=list)  # parent→child
    row_headers: list[TableCell] = field(default_factory=list)

    @property
    def column_texts(self) -> list[str]:
        return [c.text_raw.strip() for c in self.column_headers if c.text_raw.strip()]

    @property
    def row_texts(self) -> list[str]:
        return [c.text_raw.strip() for c in self.row_headers if c.text_raw.strip()]


def _covers(cell: TableCell, *, row: int, column: int) -> bool:
    return (
        cell.row_index <= row < cell.row_index + cell.rowspan
        and cell.column_index <= column < cell.column_index + cell.colspan
    )


def build_header_paths(table: Table) -> list[HeaderPath]:
    """One :class:`HeaderPath` per data cell (rows top→bottom, cols L→R)."""

    header_ids = set(table.header_cell_ids or [])
    header_rows = {
        c.row_index for c in table.cells if c.cell_id in header_ids
    }
    header_rows = header_rows or {0}

    def _is_header(c) -> bool:
        if c.header_role in ("column", "row", "both"):
            return True
        if c.cell_id in header_ids:
            return True
        # Legacy tables (roles unknown): the row-label column (column 0)
        # below the header rows carries row headers.
        if (
            c.header_role == "unknown"
            and c.column_index == 0
            and c.row_index not in header_rows
        ):
            return True
        return False

    headers = [c for c in table.cells if _is_header(c)]
    header_cell_ids = {c.cell_id for c in headers}
    data_cells = sorted(
        (c for c in table.cells if c.cell_id not in header_cell_ids),
        key=lambda c: (c.row_index, c.column_index),
    )
    column_count = table.column_count or 1
    # Where the DATA starts. A merged column header always sits above every
    # data row; a full-width cell that appears only after data has started is
    # a block label (see ``_is_block_banner``).
    first_data_row = min((c.row_index for c in data_cells), default=0)

    def _is_block_banner(h: TableCell) -> bool:
        """A full-width row INSIDE the data area — a group label, not a header.

        MinerU writes the last organism of live Table 4a as
        ``<td colspan="5">Acinetobacter johnsonii</td>`` on a row of its own.
        Read as a merged column header it becomes the TARGET of every value
        below it and the organism is lost; it actually labels the rows
        beneath it.
        """

        return h.colspan >= column_count and h.row_index >= first_data_row

    def _block_banner(data: TableCell) -> TableCell | None:
        """The nearest full-width banner above ``data``, if any.

        Such a cell is neither a column header (it is not part of the header
        block) nor a left label (it covers the data's column) — without this
        rule the rows beneath it carry no entity at all and the peptide /
        organism axes stay flipped.
        """

        best: TableCell | None = None
        for h in headers:
            if h.row_index >= data.row_index or not _is_block_banner(h):
                continue
            if not h.column_index <= data.column_index < h.column_index + h.colspan:
                continue
            if best is None or h.row_index > best.row_index:
                best = h
        return best

    paths: list[HeaderPath] = []
    for data in data_cells:
        # Legacy cells may carry header_role="unknown"; the row-0 fallback
        # for headers (header_cell_ids) keeps old tables working.
        if not data.text_raw.strip():
            continue
        column_headers = sorted(
            (
                h
                for h in headers
                if h.row_index + h.rowspan <= data.row_index
                and h.column_index <= data.column_index < h.column_index + h.colspan
                and not _is_block_banner(h)
            ),
            key=lambda h: (h.row_index, h.column_index),
        )
        row_headers = sorted(
            (
                h
                for h in headers
                if h.column_index + h.colspan <= data.column_index
                and h.row_index <= data.row_index < h.row_index + h.rowspan
            ),
            key=lambda h: (h.row_index, h.column_index),
        )
        banner = _block_banner(data)
        if banner is not None:
            row_headers.append(banner)
        paths.append(
            HeaderPath(data_cell=data, column_headers=column_headers, row_headers=row_headers)
        )
    return paths


def row_header_for(table: Table, data_cell: TableCell) -> TableCell | None:
    """The single row-label cell (mostly column 0) for a data row."""

    for path in build_header_paths(table):
        if path.data_cell.cell_id == data_cell.cell_id:
            return path.row_headers[0] if path.row_headers else None
    return None


__all__ = ["HeaderPath", "build_header_paths", "row_header_for"]
