"""Prose measurement candidates.

Conservative extraction of ``value + unit`` mentions in body text. The
extractor only fires when a numeric value is followed by a recognised
concentration unit (e.g. ``1.2 nM``), so free-form prose that merely mentions
a number is not turned into a measurement.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import Measurement
from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value

#: ``1.2 nM`` / ``>128 µg/mL`` — value followed by a unit.
_VALUE_UNIT_RE = re.compile(
    r"((?:<=|>=|<|>|=|~|≤|≥)?\s*[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"\s*(µg/mL|μg/mL|ug/mL|mg/mL|ng/mL|mg/L|ug/L|ng/L|uM|nM|pM|mM|M|U/mL|IU/mL|%)\b"
)


def extract_prose_measurements(
    blocks: list[dict[str, Any]],
    document_id: str,
) -> list[Measurement]:
    """Extract conservative prose measurement candidates.

    Args:
        blocks: List of dicts with at least ``text`` and optionally
            ``page_number``.
        document_id: Canonical document id.

    Returns:
        A list of :class:`Measurement` candidates (metric unknown unless a
        nearby keyword is present).
    """

    measurements: list[Measurement] = []
    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        if not text:
            continue
        page_number = int(block.get("page_number", 1))
        for m in _VALUE_UNIT_RE.finditer(text):
            value_text = m.group(1).strip()
            unit_raw = m.group(2)
            value = parse_activity_value(value_text)
            unit_normalized = normalize_unit(unit_raw)
            measurement_id = f"{document_id}:prose:{idx}:{m.start()}"

            measurements.append(
                Measurement(
                    measurement_id=measurement_id,
                    document_id=document_id,
                    entity_id=None,
                    sequence_ids=[],
                    assay_id=None,
                    metric="unknown",
                    value=value,
                    unit_raw=unit_raw,
                    unit_normalized=unit_normalized,
                    target_raw=None,
                    conditions=[],
                    evidence_kind="general_statement",
                    field_evidence={
                        "value": [f"{document_id}:p{page_number}:prose:{idx}:{m.start()}"]
                    },
                    evidence_ids=[f"{document_id}:p{page_number}:prose:{idx}:{m.start()}"],
                    checks=[],
                    validation_state="unchecked",
                    acceptance_state="candidate",
                )
            )
    return measurements


__all__ = ["extract_prose_measurements"]