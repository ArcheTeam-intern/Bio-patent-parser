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

#: A numeric cell carrying its unit inline (">64 μg/ml"). Patent tables
#: repeat the unit on every value instead of declaring it in the header.
_VALUE_WITH_UNIT = re.compile(
    r"^\s*((?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?)\s*"
    r"((?:[a-zA-Zμµ]+(?:\s*/\s*[a-zA-Zμµ]+)?)|[%°])\s*$"
)

#: A serial-number COLUMN header ("Strains", "Isolate number", "#"). Its
#: values are row indices, never measurements.
_ROW_NUMBER_HEADER = re.compile(
    r"^\s*(?:strains?|isolates?(?:\s*(?:no\.?|number|id))?|no\.?|#|"
    r"number|id|序号|编号)\s*$",
    re.IGNORECASE,
)

#: G07/P02: experiment-condition patterns (time / temperature / dose /
#: method / atmosphere) for header cells, captions and table notes.
#: Deliberately small and auditable — no speculative parsing; every match
#: keeps its source text so a reviewer can check it.
_CONDITION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "time",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*"
            r"(h|hr|hrs|hour|hours|min|mins|minute|minutes|day|days)(?![A-Za-z0-9])"
        ),
    ),
    (
        "temperature",
        re.compile(r"(?i)(?<![A-Za-z0-9])(\d{1,3})\s*°?\s*C(?![A-Za-z0-9])"),
    ),
    (
        "dose",
        # I06/P02: SI case is SEMANTIC — the unit token is matched
        # case-SENSITIVELY (µM/mM/nM/M/mg…), so µm (length) is never a
        # concentration. Only the digit boundary lookarounds are generic.
        re.compile(
            r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*"
            r"(µg|μg|ug|mg|ng|pg|kg|mM|µM|μM|uM|nM|M|%)(?![A-Za-z0-9])"
        ),
    ),
    (
        "method",
        re.compile(
            r"(?i)(broth microdilution|microdilution|disc diffusion|disk diffusion"
            r"|checkerboard|time[- ]kill|serial dilution)"
        ),
    ),
    ("atmosphere", re.compile(r"(?i)(aerobic|anaerobic|\d+%\s*CO2)")),
]


def conditions_from_text(
    text: str, *, source: str, evidence: str = "", block_id: str = ""
) -> list[dict[str, str]]:
    """G07: structured conditions found in one piece of table-adjacent text."""

    out: list[dict[str, str]] = []
    for kind, pattern in _CONDITION_PATTERNS:
        for match in pattern.finditer(text or ""):
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            condition = {"kind": kind, "value": value, "source": source, "evidence": evidence}
            if block_id:
                condition["block_id"] = block_id
            out.append(condition)
    return out


def merge_conditions(
    *groups: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Dedupe conditions by (kind, value) — I06: CASE-SENSITIVE (µM ≠ µm)."""

    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for condition in group or []:
            key = (
                str(condition.get("kind") or ""),
                " ".join(str(condition.get("value") or "").split()),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append({str(k): str(v) for k, v in condition.items()})
    return merged

#: Vocabulary evidence for the susceptibility-matrix axis decision: an
#: antibiotic-name side vs an organism/isolate-label side. Suffix-driven so
#: the same paper's drug families match without a maintained drug list.
_DRUG_HINT = re.compile(
    r"(mycin|micin|cillin|cycline|floxacin|penem|bactam|clavulanate|azole|"
    r"colistin|polymyxin|daptomycin|linezolid|vancomy|bacitracin|"
    r"chloramphen|clindamy|trimethoprim|methoxazole|aztreonam|cef[a-z]+|"
    r"ceph[a-z]+|quinupristin|nitrofurantoin|rifamp|tetracyclin|glycyclin)",
    re.IGNORECASE,
)
_STRAIN_HINT = re.compile(
    r"(coccus|bacill|aureus|coli\b|baumanii|pneumoniae|aeruginosa|faecalis|"
    r"faecium|klebsiell|enterobact|proteus|serratia|staphylococc|streptococc|"
    r"enterococc|neisseri|haemophil|acinetobact|pseudomonad|salmonell|"
    r"shigell|candida|aspergill|trichophyt|malassez|mycoplasma|ureaplasma|"
    r"acholeplasma|spiroplasma|escherich|listeria|clostrid|lactobacill|"
    r"bacteroid|prevotella|porphyromon|vulgaris|typhimurium)",
    re.IGNORECASE,
)
#: Isolate accession labels such as "17 0129 1317".
_ISOLATE_RE = re.compile(r"^\d{1,4}(?:\s+\d{2,6}){1,4}$")


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


def _caption_metric(table: Table) -> tuple[str, str] | None:
    """The metric a table DECLARES in its caption, if any.

    Patents write "Table 6. Minimum inhibitory concentration (MICs) of
    peptide variants…" and put strain/peptide names in the headers. This
    helper is what the extraction call sites pass as ``caption_metric``
    so those tables yield measurements instead of being dropped.
    """

    caption = str(getattr(table, "caption", "") or "").strip()
    if not caption:
        return None
    from bio_pdf.extract.activities.metrics import lookup_metric

    return lookup_metric(caption)


def extract_table_measurements(
    table: Table,
    *,
    document_id: str,
    evidences: dict[str, str],
    evidence_kind: str = "undetermined",
    metric: str | None = None,  # accepted for compatibility; NOT an override
    context_conditions: list[dict[str, str]] | None = None,
    caption_metric: tuple[str, str] | None = None,
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
        context_conditions: G07/P02 — conditions resolved from the table's
            caption / referenced notes by the caller (which owns the
            document blocks). Header-cell conditions are added HERE from
            the measurement's own header path.
        caption_metric: ``(canonical, raw)`` parsed from the table CAPTION
            ("Table 6. Minimum inhibitory concentration (MICs)…"). Used
            ONLY when the column headers carry no metric of their own —
            the patent layout puts the metric in the title and the strain
            or peptide name in the header. A header metric always wins, so
            a multi-metric table is never flattened by this fallback.
    """

    del metric  # B06: no table-level metric override, ever.

    cell_index = {c.cell_id: c for c in table.cells}
    kind = classify_table(table)
    if kind is TableKind.SUSCEPTIBILITY:
        return _extract_susceptibility(
            table, cell_index, document_id=document_id, evidences=evidences,
            evidence_kind=evidence_kind,
            context_conditions=context_conditions,
        )
    if kind is not TableKind.ACTIVITY_NUMERIC:
        return []

    def _ev(cell: TableCell | None) -> list[str]:
        if cell is None:
            return []
        eid = evidences.get(cell.cell_id)
        return [eid] if eid else []

    def _path_conditions(path) -> list[dict[str, str]]:
        """G07: conditions from this measurement's own header path."""

        found: list[dict[str, str]] = []
        column_ids = {id(c) for c in path.column_headers}
        for cell in list(path.column_headers) + list(path.row_headers):
            source = "column_header" if id(cell) in column_ids else "row_header"
            found.extend(
                conditions_from_text(
                    cell.text_raw or "",
                    source=source,
                    evidence=evidences.get(cell.cell_id, ""),
                )
            )
        return found

    measurements: list[Measurement] = []
    for path in build_header_paths(table):
        data_cell = path.data_cell
        value_text = data_cell.text_raw.strip()
        if not value_text:
            continue
        # Patent values carry their unit inline (">64 μg/ml"): peel it off
        # so the numeric parser sees the bare value and the unit travels
        # separately (it is evidence on the VALUE cell, not the header).
        cell_unit: str | None = None
        inline = _VALUE_WITH_UNIT.match(value_text)
        if inline is not None:
            value_text = inline.group(1).strip()
            cell_unit = inline.group(2).strip()
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
            if caption_metric is None:
                continue  # no registered metric anywhere → no measurement
            # Caption-declared metric fallback (patent layout): the metric
            # lives in the title, so the deepest column header names the
            # other axis (strain / peptide) and the row label names the
            # first one.
            canonical, metric_raw = caption_metric
            if path.column_headers:
                target_cell = path.column_headers[-1]
                target_raw = target_cell.text_raw.strip() or None
            # A serial-number column ("Strains: 1, 2, 3…") is a row index,
            # not a data column — its integers are isolate numbers, never
            # MIC values. Dropping it here keeps the fabricated records out
            # (the live EP3423477B1 table produced 45 of them).
            if target_raw and _ROW_NUMBER_HEADER.match(target_raw):
                continue
        # A trailing target inside the metric header ("MIC (µM), E. coli").
        if not target_raw and metric_cell is not None:
            target_raw = parse_table_header(metric_cell.text_raw.strip()).get("target")

        # Entity: the deepest row header (row label column).
        entity_cell = path.row_headers[-1] if path.row_headers else None
        entity_raw = entity_cell.text_raw.strip() if entity_cell is not None else None

        # Axis orientation. A MIC matrix can be laid out either way:
        #   row = compound, column = strain   (Table 6)  → correct as-is
        #   row = strain,   column = peptide  (Table 4b) → flipped
        # The organism vocabulary decides: when the ROW label is an
        # organism and the column label is not, the peptide/compound is the
        # entity and the organism is the target. No organism on either side
        # → leave the natural reading untouched.
        if (
            entity_raw
            and _STRAIN_HINT.search(entity_raw)
            and not (target_raw and _STRAIN_HINT.search(target_raw))
        ):
            entity_raw, target_raw = target_raw, entity_raw
            entity_cell, target_cell = target_cell, entity_cell

        value = parse_activity_value(value_text)
        if unit_raw is None and cell_unit is not None:
            unit_raw = cell_unit
            unit_cell = data_cell
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
                conditions=merge_conditions(context_conditions, _path_conditions(path)),
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
    context_conditions: list[dict[str, str]] | None = None,
) -> list[Measurement]:
    """S/R/I matrix: entity = DRUG, target = STRAIN/isolate.

    The classic layout puts drugs on the column headers; many papers (a
    rotated-header heat matrix, often geometry-reconstructed) put drugs on
    the row labels instead. The axis is decided by vocabulary evidence —
    antibiotic-name suffixes vs organism/isolate patterns — and flipped
    only when the row side is decisively drug-like (>=2 matches and more
    than the column side). No evidence, no flip: nothing is guessed.
    """

    def _ev(cell: TableCell | None) -> list[str]:
        if cell is None:
            return []
        eid = evidences.get(cell.cell_id)
        return [eid] if eid else []

    def _drug_score(labels: list[str]) -> int:
        return sum(1 for t in labels if _DRUG_HINT.search(t))

    row_score = _drug_score([c.text_raw for c in table.cells if c.header_role == "row"])
    column_score = _drug_score([c.text_raw for c in table.cells if c.header_role == "column"])
    # H04: a SINGLE unambiguous drug row (e.g. the reviewer's 2x6 case:
    # Vancomycin as the only row label, strain-like column headers) must
    # flip too — requiring >=2 drug rows lost the axes for small tables.
    flipped = (row_score >= 1 and column_score == 0) or (
        row_score >= 2 and row_score > column_score
    )

    measurements: list[Measurement] = []
    for path in build_header_paths(table):
        data_cell = path.data_cell
        match = _SRI_VALUE.match(data_cell.text_raw)
        if match is None:
            continue
        qualifier = match.group(1).upper()
        if flipped:
            drug_cell = path.row_headers[-1] if path.row_headers else None
            strain_cell = path.column_headers[-1] if path.column_headers else None
        else:
            drug_cell = path.column_headers[-1] if path.column_headers else None
            strain_cell = path.row_headers[-1] if path.row_headers else None
        if drug_cell is None or strain_cell is None:
            continue  # axes unknown → do not transpose or guess
        # H04: a value cell as the "drug" axis means the header row was
        # mangled (S/R/I leaked into it) — never emit a fabricated entity.
        if _SRI_VALUE.match(drug_cell.text_raw.strip()):
            continue
        if _SRI_VALUE.match(strain_cell.text_raw.strip()):
            continue

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
                # G07/P02: caption/note conditions resolved by the caller.
                conditions=merge_conditions(context_conditions),
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
