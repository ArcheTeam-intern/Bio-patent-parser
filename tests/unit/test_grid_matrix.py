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
