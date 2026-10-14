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
    *,
    scope: list[str] | None = None,
    evidence_index: dict[str, Any] | None = None,
) -> SequenceMatchResult:
    """Match gold sequence annotations against candidate sequence records.

    Two match routes, both one-to-one (plan B10):

    - **listing**: ``(document_id, seq_id_no)``, then residue comparison.
      Same number with different content = exact miss + the candidate is
      still a false positive (detected is counted separately).
    - **inline**: gold WITHOUT a seq id matches a candidate by (page,
      residues) — the same source position carrying the same sequence. A
      residue-equal candidate on a different page does not silently match;
      every unmatched candidate counts as a false positive, INCLUDING
      candidates the rules did not identify (author names etc).

    ``scope`` limits both gold and candidates to the listed documents
    (CorpusScope); candidates outside a verified scope must not be scored
    by the caller (see evaluation.corpus).
    """

    verified = _verified(gold)
    if scope is not None:
        in_scope = set(scope)
        verified = [a for a in verified if a.document_id in in_scope]
    seq_gold = [a for a in verified if a.kind == "sequence"]

    scoped_candidates = [
        c
        for c in candidates
        if scope is None or getattr(c, "document_id", "") in set(scope)
    ]

    # Group listing candidates by (document_id, seq_id_no).
    listing_groups: dict[tuple[str, str], list[Any]] = {}
    inline_candidates: list[Any] = []
    for c in scoped_candidates:
        seq_no = getattr(c, "seq_id_no", None)
        if seq_no is None:
            inline_candidates.append(c)
            continue
        key = (getattr(c, "document_id", ""), str(seq_no))
        listing_groups.setdefault(key, []).append(c)

    result = SequenceMatchResult(total_gold=len(seq_gold))
    matched_candidates = 0

    for annotation in seq_gold:
        cand_seq = annotation.sequence or ""
        if annotation.seq_id_no is not None:
            key = (annotation.document_id, annotation.seq_id_no)
            group = listing_groups.get(key)
            if not group:
                result.missed += 1
                continue
            # An EXACT-content candidate wins; otherwise the number was
            # detected but the content is wrong (plan B10): exact FN + the
            # candidate stays unconsumed → exact FP.
            exact_index = next(
                (
                    i
                    for i, candidate in enumerate(group)
                    if (getattr(candidate, "normalized_sequence", "") or "") == cand_seq
                ),
                None,
            )
            if exact_index is None:
                result.detected += 1
                result.missed += 1
                actual = getattr(group[0], "normalized_sequence", "") or ""
                result.char_errors.append(character_error_rate(cand_seq, actual))
                continue
            candidate = group.pop(exact_index)
            matched_candidates += 1
            result.detected += 1
            result.exact_matches += 1
            continue

        # Inline gold: match by page + residues (position edge). A
        # candidate with NO page evidence cannot be asserted correct —
        # residues alone are not a position edge (plan B10).
        hit_index = None
        for index, candidate in enumerate(inline_candidates):
            if (getattr(candidate, "normalized_sequence", "") or "") != cand_seq:
                continue
            pages = _candidate_pages(candidate, evidence_index)
            if annotation.page_number in pages:
                hit_index = index
                break
        if hit_index is None:
            result.missed += 1
            continue
        inline_candidates.pop(hit_index)
        matched_candidates += 1
        result.detected += 1
        result.exact_matches += 1

    # Every unconsumed candidate — listing leftovers AND inline leftovers
    # (including author/drug false positives) — counts as FP.
    leftover_listing = sum(len(group) for group in listing_groups.values())
    result.false_positives = leftover_listing + len(inline_candidates)
    return result


def _candidate_pages(
    candidate: Any, evidence_index: dict[str, Any] | None = None
) -> list[int]:
    """Pages a candidate is anchored to (from its evidence spans)."""

    pages: list[int] = []
    if evidence_index:
        for evidence_id in getattr(candidate, "evidence_ids", None) or []:
            span = evidence_index.get(evidence_id)
            if span is None:
                continue
            page = (
                span.get("page_number")
                if isinstance(span, dict)
                else getattr(span, "page_number", None)
            )
            if page is not None and page not in pages:
                pages.append(int(page))
            fragments = (
                span.get("fragments") or []
                if isinstance(span, dict)
                else getattr(span, "fragments", None) or []
            )
            for fragment in fragments:
                fpage = (
                    fragment.get("page_number")
                    if isinstance(fragment, dict)
                    else getattr(fragment, "page_number", None)
                )
                if fpage is not None and int(fpage) not in pages:
                    pages.append(int(fpage))
    # A candidate object may itself expose pages (test doubles).
    for page in getattr(candidate, "candidate_pages", None) or []:
        if int(page) not in pages:
            pages.append(int(page))
    return pages


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
    *,
    scope: list[str] | None = None,
) -> MeasurementMatchResult:
    """Match gold measurement annotations against candidate measurements.

    Key: ``(document_id, entity, target, metric)`` — case-insensitive on the
    free-text parts. A key hit is only ``correct`` when the full value tuple
    agrees (kind, numbers, comparator, unit). Each gold and each candidate
    is consumed at most once.
    """

    verified = _verified(gold)
    if scope is not None:
        verified = [a for a in verified if a.document_id in set(scope)]
        candidates = [
            c for c in candidates if getattr(c, "document_id", "") in set(scope)
        ]
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
