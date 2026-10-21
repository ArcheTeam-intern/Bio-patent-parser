"""Caption-declared metric (patent MIC tables).

Patent tables declare the metric in the TITLE — "Table 6. Minimum
inhibitory concentration (MICs) of peptide variants…" — while the column
headers carry strain or peptide names ("Bacteria | Strains | Chain201 |
Chain204"). Before this fix such tables were either UNCLASSIFIED (no
metric header) or ACTIVITY_NUMERIC with zero measurements, because
``_resolve_column_context`` looks for a registered metric in the headers
and finds none.

The live EP3423477B1 run produced 0 measurements across its MIC tables.
After the fix the same cached payload yields 125 + 19 measurements.

Contract:
- a header metric ALWAYS wins (a multi-metric table is never flattened);
- the caption fallback only applies when no header carries a metric;
- inline units on values (">64 μg/ml") are peeled into ``unit_raw``;
- no evidence on any cell → still no measurement.
"""

from __future__ import annotations

from bio_pdf.contracts import Table
from bio_pdf.extract.activities.table_classifier import TableKind, classify_table
from bio_pdf.extract.activities.tables import _caption_metric, extract_table_measurements


def _table(
    *,
    caption: str | None = None,
    header: list[str],
    rows: list[list[str]],
    continuation_caption: str | None = None,
) -> Table:
    cells = []
    for ci, text in enumerate(header):
        cells.append(
            {
                "cell_id": f"h{ci}",
                "row_index": 0,
                "column_index": ci,
                "text_raw": text,
                "header_role": "column",
            }
        )
    for ri, row in enumerate(rows, start=1):
        for ci, text in enumerate(row):
            cells.append(
                {
                    "cell_id": f"r{ri}c{ci}",
                    "row_index": ri,
                    "column_index": ci,
                    "text_raw": text,
                    "header_role": "row" if ci == 0 else "unknown",
                }
            )
    return Table.model_validate(
        {
            "table_id": "1:t0",
            "page_number": 1,
            "row_count": len(rows) + 1,
            "column_count": len(header),
            "header_cell_ids": [f"h{ci}" for ci in range(len(header))],
            "caption": caption,
            "continuation_caption": continuation_caption,
            "cells": cells,
        }
    )


def _evidences(table: Table) -> dict[str, str]:
    return {cell.cell_id: f"ev-{cell.cell_id}" for cell in table.cells}


def test_caption_metric_is_parsed_from_the_title() -> None:
    table = _table(
        caption="Table 6. Minimum inhibitory concentration (MICs) of peptide variants",
        header=["Compound", "S. aureus RN4220", "E. coli"],
        rows=[["104", "0.25 μg/ml", "1 μg/ml"]],
    )
    assert _caption_metric(table) == (
        "MIC",
        "Table 6. Minimum inhibitory concentration (MICs) of peptide variants",
    )


def test_patent_layout_is_classified_as_activity_numeric() -> None:
    """Metric in the caption + numeric/unit values → ACTIVITY_NUMERIC even
    though no header cell names a metric."""

    table = _table(
        caption="Table 6. Minimum inhibitory concentration (MICs) of peptide variants",
        header=["Compound", "S. aureus RN4220", "E. coli"],
        rows=[["104", "0.25 μg/ml", "1 μg/ml"], ["104 D", "64 μg/ml", "32 μg/ml"]],
    )
    assert classify_table(table) is TableKind.ACTIVITY_NUMERIC


def test_caption_metric_yields_measurements_with_inline_units() -> None:
    table = _table(
        caption="Table 6. Minimum inhibitory concentration (MICs) of peptide variants",
        header=["Compound", "S. aureus RN4220", "E. coli"],
        rows=[["104", "0.25 μg/ml", "1 μg/ml"]],
    )
    measurements = extract_table_measurements(
        table,
        document_id="sha256:test",
        evidences=_evidences(table),
        caption_metric=_caption_metric(table),
    )
    assert len(measurements) == 2
    by_target = {m.target_raw: m for m in measurements}
    assert by_target["S. aureus RN4220"].metric == "MIC"
    assert by_target["S. aureus RN4220"].value.number == "0.25"
    assert by_target["S. aureus RN4220"].unit_raw == "μg/ml"
    assert by_target["S. aureus RN4220"].entity_raw == "104"
    assert by_target["E. coli"].value.number == "1"


def test_comparator_values_keep_the_operator() -> None:
    table = _table(
        caption="Table 6. Minimum inhibitory concentration (MICs)",
        header=["Compound", "S. aureus COL"],
        rows=[["104", ">64 μg/ml"]],
    )
    measurements = extract_table_measurements(
        table,
        document_id="sha256:test",
        evidences=_evidences(table),
        caption_metric=_caption_metric(table),
    )
    assert len(measurements) == 1
    measurement = measurements[0]
    assert measurement.value.operator == ">"
    assert measurement.value.number == "64"
    assert measurement.unit_raw == "μg/ml"


def test_header_metric_wins_over_the_caption() -> None:
    """A multi-metric table must never be flattened by the caption."""

    table = _table(
        caption="Table 6. Minimum inhibitory concentration (MICs)",
        header=["Compound", "MIC (μg/mL)", "IC50 (μg/mL)"],
        rows=[["104", "2", "8"]],
    )
    measurements = extract_table_measurements(
        table,
        document_id="sha256:test",
        evidences=_evidences(table),
        caption_metric=_caption_metric(table),
    )
    metrics = sorted(m.metric for m in measurements)
    assert metrics == ["IC50", "MIC"]


def test_caption_metric_does_not_invent_records_without_evidence() -> None:
    table = _table(
        caption="Table 6. Minimum inhibitory concentration (MICs)",
        header=["Compound", "S. aureus RN4220"],
        rows=[["104", "0.25 μg/ml"]],
    )
    assert (
        extract_table_measurements(
            table,
            document_id="sha256:test",
            evidences={},  # nothing registered
            caption_metric=_caption_metric(table),
        )
        == []
    )


def test_no_caption_metric_and_no_header_metric_stays_empty() -> None:
    table = _table(
        caption="Table 3. Strain catalogue",
        header=["Compound", "S. aureus RN4220"],
        rows=[["104", "0.25 μg/ml"]],
    )
    assert _caption_metric(table) is None
    assert (
        extract_table_measurements(
            table,
            document_id="sha256:test",
            evidences=_evidences(table),
            caption_metric=None,
        )
        == []
    )


# ---- a table SPLIT across the fold ---------------------------------------


def test_continuation_caption_drives_classification_without_becoming_caption() -> None:
    """Page 26 opens with the tail of page 25's MIC grid. The title is printed
    only on page 25, so the tail must borrow it for classification while its
    OWN caption stays empty (the UI labels it as inherited)."""

    table = _table(
        header=["Bacteria*", "Strains", "Chain304"],
        rows=[["S. aureus", "1", "16"], ["E. coli", "2", "4"]],
        continuation_caption="Table 5. Minimum inhibitory concentration (MICs)",
    )
    assert table.caption is None
    assert classify_table(table) is TableKind.ACTIVITY_NUMERIC
    assert _caption_metric(table) == (
        "MIC",
        "Table 5. Minimum inhibitory concentration (MICs)",
    )


def test_continuation_caption_recovers_the_tail_page_measurements() -> None:
    table = _table(
        header=["Bacteria*", "Strains", "Chain304"],
        rows=[["S. aureus", "1", "16"], ["E. coli", "2", "4"]],
        continuation_caption="Table 5. Minimum inhibitory concentration (MICs)",
    )
    measurements = extract_table_measurements(
        table,
        document_id="sha256:test",
        evidences=_evidences(table),
        caption_metric=_caption_metric(table),
    )
    # One value per strain row; the "Strains" serial column is a row index,
    # never a measurement (its integers are isolate numbers).
    assert len(measurements) == 2
    assert {m.metric for m in measurements} == {"MIC"}
    # Rows are organisms, the column is the peptide → the axes travel as the
    # reviewer reads them (entity = chain, target = strain), as on page 23.
    assert {m.entity_raw for m in measurements} == {"Chain304"}
    assert {m.target_raw for m in measurements} == {"S. aureus", "E. coli"}
