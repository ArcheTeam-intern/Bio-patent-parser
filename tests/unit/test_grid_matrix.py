"""Grid reconstruction for parser-missed susceptibility call matrices."""

from __future__ import annotations

import pytest

from bio_pdf.parsers.grid_matrix import reconstruct_call_matrices


def _b(bid: str, text: str, x0: float, y0: float, x1: float, y1: float) -> dict:
    return {
        "block_id": bid,
        "kind": "text",
        "semantic_role": "body",
        "text_raw": text,
        "bbox": [x0, y0, x1, y1],
    }


def _paper_like_blocks() -> list[dict]:
    """4 drugs x 4 isolates of R/S calls + labels + rotated caption."""
    drugs = ["Colistin", "Tobramycin", "Meropenem", "Piperacillin-tazobactam"]
    isolates = ["17 0129 1317", "17 0112 2944", "17 0042", "17 0519"]
    calls = [
        ["R", "S", "R", "S"],
        ["S", "S", "R", "R"],
        ["R", "R", "S", "R"],
        ["S", "R", "R", "S"],
    ]
    blocks = [
        _b("cap", "TABLE 3 | Antibiotic sensitivity of clinically used antibiotics", 44, 334, 51, 711, ),
    ]
    for r, drug in enumerate(drugs):
        y = 80 + r * 30
        blocks.append(_b(f"d{r}", drug, 59, y, 140, y + 7))
        for c, call in enumerate(calls[r]):
            x = 150 + c * 25
            blocks.append(_b(f"c{r}{c}", call, x, y, x + 5, y + 7))
    for c, iso in enumerate(isolates):
        x = 150 + c * 25
        blocks.append(_b(f"i{c}", iso, x - 8, 210, x + 12, 217))
    return blocks


def test_reconstructs_regular_call_lattice_with_labels():
    tables = reconstruct_call_matrices(_paper_like_blocks(), page_number=9)
    assert len(tables) == 1
    t = tables[0]
    assert t.source == "geometry_reconstructed"
    assert t.caption is not None and t.caption.startswith("TABLE 3")
    assert t.row_count == 5  # header row (column labels) + 4 data rows
    assert t.column_count == 5  # label column + 4 call columns
    role_by_text = {(c.row_index, c.column_index): c for c in t.cells}
    assert role_by_text[(0, 1)].text_raw == "17 0129 1317"
    assert role_by_text[(0, 1)].header_role == "column"
    assert role_by_text[(1, 0)].text_raw == "Colistin"
    assert role_by_text[(1, 0)].header_role == "row"
    assert role_by_text[(1, 1)].text_raw == "R"
    assert role_by_text[(1, 1)].header_role == "data"
    assert role_by_text[(1, 1)].bbox is not None  # real geometry travels
    assert any(i.startswith("GEOMETRY_RECONSTRUCTED") for i in t.structure_issues)
    # every cell keeps the source block id for provenance
    assert role_by_text[(1, 1)].source_refs == ["c00"]


def test_noise_page_is_not_reconstructed():
    # two stray R letters do not make a table
    blocks = [
        _b("a", "R", 100, 100, 105, 107),
        _b("b", "S", 400, 500, 405, 507),
        _b("t", "Results are R or S reported", 60, 200, 300, 207),
    ]
    assert reconstruct_call_matrices(blocks, page_number=1) == []


def test_irregular_scatter_is_not_reconstructed():
    # 12 calls but every other column far outside the pitch → lattice rejected
    blocks = [_b("d", "Colistin", 59, 80, 140, 87)]
    for i in range(12):
        x = 150 + (i % 3) * 25 + (i // 3) * 400  # huge horizontal jumps
        y = 80 + (i // 3) * 30
        blocks.append(_b(f"c{i}", ["R", "S"][i % 2], x, y, x + 5, y + 7))
    assert reconstruct_call_matrices(blocks, page_number=1) == []


def test_calls_inside_parser_tables_are_excluded():
    blocks = _paper_like_blocks()
    # pretend a parser table already covers the first two data rows
    covered = (140, 70, 260, 145)
    tables = reconstruct_call_matrices(blocks, page_number=9, occupied_regions=[covered])
    assert tables == [] or all(
        len([c for c in t.cells if c.header_role == "data"]) < 12 for t in tables
    )


def test_row_without_label_is_reported_not_invented():
    blocks = [b for b in _paper_like_blocks() if b["block_id"] != "d1"]
    (tables,) = reconstruct_call_matrices(blocks, page_number=9)
    assert any("ROW_LABEL_MISSING row=1" in i for i in tables.structure_issues)
    # the row's data cells still exist (structure preserved, semantics gated)
    assert any(
        c.row_index == 2 and c.header_role == "data" for c in tables.cells
    )


def test_dense_columns_are_not_merged():
    """Real regression (paper page 9): 6 call columns at ~11.7pt pitch with
    6.5pt-wide glyphs. A fixed clustering span fused adjacent columns into
    one band and the duplicate guard then rejected the whole matrix.
    Tolerance must follow the cell size, not a constant."""
    blocks = []
    ys = [80 + r * 30 for r in range(10)]
    xs = [150.6, 171.8, 183.6, 203.3, 224.5, 236.2]
    for r, y in enumerate(ys):
        blocks.append(_b(f"d{r}", "Gentamicin", 59, y, 140, y + 7))
        for c, x in enumerate(xs):
            blocks.append(_b(f"c{r}{c}", ["R", "S"][(r + c) % 2], x, y, x + 6.5, y + 7))
    (t,) = reconstruct_call_matrices(blocks, page_number=9)
    data = [c for c in t.cells if c.header_role == "data"]
    assert len(data) == 60
    assert len({c.column_index for c in data}) == 6


def test_single_cell_noise_rows_are_trimmed_not_fatal():
    """Isolated R/S glyphs elsewhere on the page must not abort the whole
    reconstruction (they are dropped as off-lattice and reported)."""
    blocks = _paper_like_blocks()
    blocks.append(_b("n1", "R", 400, 400, 405, 407))  # stray glyph, own row+col
    blocks.append(_b("n2", "S", 500, 450, 505, 457))
    (t,) = reconstruct_call_matrices(blocks, page_number=9)
    assert any("OFF_LATTICE_CALLS_EXCLUDED" in i for i in t.structure_issues)
    data = [c for c in t.cells if c.header_role == "data"]
    assert len(data) == 16  # the real 4x4 matrix, noise excluded


def test_native_glyphs_recover_letters_fused_into_runs():
    """Real regression (paper TABLE 2): the layout channel merged adjacent
    letters into 'SSS' runs, so half the call glyphs never seeded the
    grid. The native glyph layer keeps every letter as its own line and
    must be used as the seed source when provided."""
    fused = [
        _b("cap", "TABLE 2 | Antibiotic sensitivity", 44, 20, 200, 27),
        # drug labels defining four rows
        _b("d0", "Vancomycin", 60, 100, 130, 107),
        _b("d1", "Tobramycin", 60, 120, 130, 127),
        _b("d2", "Meropenem", 60, 140, 130, 147),
        _b("d3", "Amikacin", 60, 160, 130, 167),
        # the layout channel only kept merged runs — NOT usable as seeds
        _b("run0", "SSS", 160, 100, 175, 107),
        _b("run1", "SS", 160, 120, 170, 127),
    ]
    native = [
        # native keeps individual glyphs with exact positions (3 columns)
        _b("n0", "S", 160, 100, 166, 107),
        _b("n1", "S", 172, 100, 178, 107),
        _b("n2", "S", 184, 100, 190, 107),
        _b("n3", "S", 160, 120, 166, 127),
        _b("n4", "S", 172, 120, 178, 127),
        _b("n5", "R", 160, 140, 166, 147),
        _b("n6", "I", 172, 140, 178, 147),
        _b("n7", "S", 160, 160, 166, 167),
        _b("n8", "S", 172, 160, 178, 167),
        _b("n9", "S", 184, 160, 190, 167),
    ]
    (t,) = reconstruct_call_matrices(fused, page_number=2, native_blocks=native)
    data = [c for c in t.cells if c.header_role == "data"]
    assert len(data) == 10                      # every native glyph counted
    assert len({c.column_index for c in data}) == 3
    row_headers = [c.text_raw for c in t.cells if c.header_role == "row"]
    assert row_headers == ["Vancomycin", "Tobramycin", "Meropenem", "Amikacin"]


def test_empty_rows_are_kept_and_labels_never_shift():
    """A drug row whose cells were all 'not tested' (empty in the figure)
    must still exist — anchored by its label — and the following labels
    must never slide onto a neighbour's cells."""
    blocks = [
        _b("cap", "TABLE 2 | Antibiotic sensitivity", 44, 20, 200, 27),
        _b("d0", "Vancomycin", 60, 100, 130, 107),
        _b("d1", "Tobramycin", 60, 120, 130, 127),   # row with NO cells
        _b("d2", "Meropenem", 60, 140, 130, 147),
        _b("d3", "Amikacin", 60, 160, 130, 167),
    ]
    for r, y in ((0, 100), (2, 140), (3, 160)):
        for c, x in enumerate((160, 172, 184, 196)):
            blocks.append(_b(f"c{r}{c}", "S", x, y, x + 6, y + 7))
    (t,) = reconstruct_call_matrices(blocks, page_number=2)
    rows = {c.row_index: c.text_raw for c in t.cells if c.header_role == "row"}
    # labels sit on THEIR OWN rows, including the empty one
    assert rows == {1: "Vancomycin", 2: "Tobramycin", 3: "Meropenem", 4: "Amikacin"}
    data = [c for c in t.cells if c.header_role == "data"]
    assert len(data) == 12
    assert {c.row_index for c in data} == {1, 3, 4}  # row 2 stays empty


def test_native_source_falls_back_when_unusable():
    """A native channel with no call glyphs (e.g. a scan) must fall back
    to the fused blocks instead of reconstructing nothing."""
    blocks = _paper_like_blocks()
    tables = reconstruct_call_matrices(
        blocks, page_number=9, native_blocks=[_b("only-text", "prose line", 10, 10, 100, 17)]
    )
    assert len(tables) == 1
    assert len([c for c in tables[0].cells if c.header_role == "data"]) == 16
