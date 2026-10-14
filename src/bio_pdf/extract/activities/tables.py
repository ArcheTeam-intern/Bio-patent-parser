"""Activity table → measurement extraction (plan B06, header-path driven).

Every data cell is judged from its own header PATH (child metric, parent
target, row label entity) — the previous table-level metric override made
``MIC | MBC`` columns both report ``MBC``. Extraction is scoped by the
table's classified kind:

- ``activity_numeric`` → one Measurement per numeric data cell;
- ``susceptibility``   → categorical S/R/I results (entity = drug column,
  target = strain row);
- ``inventory``/``scoring``/``sequence``/``assay_conditions``/
  ``unclassified`` → NO Measurements (structure is still preserved and
  surfaced as an issue by the caller).

Field evidence keys are unified: ``value``/``metric``/``unit_raw``/
``entity_raw``/``target_raw``/``assay_id`` — every referenced id comes
from the ``evidences`` map built from REAL spans (the caller registers
cell spans first and passes ``cell_id → evidence_id``).
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import Measurement, Table, TableCell
from bio_pdf.extract.activities.header_graph import build_header_paths
from bio_pdf.extract.activities.metrics import METRIC_REGISTRY, lookup_metric
from bio_pdf.extract.activities.table_classifier import TableKind, classify_table
from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value

#: Header like ``MIC (µg/mL), E. coli`` or ``IC50 (nM)``.
_HEADER_RE = re.compile(
    r"^\s*([A-Za-z0-9_/%₅₀]+)\s*(?:\(([^)]*)\))?\s*,?\s*(.*?)\s*$"
)

_ROW_LABEL_HEADERS = {"peptide", "compound", "antibody", "entity", "variant", "construct", "id", "name", "species/strain", "strain"}

_SRI_VALUE = re.compile(r"^\s*(S|R|I|SDD)\s*$")
_NUMERICISH = re.compile(r"^\s*(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?\s*$")


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


def _resolve_column_context(
    path_cells: list[TableCell],
) -> tuple[str | None, str | None, TableCell | None, str | None, TableCell | None, TableCell | None]:
    """Resolve the column context for a data cell.

    Returns (metric, metric_raw, metric_cell, unit_raw, unit_cell,
    target_cell, target_raw).

    Walks the column path parent→child: the DEEPEST cell whose text carries
    a registered metric becomes the metric; the remaining parent cells form
    the target. A unit is taken from the metric cell's parentheses, else
    inherited from a parent header that itself carries ``(...`` — the
    footnote/parent inheritance rule without inventing values.
    """

    metric: str | None = None
    metric_raw: str | None = None
    metric_cell: TableCell | None = None
    unit_raw: str | None = None
    unit_cell: TableCell | None = None
    target_cell: TableCell | None = None
    target_parts: list[str] = []

    for cell in path_cells:
        text = cell.text_raw.strip()
        parsed = parse_table_header(text)
        hit = lookup_metric(parsed.get("metric") or "")
        if hit is not None:
            canonical, raw = hit
            metric = canonical
            metric_raw = parsed.get("metric") or raw
            metric_cell = cell
            if parsed.get("unit"):
                unit_raw = parsed["unit"]
                unit_cell = cell
            if not target_parts and parsed.get("target"):
                target_parts.append(parsed["target"])
        else:
            if text.strip() and not _is_row_label_header(text):
                target_parts.append(text.strip())
            # Unit inheritance from a parent header lacking a metric word.
            if unit_raw is None and parsed.get("unit"):
                unit_raw = parsed["unit"]
                unit_cell = cell

    if unit_raw is None and metric is not None:
        # Registry fallback: a metric's single canonical unit is inherited
        # only when the registry defines exactly one; otherwise the unit
        # stays missing (validated, not fabricated).
        definition = METRIC_REGISTRY.get(metric)
        if definition is not None and len(definition.allowed_units) == 1:
            unit_raw = definition.allowed_units[0]

    if target_parts:
        # Wrap in a synthetic carrier via the LAST parent header when one
        # exists; otherwise keep the text only.
        parent_cells = [c for c in path_cells if c is not metric_cell]
        target_cell = parent_cells[-1] if parent_cells else None

    target_raw = ", ".join(dict.fromkeys(target_parts)) or None
    return (
        metric,
        metric_raw,
        metric_cell,
        unit_raw,
        unit_cell,
        target_cell if target_raw else None,
        target_raw,
    )


def extract_table_measurements(
    table: Table,
    *,
    document_id: str,
    evidences: dict[str, str],
    evidence_kind: str = "undetermined",
    metric: str | None = None,  # accepted for compatibility; NOT an override
) -> list[Measurement]:
    """Extract measurements from a :class:`Table` by classified kind.

    Args:
        table: The normalised table.
        document_id: Canonical document id.
        evidences: ``cell_id → evidence_id`` built from REAL spans (the
            caller registers the spans and passes the map).
        evidence_kind: Initial :data:`EvidenceKind`; callers pass
            "undetermined" — ``measured`` is never asserted without method
            evidence.
        metric: Legacy parameter, IGNORED (kept so old call sites do not
            silently reintroduce the table-level override).
    """

    del metric  # B06: no table-level metric override, ever.

    cell_index = {c.cell_id: c for c in table.cells}
    kind = classify_table(table)
    if kind is TableKind.SUSCEPTIBILITY:
        return _extract_susceptibility(
            table, cell_index, document_id=document_id, evidences=evidences,
            evidence_kind=evidence_kind,
        )
    if kind is not TableKind.ACTIVITY_NUMERIC:
        return []

    def _ev(cell: TableCell | None) -> list[str]:
        if cell is None:
            return []
        eid = evidences.get(cell.cell_id)
        return [eid] if eid else []

    measurements: list[Measurement] = []
    for path in build_header_paths(table):
        data_cell = path.data_cell
        value_text = data_cell.text_raw.strip()
        if not value_text:
            continue
        if not (_NUMERICISH.match(value_text) or value_text.upper() in ("ND", "NA", "N/A", "-")):
            # A non-numeric cell in a numeric panel (comment/unit noise):
            # skip — never turn text into a fabricated value.
            continue

        (
            canonical,
            metric_raw,
            metric_cell,
            unit_raw,
            unit_cell,
            target_cell,
            target_raw,
        ) = _resolve_column_context(path.column_headers)
        if canonical is None:
            continue  # no registered metric on this column → no measurement
        # A trailing target inside the metric header ("MIC (µM), E. coli").
        if not target_raw and metric_cell is not None:
            target_raw = parse_table_header(metric_cell.text_raw.strip()).get("target")

        # Entity: the deepest row header (row label column).
        entity_cell = path.row_headers[-1] if path.row_headers else None
        entity_raw = entity_cell.text_raw.strip() if entity_cell is not None else None

        value = parse_activity_value(value_text)
        unit_normalized = normalize_unit(unit_raw) if unit_raw else None

        field_evidence: dict[str, list[str]] = {"value": _ev(data_cell)}
        if metric_cell is not None:
            field_evidence["metric"] = _ev(metric_cell)
        if unit_cell is not None:
            field_evidence["unit_raw"] = _ev(unit_cell)
        if entity_cell is not None:
            field_evidence["entity_raw"] = _ev(entity_cell)
        if target_cell is not None:
            field_evidence["target_raw"] = _ev(target_cell)

        evidence_ids: list[str] = []
        for ids in field_evidence.values():
            for eid in ids:
                if eid not in evidence_ids:
                    evidence_ids.append(eid)
        if not evidence_ids:
            continue  # no loadable evidence → no record (never fake one)

        measurements.append(
            Measurement(
                measurement_id=f"{document_id}:meas:{table.table_id}:r{data_cell.row_index}c{data_cell.column_index}",
                document_id=document_id,
                entity_raw=entity_raw,
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
                evidence_ids=evidence_ids,
                checks=[],
                validation_state="unchecked",
                acceptance_state="candidate",
            )
        )
    return measurements


def _extract_susceptibility(
    table: Table,
    cell_index: dict[str, TableCell],
    *,
    document_id: str,
    evidences: dict[str, str],
    evidence_kind: str,
) -> list[Measurement]:
    """S/R/I matrix: entity = DRUG (column header), target = STRAIN (row)."""

    def _ev(cell: TableCell | None) -> list[str]:
        if cell is None:
            return []
        eid = evidences.get(cell.cell_id)
        return [eid] if eid else []

    measurements: list[Measurement] = []
    for path in build_header_paths(table):
        data_cell = path.data_cell
        match = _SRI_VALUE.match(data_cell.text_raw)
        if match is None:
            continue
        qualifier = match.group(1).upper()
        drug_cell = path.column_headers[-1] if path.column_headers else None
        strain_cell = path.row_headers[-1] if path.row_headers else None
        if drug_cell is None or strain_cell is None:
            continue  # axes unknown → do not transpose or guess

        field_evidence: dict[str, list[str]] = {"value": _ev(data_cell)}
        field_evidence["entity_raw"] = _ev(drug_cell)
        field_evidence["target_raw"] = _ev(strain_cell)
        evidence_ids: list[str] = []
        for ids in field_evidence.values():
            for eid in ids:
                if eid not in evidence_ids:
                    evidence_ids.append(eid)
        if not evidence_ids:
            continue

        measurements.append(
            Measurement(
                measurement_id=f"{document_id}:meas:{table.table_id}:r{data_cell.row_index}c{data_cell.column_index}",
                document_id=document_id,
                entity_raw=drug_cell.text_raw.strip(),
                sequence_ids=[],
                assay_id=None,
                metric="antimicrobial_susceptibility",
                metric_raw=drug_cell.text_raw.strip(),
                value={"kind": "categorical", "qualifier": qualifier, "raw": qualifier},  # type: ignore[arg-type]
                unit_raw=None,
                unit_normalized=None,
                target_raw=strain_cell.text_raw.strip(),
                conditions=[],
                evidence_kind=evidence_kind,  # type: ignore[arg-type]
                field_evidence=field_evidence,
                evidence_ids=evidence_ids,
                checks=[],
                validation_state="unchecked",
                acceptance_state="candidate",
            )
        )
    return measurements


__all__ = ["extract_table_measurements", "parse_table_header"]
