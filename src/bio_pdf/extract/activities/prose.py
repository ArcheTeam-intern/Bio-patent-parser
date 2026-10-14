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
from bio_pdf.extract.activities.mentions import classify_mention_role
from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value
from bio_pdf.extract.relations import classify_evidence_kind

#: Numeric literal, including scientific notation. Values stay strings.
_NUM = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"

#: Recognised concentration/activity units in prose. µ/μ spellings are
#: accepted here and normalised later by :func:`normalize_unit`.
#: The lookahead ``(?![A-Za-zµμ])`` stops bare ``M`` from matching the M in
#: words like "May"/"Mouse"/"Model" (plan T08: the classifier matched
#: "17 May 2017" as "17 M"). Case matters: mM ≠ mm.
_UNIT = (
    r"(?:µg/mL|μg/mL|ug/mL|mg/mL|ng/mL|µg/L|μg/L|ug/L|mg/L|ng/L"
    r"|µM|μM|uM|nM|pM|mM|U/mL|IU/mL|M|%)(?![A-Za-zµμ])"
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


#: A leading operator directly followed by a ± or range expression is
#: sentence context ("IC50 = 1.2 ± 0.3"), not a value comparator.
_PROSE_OP_PREFIX_RE = re.compile(r"^(<=|>=|<|>|=|~|≤|≥)\s*(.+)$")


def _strip_prose_operator(expr: str) -> str:
    """Drop a leading operator when the remainder carries ± or a range.

    ``= 1.2 ± 0.3`` → ``1.2 ± 0.3``; ``>128`` stays (a real comparator).
    """

    m = _PROSE_OP_PREFIX_RE.match(expr)
    if m and re.search(r"±|\d\s*(?:-|–|—|~|\bto\b)\s*\d", m.group(2)):
        return m.group(2).strip()
    return expr


def extract_prose_measurements(
    blocks: list[dict[str, Any]],
    document_id: str,
    *,
    parser_run_id: str = "native",
    evidence_index: Any | None = None,
    raw_mentions: list[dict[str, Any]] | None = None,
) -> list[Measurement]:
    """Extract conservative prose measurement candidates.

    Args:
        blocks: List of dicts with at least ``text`` and optionally
            ``page_number``/``bbox``/``block_id``.
        document_id: Canonical document id.
        parser_run_id: Parser run for evidence id derivation.
        evidence_index: Optional :class:`~bio_pdf.extract.evidence.EvidenceIndex`;
            when given, every match registers a character-precise evidence
            span built from the real match offsets (plan T06).

    Returns:
        A list of :class:`Measurement` candidates. The metric is taken from
        a directly preceding keyword (``IC50 …``) when present and left
        ``unknown`` otherwise.
    """

    from bio_pdf.extract.evidence import BlockRef, span_from_block_match

    measurements: list[Measurement] = []
    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        if not text:
            continue
        page_number = int(block.get("page_number", 1))
        block_id = str(block.get("block_id") or f"{document_id}:p{page_number}:prose:{idx}")
        for m in _VALUE_UNIT_RE.finditer(text):
            expr = _strip_prose_operator(m.group("expr").strip())
            unit_raw = m.group("unit")
            value = parse_activity_value(expr)
            unit_normalized = normalize_unit(unit_raw)
            metric = _metric_before(text, m.start()) or "unknown"

            if evidence_index is not None:
                span = span_from_block_match(
                    BlockRef(
                        document_id=document_id,
                        parser_run_id=parser_run_id,
                        block_id=block_id,
                        page_number=page_number,
                        bbox=tuple(block["bbox"]) if block.get("bbox") else None,
                    ),
                    text_raw=m.group(0),
                    char_start=m.start(),
                    char_end=m.end(),
                    full_text=text,
                )
                evidence_index.add(span)
                evidence_id = span.evidence_id
            else:
                # Legacy path (unit tests / pre-T06 callers).
                evidence_id = f"{document_id}:p{page_number}:prose:{idx}:{m.start()}"

            # Role gating (plan T08): metadata/statistic/condition mentions
            # are NOT activity results. They are kept as RawMentions with
            # their role and reasons, never silently promoted to a value.
            window_text, _ws, _we = _sentence_for(text, m.start(), m.end())
            role = classify_mention_role(window_text)
            if role.role in ("metadata", "statistic", "condition"):
                if raw_mentions is not None:
                    raw_mentions.append(
                        {
                            "text": m.group(0),
                            "role": role.role,
                            "reasons": role.reasons,
                            "page": page_number,
                            "block_id": block_id,
                            "char_start": m.start(),
                        }
                    )
                continue

            # A prose sentence stating a claim threshold ("claims … less
            # than …") is not a measured value.
            if role.role == "claim" or classify_evidence_kind(text) == "claim_threshold":
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


_SENTENCE_SPLIT_RE = __import__("re").compile(r"(?<=[.;!?])\s+")


def _sentence_for(text: str, start: int, end: int) -> tuple[str, int, int]:
    """Return (sentence, abs_start, abs_end) containing the match."""

    boundaries = [0]
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        boundaries.append(m.end())
    boundaries.append(len(text))
    for i in range(len(boundaries) - 1):
        if boundaries[i] <= start < boundaries[i + 1]:
            return text[boundaries[i]:boundaries[i + 1]], boundaries[i], boundaries[i + 1]
    return text, 0, len(text)
