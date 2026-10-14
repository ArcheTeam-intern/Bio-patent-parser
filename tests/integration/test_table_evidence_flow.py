"""B06: table metric columns, evidence chain, hierarchical headers.

Counterexamples from the post-M1 review and the remediation plan:

- ``Peptide | MIC (µM) | MBC (µM)`` with ``A | 8 | 16`` produced
  ``MBC=8``/``MBC=16`` (the table-level metric override replaced every
  column's own header);
- record evidence ids (``doc:cell:c11``) did not match the spans actually
  registered (``ev-…``), so the API could not load field evidence;
- merged headers (Peptide[rowspan=2] | E.coli[colspan=2] | S.aureus…) must
  give each data cell its parent target and child metric;
- an S/R/I susceptibility matrix must yield categorical results with
  entity=drug (column header) and target=strain (row header), never
  transposed and never converted to MIC numbers;
- inventory/scoring tables must not fabricate activity.
"""

from __future__ import annotations

import pytest

from bio_pdf.contracts import Table, TableCell
from bio_pdf.extract.activities.table_classifier import TableKind, classify_table
from bio_pdf.extract.activities.tables import extract_table_measurements


def _cell(cid: str, r: int, c: int, text: str, role: str = "data", **kw) -> TableCell:
    return TableCell(
        cell_id=cid, row_index=r, column_index=c, text_raw=text, header_role=role, **kw
    )


def _table(cells: list[TableCell], header_ids: list[str], **kw) -> Table:
    rows = max(c.row_index + c.rowspan for c in cells)
    cols = max(c.column_index + c.colspan for c in cells)
    return Table(
        table_id=kw.pop("table_id", "t1"),
        page_number=kw.pop("page_number", 1),
        row_count=rows,
        column_count=cols,
        cells=cells,
        header_cell_ids=header_ids,
        **kw,
    )


# ---- 1. MIC/MBC: each column keeps its own metric -------------------------

def test_mic_mbc_columns_keep_their_own_metric() -> None:
    cells = [
        _cell("c00", 0, 0, "Peptide", "column"),
        _cell("c01", 0, 1, "MIC (µM)", "column"),
        _cell("c02", 0, 2, "MBC (µM)", "column"),
        _cell("c10", 1, 0, "A", "row"),
        _cell("c11", 1, 1, "8"),
        _cell("c12", 1, 2, "16"),
    ]
    table = _table(cells, ["c00", "c01", "c02"])
    assert classify_table(table) is TableKind.ACTIVITY_NUMERIC

    evidences = {c.cell_id: f"ev-{c.cell_id}" for c in cells}
    measurements = extract_table_measurements(
        table, document_id="doc", evidences=evidences
    )
    by_metric = {m.metric: m for m in measurements}
    assert by_metric["MIC"].value.number == "8"
    assert by_metric["MBC"].value.number == "16"
    assert by_metric["MIC"].unit_raw == "µM"
    assert by_metric["MBC"].unit_raw == "µM"


def test_every_field_evidence_id_exists_in_the_index() -> None:
    cells = [
        _cell("c00", 0, 0, "Peptide", "column"),
        _cell("c01", 0, 1, "MIC (µM)", "column"),
        _cell("c10", 1, 0, "A", "row"),
        _cell("c11", 1, 1, "8"),
    ]
    table = _table(cells, ["c00", "c01"])
    evidences = {c.cell_id: f"ev-{c.cell_id}" for c in cells}
    measurements = extract_table_measurements(
        table, document_id="doc", evidences=evidences
    )
    assert len(measurements) == 1
    m = measurements[0]
    known = set(evidences.values())
    for field, ids in m.field_evidence.items():
        for eid in ids:
            assert eid in known, f"{field} references unknown evidence {eid}"
    assert set(m.evidence_ids) <= known | set(evidences.values())
    # Field keys are unified (no legacy 'unit'/'entity_id' mix).
    assert "unit_raw" in m.field_evidence
    assert "value" in m.field_evidence


# ---- 2. hierarchical headers (rowspan/colspan) ----------------------------

def _merged_header_table() -> Table:
    cells = [
        _cell("h_pep", 0, 0, "Peptide", "both", rowspan=2),
        _cell("h_ec", 0, 1, "E. coli", "column", colspan=2),
        _cell("h_sa", 0, 3, "S. aureus", "column", colspan=2),
        _cell("h_ec_mic", 1, 1, "MIC (µM)", "column"),
        _cell("h_ec_mbc", 1, 2, "MBC (µM)", "column"),
        _cell("h_sa_mic", 1, 3, "MIC (µM)", "column"),
        _cell("h_sa_mbc", 1, 4, "MBC (µM)", "column"),
        _cell("d_a", 2, 0, "A", "row"),
        _cell("d1", 2, 1, ">128"),
        _cell("d2", 2, 2, "ND"),
        _cell("d3", 2, 3, "8"),
        _cell("d4", 2, 4, "16"),
    ]
    header_ids = [c.cell_id for c in cells if c.header_role in ("column", "both")]
    return _table(cells, header_ids)


def test_merged_headers_give_parent_target_and_child_metric() -> None:
    table = _merged_header_table()
    evidences = {c.cell_id: f"ev-{c.cell_id}" for c in table.cells}
    measurements = extract_table_measurements(
        table, document_id="doc", evidences=evidences
    )
    assert len(measurements) == 4
    tuples = {(m.target_raw, m.metric): m for m in measurements}
    assert ("E. coli", "MIC") in tuples
    assert ("E. coli", "MBC") in tuples
    assert ("S. aureus", "MIC") in tuples
    assert ("S. aureus", "MBC") in tuples

    ec_mic = tuples[("E. coli", "MIC")]
    assert ec_mic.value.operator == ">"
    assert ec_mic.value.number == "128"
    assert tuples[("E. coli", "MBC")].value.kind == "categorical"

    sa_mic = tuples[("S. aureus", "MIC")]
    assert sa_mic.value.number == "8"
    # Target evidence points at the PARENT header, metric at the child.
    assert sa_mic.field_evidence.get("target_raw") == ["ev-h_sa"]
    assert sa_mic.field_evidence.get("metric") == ["ev-h_sa_mic"]
    ec_mic_ev = tuples[("E. coli", "MIC")].field_evidence
    assert ec_mic_ev.get("target_raw") == ["ev-h_ec"]
    assert ec_mic_ev.get("metric") == ["ev-h_ec_mic"]


# ---- 3. susceptibility matrix ---------------------------------------------

def _susceptibility_table() -> Table:
    cells = [
        _cell("s00", 0, 0, "Species/strain", "column"),
        _cell("s01", 0, 1, "Benzylpenicillin", "column"),
        _cell("s02", 0, 2, "Cefuroxime axetil", "column"),
        _cell("s10", 1, 0, "E. coli A", "row"),
        _cell("s11", 1, 1, "R"),
        _cell("s12", 1, 2, "S"),
        _cell("s20", 2, 0, "S. capitis B", "row"),
        _cell("s21", 2, 1, "S"),
        _cell("s22", 2, 2, "I"),
    ]
    header_ids = ["s00", "s01", "s02"]
    return _table(cells, header_ids)


def test_susceptibility_matrix_is_categorical_with_correct_axes() -> None:
    table = _susceptibility_table()
    assert classify_table(table) is TableKind.SUSCEPTIBILITY

    evidences = {c.cell_id: f"ev-{c.cell_id}" for c in table.cells}
    measurements = extract_table_measurements(
        table, document_id="doc", evidences=evidences
    )
    assert len(measurements) == 4
    triples = {(m.entity_raw, m.target_raw): m for m in measurements}
    # entity = DRUG (column header), target = STRAIN (row header).
    assert ("Benzylpenicillin", "E. coli A") in triples
    assert ("Cefuroxime axetil", "S. capitis B") in triples
    ecoli_bp = triples[("Benzylpenicillin", "E. coli A")]
    assert ecoli_bp.metric == "antimicrobial_susceptibility"
    assert ecoli_bp.value.kind == "categorical"
    assert ecoli_bp.value.qualifier == "R"
    assert ecoli_bp.unit_raw is None  # not applicable, not fabricated


# ---- 4. inventory / scoring produce nothing -------------------------------

def test_inventory_table_produces_no_measurements() -> None:
    cells = [
        _cell("i00", 0, 0, "Strain", "column"),
        _cell("i01", 0, 1, "Used in", "column"),
        _cell("i02", 0, 2, "Notes", "column"),
        _cell("i10", 1, 0, "ATCC 25922", "row"),
        _cell("i11", 1, 1, "Figure 2", "data"),
        _cell("i12", 1, 2, "clinical isolate", "data"),
    ]
    table = _table(cells, ["i00", "i01", "i02"])
    assert classify_table(table) is TableKind.INVENTORY
    assert extract_table_measurements(table, document_id="doc", evidences={}) == []


def test_scoring_table_produces_no_measurements() -> None:
    cells = [
        _cell("g00", 0, 0, "Parameter", "column"),
        _cell("g01", 0, 1, "Animal ID", "column"),
        _cell("g02", 0, 2, "Score", "column"),
        _cell("g10", 1, 0, "Behavior", "row"),
        _cell("g11", 1, 1, "3", "data"),
        _cell("g12", 1, 2, "2", "data"),
    ]
    table = _table(cells, ["g00", "g01", "g02"])
    assert classify_table(table) is TableKind.SCORING
    assert extract_table_measurements(table, document_id="doc", evidences={}) == []


# ---- 5. registry: MIC50 is its own metric ---------------------------------

def test_mic50_is_not_silently_folded_to_mic() -> None:
    from bio_pdf.extract.activities.metrics import lookup_metric

    hit = lookup_metric("MIC50 (µM)")
    assert hit is not None
    canonical, raw = hit
    assert canonical == "MIC50"
    assert raw == "MIC50 (µM)"
