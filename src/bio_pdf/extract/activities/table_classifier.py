"""Table type classification (plan B06).

A table's TYPE decides which extraction applies — and a type is not a
license to fabricate activity:

- ``activity_numeric`` — MIC/MBC/IC50/… numeric panels;
- ``susceptibility`` — S/R/I matrix (drugs as columns, strains as rows);
- ``sequence`` — sequence/amino-acid/CDR listing columns;
- ``assay_conditions`` — method/condition tables (temperature/time/…);
- ``inventory`` — strain/compound catalogues (Used in/Notes);
- ``scoring`` — animal/clinical score tables;
- ``unclassified`` — everything else; cells produce NO Measurements.

Classification uses caption, header vocabulary, cell value shapes and the
header role structure — never a single metric word picked at random.
"""

from __future__ import annotations

import re
from enum import Enum

from bio_pdf.contracts import Table

from bio_pdf.extract.activities.metrics import METRIC_REGISTRY, is_non_metric_header, lookup_metric
from bio_pdf.extract.sequences.amino_acids import looks_like_residue_token


class TableKind(str, Enum):
    ACTIVITY_NUMERIC = "activity_numeric"
    SUSCEPTIBILITY = "susceptibility"
    SEQUENCE = "sequence"
    ASSAY_CONDITIONS = "assay_conditions"
    INVENTORY = "inventory"
    SCORING = "scoring"
    UNCLASSIFIED = "unclassified"


#: Inventory vocabulary (strain/compound catalogues).
_INVENTORY_WORDS = re.compile(
    r"\b(strain|used in|notes?|source|species|obtained from|atcc|isolate|"
    r"reference|catalogue|catalog)\b",
    re.IGNORECASE,
)
#: Scoring vocabulary.
_SCORING_WORDS = re.compile(
    r"\b(score|scoring|grading|parameter|animal id|severity|clinical sign|"
    r"behaviou?r)\b",
    re.IGNORECASE,
)
#: Susceptibility vocabulary.
_SUSCEPTIBILITY_WORDS = re.compile(
    r"\b(susceptib\w*|sensitivity|resistance|antibiotic|antibiogram|"
    r"s\s*/\s*r\s*/\s*i)\b",
    re.IGNORECASE,
)
#: Sequence-column vocabulary.
_SEQUENCE_WORDS = re.compile(
    r"\b(sequence|amino acid|residues?|cdr\d?|nucleotide|seq id)\b",
    re.IGNORECASE,
)
#: Assay-condition vocabulary.
_CONDITION_WORDS = re.compile(
    r"\b(temperature|time|ph|buffer|medium|incubat\w*|concentration|"
    r"condition|rpm|od600)\b",
    re.IGNORECASE,
)
#: A pure S/R/I value cell.
_SRI_VALUE = re.compile(r"^\s*(?:S|R|I|SDD)\s*$")

_NUMERIC_VALUE = re.compile(r"^\s*(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?\s*$")
#: A numeric cell that carries its unit inline (">64 μg/ml", "0.25 mg/L") —
#: the patent layout puts the unit on every value instead of the header.
_VALUE_WITH_UNIT = re.compile(
    r"^\s*(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?\s*"
    r"(?:[a-zA-Zμµ]+(?:\s*/\s*[a-zA-Zμµ]+)?|[%°])\s*$"
)


def _header_texts(table: Table) -> list[str]:
    header_ids = set(table.header_cell_ids or [])
    cells = [c for c in table.cells if c.cell_id in header_ids]
    if not cells:
        rows = sorted({c.row_index for c in table.cells})[:2]
        cells = [c for c in table.cells if c.row_index in rows]
    return [c.text_raw.strip() for c in cells if c.text_raw.strip()]


def _data_cells(table: Table) -> list:
    header_ids = set(table.header_cell_ids or [])
    data = [
        c
        for c in table.cells
        if c.cell_id not in header_ids and c.header_role == "data"
    ]
    if data:
        return data
    # G11/P07: tables whose roles were never normalised (td-only sources,
    # legacy exports) have NO role=data cells — counting only those made
    # every S/R/I matrix look empty. Fall back to all non-header cells.
    return [c for c in table.cells if c.cell_id not in header_ids and c.text_raw.strip()]


def _data_values(table: Table) -> list[str]:
    return [c.text_raw.strip() for c in _data_cells(table) if c.text_raw.strip()]


def classify_table(table: Table, *, caption: str | None = None) -> TableKind:
    """Classify a table; see module docstring."""

    headers = _header_texts(table)
    header_text = " | ".join(headers)
    caption_text = caption if caption is not None else (table.caption or "")
    # A table SPLIT across the fold loses the title to the earlier page; the
    # inherited title is the same physical table's declaration, so it decides
    # the metric exactly like a caption printed on this page would (the UI
    # still labels it as inherited).
    if not caption_text:
        caption_text = str(getattr(table, "continuation_caption", "") or "")
    combined = f"{caption_text} | {header_text}"

    # 1) Susceptibility: S/R/I values dominating the data cells.
    values = _data_values(table)
    sri_count = sum(1 for v in values if _SRI_VALUE.match(v))
    if values and sri_count / len(values) >= 0.5:
        return TableKind.SUSCEPTIBILITY
    if _SUSCEPTIBILITY_WORDS.search(combined) and sri_count > 0:
        return TableKind.SUSCEPTIBILITY

    # 2) Sequence listing columns.
    if _SEQUENCE_WORDS.search(header_text):
        return TableKind.SEQUENCE

    # 2b) A residue LISTING: most data cells are three-letter codes
    #     (``Glu``, ``Leu180``) instead of measurements. Residue grids carry
    #     no "Residue" header at all (the first row is already residues), so
    #     the header vocabulary above cannot see them — EP3423477B1 page 31
    #     was reported as UNCLASSIFIED while being an entire page of SEQ 1.
    residue_cells = sum(1 for v in values if looks_like_residue_token(v))
    if values and residue_cells / len(values) >= 0.6:
        return TableKind.SEQUENCE

    # 3) Scoring vocabulary (before inventory: "Parameter|Animal ID|Score").
    if _SCORING_WORDS.search(header_text):
        return TableKind.SCORING

    # 4) Inventory vocabulary.
    if _INVENTORY_WORDS.search(header_text):
        return TableKind.INVENTORY

    # 5) Activity-numeric: at least one registered metric header AND the
    #    data cells look like numbers/ND.
    metric_headers = [h for h in headers if lookup_metric(h) is not None]
    if metric_headers:
        numericish = sum(
            1
            for v in values
            if _NUMERIC_VALUE.match(v) or v.upper() in ("ND", "NA", "N/A", "-")
        )
        if not values or numericish / len(values) >= 0.5:
            return TableKind.ACTIVITY_NUMERIC

    # 5b) CAPTION-declared metric. Patent tables put "Minimum inhibitory
    #     concentration (MICs)" in the TITLE and use strain/peptide names
    #     as column headers ("Bacteria | Strains | Chain201 | Chain204").
    #     Without this branch the table is UNCLASSIFIED and every MIC
    #     value in it is dropped — the live EP3423477B1 run hit exactly
    #     this (3 tables, 0 measurements).
    if lookup_metric(caption_text) is not None:
        numericish = sum(
            1
            for v in values
            if _NUMERIC_VALUE.match(v)
            or _VALUE_WITH_UNIT.match(v)
            or v.upper() in ("ND", "NA", "N/A", "-")
        )
        if values and numericish / len(values) >= 0.5:
            return TableKind.ACTIVITY_NUMERIC

    # 6) Assay conditions.
    if _CONDITION_WORDS.search(header_text):
        return TableKind.ASSAY_CONDITIONS

    return TableKind.UNCLASSIFIED


__all__ = ["TableKind", "classify_table"]
