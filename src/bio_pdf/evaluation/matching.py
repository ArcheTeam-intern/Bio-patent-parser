"""Matching ground-truth annotations against candidate records.

Sequences match by ``(document_id, seq_id_no)`` plus character-level content
comparison. Measurements use a *full tuple* match: entity, target, metric,
value (kind + number/range/comparator) and unit must all agree — a candidate
with the right key but the wrong value (e.g. ``0.001 nM`` for gold
``>128 ug/mL``) is NOT counted correct.

Matching is one-to-one on both sides: a gold matches at most one candidate
and a candidate is consumed by at most one gold, so duplicates can never
inflate the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from bio_pdf.evaluation.annotation import Annotation
from bio_pdf.evaluation.metrics import character_error_rate


@dataclass
class SequenceMatchResult:
    """Sequence matching outcome.

    - ``exact_matches``: seq_id found AND sequence identical.
    - ``detected``: seq_id found (regardless of residue content).
    - ``missed``: gold sequence not found by any candidate.
    - ``false_positives``: candidates with no matching gold.
    - ``char_errors``: per-pair character error rate (edit distance / length).
    """

    total_gold: int = 0
    exact_matches: int = 0
    detected: int = 0
    missed: int = 0
    false_positives: int = 0
    char_errors: list[float] = field(default_factory=list)


@dataclass
class MeasurementMatchResult:
    """Measurement matching outcome (full-tuple, one-to-one).

    ``correct`` requires key AND value AND unit agreement. ``wrong_value``
    counts golds whose key matched but whose value/unit did not; these are
    misses (and their candidates false positives), never correct.
    """

    total_gold: int = 0
    correct: int = 0
    missed: int = 0
    false_positives: int = 0
    wrong_value: int = 0
    field_errors: list[dict[str, str]] = field(default_factory=list)


def _verified(gold: list[Annotation]) -> list[Annotation]:
    """Return only human-verified annotations (the official denominator)."""

    return [a for a in gold if a.human_verified and a.status == "verified"]


def match_sequences(
    gold: list[Annotation],
    candidates: list[Any],
) -> SequenceMatchResult:
    """Match gold sequence annotations against candidate sequence records.

    A candidate is matched by ``(document_id, seq_id_no)``. Content equality
    is then checked on ``normalized_sequence``. Candidates without a
    ``seq_id_no`` cannot be matched by identifier and are skipped (they
    cannot be asserted correct without an explicit evidence mapping).

    Matching is one-to-one: each gold consumes its candidate, and a second
    gold with the same key cannot reuse it.
    """

    verified = _verified(gold)
    seq_gold = [a for a in verified if a.kind == "sequence" and a.seq_id_no is not None]

    # Group candidates by (document_id, seq_id_no); matching pops from the
    # group so one gold can never consume two candidates or vice versa.
    candidate_groups: dict[tuple[str, str], list[Any]] = {}
    candidate_count = 0
    for c in candidates:
        seq_no = getattr(c, "seq_id_no", None)
        if seq_no is None:
            continue
        key = (getattr(c, "document_id", ""), str(seq_no))
        candidate_groups.setdefault(key, []).append(c)
        candidate_count += 1

    result = SequenceMatchResult(total_gold=len(seq_gold))

    matched_candidates = 0
    for annotation in seq_gold:
        key = (annotation.document_id, annotation.seq_id_no)
        group = candidate_groups.get(key)
        if not group:
            result.missed += 1
            continue

        candidate = group.pop(0)
        matched_candidates += 1
        result.detected += 1

        gold_seq = annotation.sequence or ""
        cand_seq = getattr(candidate, "normalized_sequence", "") or ""
        if gold_seq == cand_seq:
            result.exact_matches += 1
        else:
            result.char_errors.append(character_error_rate(gold_seq, cand_seq))

    result.false_positives = candidate_count - matched_candidates
    return result


# ---------------------------------------------------------------------------
# Measurement value comparison
# ---------------------------------------------------------------------------


def _dec(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _values_agree(gold_value: str, gold_unit: str | None, candidate: Any) -> bool:
    """Compare a gold ``value_raw`` against a candidate Measurement.

    The candidate's :class:`ActivityValue` carries the structured parse. The
    gold carries the raw source string; we parse it with the same
    :func:`parse_activity_value` used by the extractors, then compare kind,
    numbers (decimal equality, not float), comparator and unit.
    """

    from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value
    from bio_pdf.contracts import ActivityValue

    gold_parsed: ActivityValue = parse_activity_value(gold_value)
    cand_value = getattr(candidate, "value", None)
    if cand_value is None:
        return False

    if gold_parsed.kind != cand_value.kind:
        return False

    if gold_parsed.kind == "categorical":
        gq = (gold_parsed.qualifier or "").upper()
        cq = (cand_value.qualifier or "").upper()
        return gq == cq

    # Comparator must match exactly.
    if (gold_parsed.operator or None) != (cand_value.operator or None):
        return False

    if gold_parsed.kind == "scalar":
        g, c = _dec(gold_parsed.number), _dec(cand_value.number)
        if g is None or c is None or g != c:
            return False
    elif gold_parsed.kind == "range":
        gl, cu = _dec(gold_parsed.lower), _dec(cand_value.lower)
        gu, cl = _dec(gold_parsed.upper), _dec(cand_value.upper)
        if gl is None or cu is None or gl != cu or gu != cl:
            return False
    elif gold_parsed.kind == "plus_minus":
        g, c = _dec(gold_parsed.number), _dec(cand_value.number)
        gu_, cu_ = _dec(gold_parsed.uncertainty), _dec(cand_value.uncertainty)
        if g is None or c is None or g != c or gu_ != cu_:
            return False

    # Unit: normalise both sides; an unknown gold unit only passes when the
    # candidate keeps the same raw spelling.
    gold_unit_n = normalize_unit(gold_unit) if gold_unit else None
    cand_unit_n = (
        normalize_unit(candidate.unit_raw)
        if getattr(candidate, "unit_raw", None)
        else None
    )
    if gold_unit_n != cand_unit_n:
        return False

    return True


def match_measurements(
    gold: list[Annotation],
    candidates: list[Any],
) -> MeasurementMatchResult:
    """Match gold measurement annotations against candidate measurements.

    Key: ``(document_id, entity, target, metric)`` — case-insensitive on the
    free-text parts. A key hit is only ``correct`` when the full value tuple
    agrees (kind, numbers, comparator, unit). Each gold and each candidate
    is consumed at most once.
    """

    verified = _verified(gold)
    meas_gold = [a for a in verified if a.kind == "measurement"]

    def norm(text: str | None) -> str:
        return (text or "").strip().lower()

    def gold_key(a: Annotation) -> tuple[str, str, str, str]:
        return (
            a.document_id,
            norm(a.entity_raw),
            norm(a.target_raw),
            norm(a.metric),
        )

    def cand_key(c: Any) -> tuple[str, str, str, str]:
        return (
            getattr(c, "document_id", ""),
            norm(getattr(c, "entity_id", None) or getattr(c, "entity_raw", None)),
            norm(getattr(c, "target_raw", None)),
            norm(getattr(c, "metric", None)),
        )

    candidate_groups: dict[tuple[str, str, str, str], list[Any]] = {}
    for c in candidates:
        candidate_groups.setdefault(cand_key(c), []).append(c)
    candidate_count = sum(len(v) for v in candidate_groups.values())

    result = MeasurementMatchResult(total_gold=len(meas_gold))
    matched_candidates = 0

    for annotation in meas_gold:
        key = gold_key(annotation)
        group = candidate_groups.get(key)
        if not group:
            result.missed += 1
            continue

        # First candidate in the group whose full tuple agrees wins; if none
        # agrees, the first key-matching candidate is the wrong-value hit.
        value_hit = None
        for candidate in group:
            if _values_agree(annotation.value_raw or "", annotation.unit_raw, candidate):
                value_hit = candidate
                break

        if value_hit is not None:
            group.remove(value_hit)
            matched_candidates += 1
            result.correct += 1
            continue

        wrong = group.pop(0)
        matched_candidates += 1
        result.wrong_value += 1
        cand_value = getattr(wrong, "value", None)
        result.field_errors.append(
            {
                "gold": f"{annotation.metric}={annotation.value_raw} {annotation.unit_raw or ''}".strip(),
                "candidate": f"{getattr(wrong, 'metric', '')}={getattr(cand_value, 'raw', '')}",
                "type": "value_or_unit_mismatch",
            }
        )

    result.missed = result.total_gold - result.correct - result.wrong_value
    result.false_positives = candidate_count - matched_candidates
    return result


__all__ = [
    "SequenceMatchResult",
    "MeasurementMatchResult",
    "match_sequences",
    "match_measurements",
]
