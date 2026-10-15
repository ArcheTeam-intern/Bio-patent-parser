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
        (m.entity_raw, m.target_raw): m for m in measurements
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
        if m.entity_raw == "B" and m.target_raw == "S. aureus"
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
    assert m.field_evidence.get("entity_raw") is not None  # unified key (B06)
    assert m.field_evidence.get("unit_raw") is not None  # unified key (B06)


def test_metric_is_consistent_across_columns() -> None:
    table, evidences = _load_table()
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    assert all(m.metric == "MIC" for m in measurements)

def _axis_table(*, drugs_on_rows: bool) -> "tuple[Table, dict[str, str]]":
    """2x2 S/R matrix + labels; either orientation, caption says sensitivity."""
    drugs = ["Tobramycin", "Meropenem"]
    strains = ["17 0129 1317", "S. aureus 17 0042"]
    calls = [["R", "S"], ["S", "R"]]
    cells = []
    header_ids = []

    def add(row, col, text, role):
        cid = f"t:r{row}c{col}"
        cells.append(TableCell(
            cell_id=cid, row_index=row, column_index=col,
            text_raw=text, header_role=role,
        ))
        if role != "data":
            header_ids.append(cid)

    add(0, 0, "corner", "both")
    for j in range(2):
        # row 0 holds the COLUMN headers: classic → drugs, flipped → strains
        add(0, j + 1, strains[j] if drugs_on_rows else drugs[j], "column")
    for i in range(2):
        # column 0 holds the ROW headers: classic → strains, flipped → drugs
        add(i + 1, 0, drugs[i] if drugs_on_rows else strains[i], "row")
        for j in range(2):
            add(i + 1, j + 1, calls[i][j], "data")
    table = Table(
        table_id="t", page_number=9, row_count=3, column_count=3,
        cells=cells, header_cell_ids=header_ids,
        caption="TABLE 3 | Antibiotic sensitivity data",
    )
    evidences = {c.cell_id: f"ev-{c.cell_id}" for c in table.cells}
    return table, evidences


def test_susceptibility_classic_axis_drugs_in_columns() -> None:
    table, evidences = _axis_table(drugs_on_rows=False)
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    assert len(measurements) == 4
    m = measurements[0]
    assert m.metric == "antimicrobial_susceptibility"
    assert m.entity_raw == "Tobramycin"
    assert m.target_raw == "17 0129 1317"
    assert m.value.kind == "categorical"
    assert m.value.qualifier == "R"


def test_susceptibility_flipped_axis_drugs_in_rows() -> None:
    """Heat-matrix layout (geometry-reconstructed paper tables): drug names
    row-side with organism/isolate headers on columns must still bind
    entity=drug and target=strain via vocabulary evidence."""
    table, evidences = _axis_table(drugs_on_rows=True)
    measurements = extract_table_measurements(
        table, document_id="sha256:fixture", evidences=evidences
    )
    assert len(measurements) == 4
    for m in measurements:
        assert m.metric == "antimicrobial_susceptibility"
        # entity is ALWAYS the drug, whichever axis carries it
        assert m.entity_raw in ("Tobramycin", "Meropenem")
        assert m.target_raw in ("17 0129 1317", "S. aureus 17 0042")
    assert {m.entity_raw for m in measurements} == {"Tobramycin", "Meropenem"}
    # field evidence still points at the REAL source cells of each axis
    m0 = measurements[0]
    assert len(m0.field_evidence["value"]) == 1
    assert m0.field_evidence.get("entity_raw") and m0.field_evidence.get("target_raw")
