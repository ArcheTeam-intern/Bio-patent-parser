"""Metric definition registry (plan T08).

A table (or prose mention) only produces Measurements when its header
carries a *recognized* metric. Unknown metrics stay in diagnostics/review —
they are never forced into the bioactivity view. Registry entries carry
what validation needs later: value kind and unit/target/entity
requirements. This is policy pending validation, not an accuracy claim.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import MetricDefinition, Table

#: Canonical metric definitions. `aliases` are matched case-insensitively
#: against table headers and prose keywords; the canonical name is stored on
#: the record (`metric`), the source spelling stays in `metric_raw`.
METRIC_REGISTRY: dict[str, MetricDefinition] = {
    m.name: m
    for m in [
        MetricDefinition(
            name="MIC",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="required",
            entity_requirement="required",
            allowed_units=["ug/mL", "mg/mL", "ng/mL", "uM", "nM", "mM", "µg/mL", "µM"],
            description="Minimum inhibitory concentration",
        ),
        MetricDefinition(
            name="MBC",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="required",
            entity_requirement="required",
            allowed_units=["ug/mL", "mg/mL", "uM", "nM", "mM"],
            description="Minimum bactericidal concentration",
        ),
        MetricDefinition(
            name="IC50",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="optional",
            entity_requirement="required",
            allowed_units=["uM", "nM", "pM", "mM", "ug/mL", "ng/mL"],
            description="Half maximal inhibitory concentration",
        ),
        MetricDefinition(
            name="EC50",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="optional",
            entity_requirement="required",
            allowed_units=["uM", "nM", "pM", "mM", "ug/mL", "ng/mL"],
            description="Half maximal effective concentration",
        ),
        MetricDefinition(
            name="KD",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="required",
            entity_requirement="required",
            allowed_units=["uM", "nM", "pM", "mM"],
            description="Dissociation constant",
        ),
        MetricDefinition(
            name="Ki",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="required",
            entity_requirement="required",
            allowed_units=["uM", "nM", "pM", "mM"],
            description="Inhibition constant",
        ),
        MetricDefinition(
            name="Km",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="not_applicable",
            entity_requirement="required",
            allowed_units=["uM", "nM", "mM"],
            description="Michaelis constant",
        ),
        MetricDefinition(
            name="kcat",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="not_applicable",
            entity_requirement="required",
            allowed_units=["s^-1", "min^-1"],
            description="Catalytic rate constant",
        ),
        MetricDefinition(
            name="inhibition",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="optional",
            entity_requirement="required",
            allowed_units=["%"],
            description="Percent inhibition",
        ),
        MetricDefinition(
            name="viability",
            value_kind="numeric",
            unit_requirement="required",
            target_requirement="not_applicable",
            entity_requirement="optional",
            allowed_units=["%"],
            description="Percent cell viability",
        ),
        MetricDefinition(
            name="antimicrobial_susceptibility",
            value_kind="categorical",
            unit_requirement="not_applicable",
            target_requirement="required",
            entity_requirement="required",
            description="CLAUDE/EUCAST-style S/R/I categorical result",
        ),
    ]
}

#: Non-metric header vocabulary seen in real documents. These NEVER justify
#: a Measurement (plan: Used/Notes/Animal/Score noise).
_NON_METRIC_HEADERS = {
    "used",
    "used in",
    "notes",
    "note",
    "animal",
    "score",
    "value",
    "id",
    "name",
    "strain",
    "species",
    "source",
    "comments",
    "remark",
    "remarks",
    "animal id",
    "group",
}

#: alias (lowercase) → canonical metric name.
_ALIAS_MAP: dict[str, str] = {}
for _name, _def in METRIC_REGISTRY.items():
    _ALIAS_MAP[_name.lower()] = _name
for _alias, _canonical in [
    ("mic50", "MIC"),
    ("mbc50", "MBC"),
    ("ic₅₀", "IC50"),
    ("ec₅₀", "EC50"),
    ("kd", "KD"),
    ("ki", "Ki"),
    ("kd value", "KD"),
    ("susceptibility", "antimicrobial_susceptibility"),
    ("s/r/i", "antimicrobial_susceptibility"),
]:
    _ALIAS_MAP.setdefault(_alias.lower(), _canonical)

_STRIP_RE = re.compile(r"[^a-z0-9₅°/ ]+")


def lookup_metric(header_text: str) -> tuple[str, str] | None:
    """Match header text against the registry.

    Returns ``(canonical_name, raw_spelling)`` or None. The match must be a
    word-level hit, not a substring of a longer word ("Mic" inside
    "Microbiology" does not count).
    """

    text = header_text.strip()
    if not text:
        return None
    normalized = _STRIP_RE.sub(" ", text.lower())
    for token in normalized.split():
        canonical = _ALIAS_MAP.get(token)
        if canonical is not None:
            return canonical, text
    # Two-word aliases like "used in" handled by direct containment.
    lowered = text.lower()
    if lowered in _NON_METRIC_HEADERS:
        return None
    return None


def is_non_metric_header(header_text: str) -> bool:
    return header_text.strip().lower() in _NON_METRIC_HEADERS


def classify_table_metric(table: Any) -> str | None:
    """Return the canonical metric a table reports, or None.

    A table qualifies only when its header cells (row 0/1, per the normalised
    header set) carry a recognized metric. Tables whose headers are all
    non-metric vocabulary (Used/Notes/Animal/Score/inventory labels) return
    None — the caller records a table-level semantic review item instead of
    per-cell noise.
    """

    if not isinstance(table, Table):
        return None
    header_ids = set(table.header_cell_ids or [])
    header_cells = [c for c in table.cells if c.cell_id in header_ids]
    if not header_cells:
        # fall back to the first two rows
        rows = sorted({c.row_index for c in table.cells})[:2]
        header_cells = [c for c in table.cells if c.row_index in rows]

    hits: dict[str, str] = {}
    for cell in header_cells:
        text = (cell.text_raw or "").strip()
        if not text or is_non_metric_header(text):
            continue
        hit = lookup_metric(text)
        if hit is not None:
            canonical, raw = hit
            hits.setdefault(canonical, raw)
    if not hits:
        return None
    # One table = one primary metric; conflicting metrics in one header set
    # (e.g. MIC + MBC columns) are still a valid MIC/MBC panel only if they
    # share the family — take the first canonical hit for extraction; the
    # per-column header path in tables.py refines per data column.
    return next(iter(sorted(hits)))


__all__ = [
    "METRIC_REGISTRY",
    "lookup_metric",
    "is_non_metric_header",
    "classify_table_metric",
]
