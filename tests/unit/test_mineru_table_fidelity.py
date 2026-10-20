"""MinerU table fidelity: HTML entities and stacked-table splits.

Two defects the reviewer saw in the live EP3423477B1 run:

1. MIC thresholds arrived as ``&gt;128`` — MinerU's ``table_body`` is
   HTML-escaped, and the raw form both reads wrong in the UI and loses
   the comparator (">128" and "128" became the same value).
2. A page carrying TWO stacked tables (a continued listing above, Table 4b
   below) was detected as ONE grid, so the second table's header repeats
   mid-grid and every row after it reads as misaligned.

These tests pin the fixes without touching the network.
"""

from __future__ import annotations

from bio_pdf.parsers.mineru_adapter import (
    _project_content_list,
    _split_repeated_header_table,
)


def _table_item(body: str, *, page_idx: int = 0) -> dict:
    return {
        "type": "table",
        "page_idx": page_idx,
        "bbox": [50, 100, 550, 700],
        "table_body": body,
        "table_caption": ["Table 4b: Minimum inhibitory concentration"],
    }


def test_html_entities_are_decoded_in_cell_text() -> None:
    body = (
        "<table>"
        "<tr><td>Bacteria*</td><td>Strains</td><td>Chain201</td></tr>"
        "<tr><td>Serratia marcescens</td><td>1</td><td>&gt;128</td></tr>"
        "<tr><td></td><td>2</td><td>&lt;0.06</td></tr>"
        "</table>"
    )
    pages = _project_content_list([_table_item(body)], page_order=[1], page_sizes={1: (595.0, 780.0, 0)})
    cells = pages["pages"][0]["tables"][0]["data"]["table_cells"]
    texts = [c["text"] for c in cells]
    assert "&gt;128" not in texts and "&lt;0.06" not in texts
    assert ">128" in texts and "<0.06" in texts


def test_comparator_survives_into_the_activity_value() -> None:
    """End to end: ">128" must parse as operator ">" + number "128"."""

    from bio_pdf.contracts import NormalizedDocument
    from bio_pdf.extract.activities.table_classifier import classify_table
    from bio_pdf.extract.activities.tables import _caption_metric, extract_table_measurements
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    body = (
        "<table>"
        "<tr><td>Compound</td><td>S. aureus</td></tr>"
        "<tr><td>104</td><td>&gt;128</td></tr>"
        "</table>"
    )
    pages = _project_content_list([_table_item(body)], page_order=[1], page_sizes={1: (595.0, 780.0, 0)})
    doc = normalize_docling_document(pages, parser_run_id="probe", document_id="d")
    table = doc.pages[0].tables[0]
    assert str(classify_table(table)) == "TableKind.ACTIVITY_NUMERIC"
    measurements = extract_table_measurements(
        table,
        document_id="sha256:test",
        evidences={c.cell_id: f"ev-{c.cell_id}" for c in table.cells},
        caption_metric=_caption_metric(table),
    )
    assert len(measurements) == 1
    assert measurements[0].value.operator == ">"
    assert measurements[0].value.number == "128"


def test_stacked_tables_are_split_on_the_repeated_header() -> None:
    """Two tables in one grid: the second header row starts a new table."""

    body = (
        "<table>"
        # table 1
        "<tr><td>Bacteria*</td><td>Strains</td><td>Chain104</td></tr>"
        "<tr><td>Acinetobacter johnsonii</td><td>1</td><td>8</td></tr>"
        "<tr><td></td><td>2</td><td>4</td></tr>"
        # table 2 — same header vocabulary
        "<tr><td>Bacteria*</td><td>Strains</td><td>Chain201</td></tr>"
        "<tr><td>Escherichia coli</td><td>1</td><td>16</td></tr>"
        "<tr><td></td><td>2</td><td>1</td></tr>"
        "</table>"
    )
    pages = _project_content_list([_table_item(body)], page_order=[1], page_sizes={1: (595.0, 780.0, 0)})
    tables = pages["pages"][0]["tables"]
    assert len(tables) == 2, [t["row_count"] for t in tables]
    first = [c["text"] for c in tables[0]["data"]["table_cells"]]
    second = [c["text"] for c in tables[1]["data"]["table_cells"]]
    assert "Chain104" in first and "Chain201" not in first
    assert "Chain201" in second and "Chain104" not in second
    for table in tables:
        assert any(
            str(issue).startswith("TABLE_SPLIT_REPEATED_HEADER")
            for issue in table["structure_issues"]
        )


def test_multi_level_header_is_not_split() -> None:
    """A colspan parent above column names shares NO cell text with the
    first row, so it must stay one table."""

    body = (
        "<table>"
        '<tr><td colspan="2">Measurement</td><td>Notes</td></tr>'
        "<tr><td>Compound</td><td>Value</td><td>n</td></tr>"
        "<tr><td>104</td><td>16</td><td>3</td></tr>"
        "</table>"
    )
    pages = _project_content_list([_table_item(body)], page_order=[1], page_sizes={1: (595.0, 780.0, 0)})
    assert len(pages["pages"][0]["tables"]) == 1


def test_table_without_repeated_header_stays_whole() -> None:
    body = (
        "<table>"
        "<tr><td>Bacteria*</td><td>Strains</td><td>Chain201</td></tr>"
        "<tr><td>Escherichia coli</td><td>1</td><td>16</td></tr>"
        "<tr><td></td><td>2</td><td>1</td></tr>"
        "</table>"
    )
    pages = _project_content_list([_table_item(body)], page_order=[1], page_sizes={1: (595.0, 780.0, 0)})
    assert len(pages["pages"][0]["tables"]) == 1
