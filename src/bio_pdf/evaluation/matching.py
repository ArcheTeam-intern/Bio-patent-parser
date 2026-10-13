"""Matching ground-truth annotations against candidate records.

Matching is keyed on stable identifiers (``(document_id, seq_id_no)`` for
sequences; ``(document_id, entity, target, metric)`` for measurements). A
gold annotation is matched to at most one candidate, so a single gold can
never be double-counted as multiple true positives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """Measurement matching outcome (tuple-level)."""

    total_gold: int = 0
    correct: int = 0
    missed: int = 0
    false_positives: int = 0


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
    """

    verified = _verified(gold)
    seq_gold = [a for a in verified if a.kind == "sequence" and a.seq_id_no is not None]

    # Index candidates by (document_id, seq_id_no). The first candidate wins
    # so a gold is never double-counted.
    candidate_index: dict[tuple[str, str], Any] = {}
    candidate_ids: set[tuple[str, str]] = set()
    for c in candidates:
        seq_no = getattr(c, "seq_id_no", None)
        if seq_no is None:
            continue
        key = (getattr(c, "document_id", ""), str(seq_no))
        candidate_ids.add(key)
        candidate_index.setdefault(key, c)

    result = SequenceMatchResult(total_gold=len(seq_gold))

    matched_candidate_keys: set[tuple[str, str]] = set()
    for annotation in seq_gold:
        key = (annotation.document_id, annotation.seq_id_no)
        candidate = candidate_index.get(key)
        if candidate is None:
            result.missed += 1
            continue

        result.detected += 1
        matched_candidate_keys.add(key)

        gold_seq = annotation.sequence or ""
        cand_seq = getattr(candidate, "normalized_sequence", "") or ""
        if gold_seq == cand_seq:
            result.exact_matches += 1
        else:
            result.char_errors.append(character_error_rate(gold_seq, cand_seq))

    result.false_positives = len(candidate_ids - matched_candidate_keys)
    return result


def match_measurements(
    gold: list[Annotation],
    candidates: list[Any],
) -> MeasurementMatchResult:
    """Match gold measurement annotations against candidate measurements.

    The match key is the ``(document_id, entity, target, metric)`` tuple. A
    candidate is ``correct`` only when the entity, target and metric all line
    up — a value that is right but assigned to the wrong entity is a miss.
    """

    verified = _verified(gold)
    meas_gold = [a for a in verified if a.kind == "measurement"]

    def gold_key(a: Annotation) -> tuple[str, str, str, str]:
        return (a.document_id, a.entity_raw or "", a.target_raw or "", a.metric or "")

    candidate_index: dict[tuple[str, str, str, str], Any] = {}
    candidate_keys: set[tuple[str, str, str, str]] = set()
    for c in candidates:
        key = (
            getattr(c, "document_id", ""),
            getattr(c, "entity_id", "") or "",
            getattr(c, "target_raw", "") or "",
            getattr(c, "metric", "") or "",
        )
        candidate_keys.add(key)
        candidate_index.setdefault(key, c)

    result = MeasurementMatchResult(total_gold=len(meas_gold))
    matched: set[tuple[str, str, str, str]] = set()

    for annotation in meas_gold:
        key = gold_key(annotation)
        candidate = candidate_index.get(key)
        if candidate is None:
            result.missed += 1
            continue
        matched.add(key)
        result.correct += 1

    result.false_positives = len(candidate_keys - matched)
    return result


__all__ = [
    "SequenceMatchResult",
    "MeasurementMatchResult",
    "match_sequences",
    "match_measurements",
]