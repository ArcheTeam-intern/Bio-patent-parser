"""Prose measurement candidates.

Conservative extraction of ``value + unit`` mentions in body text. The
extractor matches the *whole* value expression — comparator, range or
plus-minus included — before a recognised unit, so a range like ``1–10 nM``
is never truncated into the trailing scalar ``10 nM`` and ``1.2 ± 0.3 nM``
never becomes ``0.3 nM``.

Values are parsed by :func:`parse_activity_value` (string maths only). When
the expression cannot be represented it stays ``unparsed`` with its raw
text; the extractor never fabricates a scalar from a suffix.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import Measurement
from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value
from bio_pdf.extract.relations import classify_evidence_kind

#: Numeric literal, including scientific notation. Values stay strings.
_NUM = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"

#: Recognised concentration/activity units in prose. µ/μ spellings are
#: accepted here and normalised later by :func:`normalize_unit`.
_UNIT = (
    r"(?:µg/mL|μg/mL|ug/mL|mg/mL|ng/mL|µg/L|μg/L|ug/L|mg/L|ng/L"
    r"|µM|μM|uM|nM|pM|mM|U/mL|IU/mL|M|%)"
)

#: One full value expression followed by a unit:
#: ``>128 µg/mL`` / ``1–10 nM`` / ``1.2 ± 0.3 nM`` / ``1 to 10 nM`` / ``5 uM``.
#: Group ``expr`` is everything before the unit so the original expression is
#: parsed as a whole, never by suffix.
_VALUE_UNIT_RE = re.compile(
    rf"(?P<expr>(?P<op><=|>=|<|>|=|~|≤|≥)?\s*(?P<first>{_NUM})"
    rf"(?:\s*±\s*(?P<unc>{_NUM})|\s*(?:-|–|—|~|\bto\b)\s*(?P<second>{_NUM}))?)"
    rf"\s*(?P<unit>{_UNIT})"
)

#: Metric keywords searched immediately before a value mention.
_METRIC_HINT_RE = re.compile(
    r"(IC50|IC₅₀|EC50|EC₅₀|ED50|ED₅₀|GI50|KI|K[Ii]|KD|Kd|KM|Km|MIC|KB)"
    r"\s*(?:value|of|was|is|=|:)?\s*$",
    re.IGNORECASE,
)


def _metric_before(text: str, start: int) -> str | None:
    """Return a metric keyword immediately preceding ``start``, else None."""

    window = text[max(0, start - 40) : start]
    if m := _METRIC_HINT_RE.search(window):
        return m.group(1).upper().replace("₅₀", "50")
    return None


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
        A list of :class:`Measurement` candidates. The metric is taken from
        a directly preceding keyword (``IC50 …``) when present and left
        ``unknown`` otherwise.
    """

    measurements: list[Measurement] = []
    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        if not text:
            continue
        page_number = int(block.get("page_number", 1))
        for m in _VALUE_UNIT_RE.finditer(text):
            expr = m.group("expr").strip()
            unit_raw = m.group("unit")
            value = parse_activity_value(expr)
            unit_normalized = normalize_unit(unit_raw)
            metric = _metric_before(text, m.start()) or "unknown"
            evidence_id = f"{document_id}:p{page_number}:prose:{idx}:{m.start()}"

            # A prose sentence stating a claim threshold ("claims … less
            # than …") is not a measured value.
            if classify_evidence_kind(text) == "claim_threshold":
                evidence_kind = "claim_threshold"
            else:
                evidence_kind = "general_statement"

            measurements.append(
                Measurement(
                    measurement_id=f"{document_id}:prose:{idx}:{m.start()}",
                    document_id=document_id,
                    entity_id=None,
                    sequence_ids=[],
                    assay_id=None,
                    metric=metric,
                    value=value,
                    unit_raw=unit_raw,
                    unit_normalized=unit_normalized,
                    target_raw=None,
                    conditions=[],
                    evidence_kind=evidence_kind,  # type: ignore[arg-type]
                    field_evidence={"value": [evidence_id]},
                    evidence_ids=[evidence_id],
                    checks=[],
                    validation_state="unchecked",
                    acceptance_state="candidate",
                )
            )
    return measurements


__all__ = ["extract_prose_measurements"]
