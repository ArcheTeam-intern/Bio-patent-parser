"""Normalization & fusion tests (plan T05 acceptance)."""

from __future__ import annotations

import pytest

from bio_pdf.contracts import NormalizedDocument, NormalizedPage, Table, TableCell, TextBlock
from bio_pdf.errors import WorkflowError
from bio_pdf.parsers.fusion import fuse_documents
from bio_pdf.parsers.normalize_docling import (
    _structure_issues,
    _validate_id_uniqueness,
)


def _page(number: int, blocks: list[TextBlock], tables: list[Table] | None = None) -> NormalizedPage:
    return NormalizedPage(
        page_number=number,
        width_pt=612.0,
        height_pt=792.0,
        blocks=blocks,
        tables=tables or [],
    )


def _doc(pages: list[NormalizedPage]) -> NormalizedDocument:
    return NormalizedDocument(
        schema_version="1.1.0",
        document_id="sha256:x",
        parser_run_id="r1",
        pages=pages,
    )


def test_two_tables_same_page_ids_do_not_collide() -> None:
    cells_a = [TableCell(cell_id="1:t0:c0", row_index=0, column_index=0, text_raw="A")]
    cells_b = [TableCell(cell_id="1:t1:c0", row_index=0, column_index=0, text_raw="B")]
    doc = _doc(
        [
            _page(
                1,
                [],
                [
                    Table(table_id="1:t0", page_number=1, row_count=1, column_count=1, cells=cells_a),
                    Table(table_id="1:t1", page_number=1, row_count=1, column_count=1, cells=cells_b),
                ],
            )
        ]
    )
    _validate_id_uniqueness(doc)  # no collision


def test_same_id_different_content_raises() -> None:
    doc = _doc(
        [
            _page(
                1,
                [
                    TextBlock(block_id="dup", text_raw="one"),
                    TextBlock(block_id="dup", text_raw="two"),
                ],
            )
        ]
    )
    with pytest.raises(WorkflowError, match="NORMALIZATION_ID_COLLISION"):
        _validate_id_uniqueness(doc)


def test_structure_issues_detects_grid_overlap() -> None:
    cells = [
        TableCell(cell_id="c0", row_index=0, column_index=0, colspan=2, text_raw="x"),
        TableCell(cell_id="c1", row_index=0, column_index=1, text_raw="y"),
    ]
    issues = _structure_issues(cells)
    assert any("TABLE_GRID_OVERLAP" in i for i in issues)


def test_structure_issues_clean_table_has_none() -> None:
    cells = [
        TableCell(cell_id="c0", row_index=0, column_index=0, colspan=2, text_raw="x"),
        TableCell(cell_id="c1", row_index=1, column_index=0, rowspan=2, text_raw="y"),
    ]
    assert _structure_issues(cells) == []


def test_fusion_prefers_native_lexical_source_on_overlap() -> None:
    # Layout block and native block cover the same area; native text is the
    # preferred lexical source when they agree modulo whitespace.
    layout = _doc(
        [
            _page(
                1,
                [
                    TextBlock(
                        block_id="layout-1",
                        bbox=(10.0, 10.0, 200.0, 24.0),
                        text_raw="Ala Gly Leu",
                    )
                ],
            )
        ]
    )
    native = _doc(
        [
            _page(
                1,
                [
                    TextBlock(
                        block_id="native-1",
                        bbox=(10.0, 10.0, 200.0, 24.0),
                        text_raw="AlaGly Leu",
                    )
                ],
            )
        ]
    )
    fused = fuse_documents(layout, native)
    block = fused.document.pages[0].blocks[0]
    assert block.text_raw == "AlaGly Leu"  # native characters win
    assert fused.merged_pairs == 1
    assert not fused.conflicts


def test_fusion_records_conflict_and_keeps_primary() -> None:
    layout = _doc(
        [
            _page(
                1,
                [
                    TextBlock(
                        block_id="layout-1",
                        bbox=(10.0, 10.0, 200.0, 24.0),
                        text_raw="AGLK",
                    )
                ],
            )
        ]
    )
    native = _doc(
        [
            _page(
                1,
                [
                    TextBlock(
                        block_id="native-1",
                        bbox=(10.0, 10.0, 200.0, 24.0),
                        text_raw="AG1K",  # OCR confusion I/1
                    )
                ],
            )
        ]
    )
    fused = fuse_documents(layout, native)
    assert fused.conflicts
    assert fused.document.pages[0].blocks[0].text_raw == "AGLK"  # no silent pick
    assert any("conflict" in w for w in fused.warnings)


def test_fusion_keeps_native_only_blocks() -> None:
    layout = _doc([_page(1, [])])
    native = _doc(
        [_page(1, [TextBlock(block_id="n1", bbox=(0.0, 0.0, 10.0, 10.0), text_raw="extra")])]
    )
    fused = fuse_documents(layout, native)
    assert any(b.block_id == "n1" for b in fused.document.pages[0].blocks)


def test_fusion_without_native_is_passthrough() -> None:
    layout = _doc([_page(1, [TextBlock(block_id="b", text_raw="x")])])
    fused = fuse_documents(layout, None)
    assert fused.document is layout
