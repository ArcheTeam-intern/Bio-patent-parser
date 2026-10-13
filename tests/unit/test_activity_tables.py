"""Activity table extraction tests."""

from __future__ import annotations

import json
from pathlib import Path

from bio_pdf.contracts import Table, TableCell
from bio_pdf.extract.activities.tables import (
    extract_table_measurements,
    parse_table_header,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "domain" / "activity_table.json"
)


def _load_table() -> tuple[Table, dict[str, str]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cells = [
        TableCell(
            cell_id=c["cell_id"],
            row_index=c["row_index"],
            column_index=c["column_index"],
            text_raw=c["text_raw"],
        )
        for c in payload["cells"]
    ]
    table = Table(
        table_id=payload["table_id"],
        page_number=payload["page_number"],
        row_count=payload["row_count"],
        column_count=payload["column_count"],
        cells=cells,
        header_cell_ids=payload["header_cell_ids"],
    )
    return table, payload["evidences"]


def test_parse_table_header_metric_unit_target() -> None:
    header = parse_table_header("MIC (µg/mL), E. coli")
    assert header["metric"] == "MIC"
    assert header["unit"] == "µg/mL"
    assert header["target"] == "E. coli"


def test_parse_table_header_metric_only() -> None:
    header = parse_table_header("IC50 (nM)")
    assert header["metric"] == "IC50"
    assert header["unit"] == "nM"
    assert header["target"] is None


def test_four_measurements_extracted() -> None:
    table, evidences = _load_table()
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    assert len(measurements) == 4


def test_measurement_entity_target_unit_correct() -> None:
    table, evidences = _load_table()
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    by_key = {
        (m.entity_id, m.target_raw): m for m in measurements
    }
    a_ecoli = by_key[("A", "E. coli")]
    assert a_ecoli.value.kind == "scalar"
    assert a_ecoli.value.operator == ">"
    assert a_ecoli.value.number == "128"
    assert a_ecoli.unit_raw == "µg/mL"
    assert a_ecoli.unit_normalized == "ug/mL"
    assert a_ecoli.metric == "MIC"


def test_categorical_nd_is_not_zero() -> None:
    table, evidences = _load_table()
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    nd = next(
        m for m in measurements
        if m.entity_id == "B" and m.target_raw == "S. aureus"
    )
    assert nd.value.kind == "categorical"
    assert nd.value.qualifier == "ND"
    assert nd.value.number is None  # ND must not become 0


def test_field_evidence_references_source_cells() -> None:
    table, evidences = _load_table()
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    m = measurements[0]
    assert m.field_evidence["value"] == ["ev-a1"] or "ev-a1" in m.evidence_ids
    assert m.field_evidence.get("entity_id") is not None
    assert m.field_evidence.get("unit") is not None


def test_metric_is_consistent_across_columns() -> None:
    table, evidences = _load_table()
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    assert all(m.metric == "MIC" for m in measurements)