"""Activity table → measurement extraction.

A table row is interpreted as: first column = row label (entity), remaining
columns = data cells whose column header carries ``metric (unit), target``.
Each data cell becomes one :class:`Measurement` with the entity, target and
unit taken from its row/column headers — so a value can never be silently
re-assigned to the wrong row.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import Measurement, Table
from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value

#: Header like ``MIC (µg/mL), E. coli`` or ``IC50 (nM)``.
_HEADER_RE = re.compile(
    r"^\s*([A-Za-z0-9_/%]+)\s*(?:\(([^)]*)\))?\s*,?\s*(.*?)\s*$"
)

_ROW_LABEL_HEADERS = {"peptide", "compound", "antibody", "entity", "variant", "construct", "id", "name"}


def parse_table_header(text: str) -> dict[str, str | None]:
    """Parse a column header into ``{metric, unit, target}``.

    ``MIC (µg/mL), E. coli`` → ``metric=MIC, unit=µg/mL, target=E. coli``.
    Missing pieces are ``None``.
    """

    text = text.strip()
    m = _HEADER_RE.match(text)
    if not m:
        return {"metric": text or None, "unit": None, "target": None}

    metric = m.group(1).strip() or None
    unit = m.group(2).strip() if m.group(2) else None
    target = m.group(3).strip() if m.group(3) else None
    return {"metric": metric, "unit": unit, "target": target}


def _is_row_label_header(text: str) -> bool:
    return text.strip().lower() in _ROW_LABEL_HEADERS


def _build_cell_index(table: Table) -> dict[tuple[int, int], Any]:
    index: dict[tuple[int, int], Any] = {}
    for cell in table.cells:
        index[(cell.row_index, cell.column_index)] = cell
    return index


def extract_table_measurements(
    table: Table,
    *,
    document_id: str,
    evidences: dict[str, str],
    evidence_kind: str = "undetermined",
    metric: str | None = None,
) -> list[Measurement]:
    """Extract measurements from a :class:`Table`.

    Args:
        table: The normalised table.
        document_id: Canonical document id.
        evidences: Mapping of ``cell_id → evidence_id`` used to anchor each
            value/unit/entity/target back to the source cell.
        evidence_kind: Initial :data:`EvidenceKind` for the measurements.
            Callers pass "undetermined" — the handler never asserts
            ``measured`` without method evidence (plan T09).
        metric: Canonical metric name pre-validated by the caller via
            :func:`bio_pdf.extract.activities.metrics.classify_table_metric`.
            When given, it overrides per-column header metric guessing and
            the source spelling is kept as ``metric_raw``.

    Returns:
        A list of :class:`Measurement`, one per data cell.
    """

    cell_index = _build_cell_index(table)
    if not cell_index:
        return []

    rows = sorted({r for (r, _c) in cell_index})
    cols = sorted({c for (_r, c) in cell_index})

    # Identify header row: the row whose non-label columns have parseable
    # metric/unit headers. Fall back to row 0.
    header_row = rows[0]
    for row in rows[:3]:
        header_cells = [cell_index.get((row, c)) for c in cols if c > 0]
        if any(
            cell is not None and (parse_table_header(cell.text_raw)["metric"] is not None)
            for cell in header_cells
        ):
            header_row = row
            break

    # Column headers (skip column 0 which is the row label).
    column_headers: dict[int, dict[str, str | None]] = {}
    for c in cols:
        if c == 0:
            continue
        header_cell = cell_index.get((header_row, c))
        if header_cell is not None:
            column_headers[c] = parse_table_header(header_cell.text_raw)
        else:
            column_headers[c] = {"metric": None, "unit": None, "target": None}

    measurements: list[Measurement] = []
    for row in rows:
        if row == header_row:
            continue
        row_label_cell = cell_index.get((row, 0))
        entity = row_label_cell.text_raw.strip() if row_label_cell is not None else ""
        if not entity:
            continue

        for c in cols:
            if c == 0:
                continue
            cell = cell_index.get((row, c))
            if cell is None:
                continue
            value_text = cell.text_raw.strip()
            if not value_text:
                continue

            header = column_headers.get(c, {"metric": None, "unit": None, "target": None})
            header_metric = header.get("metric")
            if metric is not None:
                # Caller-validated canonical metric wins; header spelling
                # stays as metric_raw evidence.
                canonical = metric
                metric_raw = header_metric
            else:
                from bio_pdf.extract.activities.metrics import lookup_metric

                hit = lookup_metric(header_metric or "") if header_metric else None
                if hit is None:
                    continue  # unclassified column: no fabricated Measurements
                canonical, _ = hit
                metric_raw = header_metric
            unit_raw = header.get("unit")
            target_raw = header.get("target")

            value = parse_activity_value(value_text)
            unit_normalized = normalize_unit(unit_raw) if unit_raw else None

            measurement_id = f"{document_id}:meas:{table.table_id}:r{row}c{c}"

            field_evidence: dict[str, list[str]] = {}
            def _ev(cid: str) -> str:
                return evidences.get(cid, f"{document_id}:cell:{cid}")

            field_evidence["value"] = [_ev(cell.cell_id)]
            if row_label_cell is not None:
                field_evidence["entity_id"] = [_ev(row_label_cell.cell_id)]
            if header_row != row:
                header_cell = cell_index.get((header_row, c))
                if header_cell is not None:
                    field_evidence["unit"] = [_ev(header_cell.cell_id)]
                    field_evidence["target_raw"] = [_ev(header_cell.cell_id)]

            measurements.append(
                Measurement(
                    measurement_id=measurement_id,
                    document_id=document_id,
                    # The row label is *surface text*, not a linked entity —
                    # entity linking (plan T11) fills entity_id.
                    entity_raw=entity,
                    sequence_ids=[],
                    assay_id=None,
                    metric=canonical,
                    metric_raw=metric_raw,
                    value=value,
                    unit_raw=unit_raw,
                    unit_normalized=unit_normalized,
                    target_raw=target_raw,
                    conditions=[],
                    evidence_kind=evidence_kind,  # type: ignore[arg-type]
                    field_evidence=field_evidence,
                    evidence_ids=[_ev(cell.cell_id)],
                    checks=[],
                    validation_state="unchecked",
                    acceptance_state="candidate",
                )
            )

    return measurements


__all__ = ["extract_table_measurements", "parse_table_header"]