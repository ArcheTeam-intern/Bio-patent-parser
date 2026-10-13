"""Matching tests (sequences and measurements)."""

from __future__ import annotations

from bio_pdf.contracts import ActivityValue, Measurement, SequenceRecord
from bio_pdf.evaluation.annotation import Annotation
from bio_pdf.evaluation.matching import match_measurements, match_sequences


def _seq_gold(seq_id_no: str, seq: str, *, verified: bool = True) -> Annotation:
    return Annotation(
        annotation_id=f"g-{seq_id_no}",
        document_id="sha256:doc",
        page_number=1,
        kind="sequence",
        seq_id_no=seq_id_no,
        sequence=seq,
        human_verified=verified,
        status="verified" if verified else "draft",
    )


def _seq_candidate(seq_id_no: str, seq: str) -> SequenceRecord:
    return SequenceRecord(
        sequence_id=f"sha256:doc:seq:{seq_id_no}",
        document_id="sha256:doc",
        seq_id_no=seq_id_no,
        molecule_type="protein",
        representation="one_letter",
        raw_sequence=seq,
        normalized_sequence=seq,
        evidence_ids=["ev"],
    )


def test_exact_match_counts() -> None:
    gold = [_seq_gold("1", "MKTG"), _seq_gold("2", "AAAA")]
    candidates = [_seq_candidate("1", "MKTG"), _seq_candidate("2", "AAAA")]
    result = match_sequences(gold, candidates)
    assert result.total_gold == 2
    assert result.exact_matches == 2
    assert result.detected == 2
    assert result.missed == 0
    assert result.false_positives == 0


def test_wrong_residue_is_detected_but_not_exact() -> None:
    gold = [_seq_gold("1", "MKTG")]
    candidates = [_seq_candidate("1", "MKTA")]  # one residue differs
    result = match_sequences(gold, candidates)
    assert result.exact_matches == 0
    assert result.detected == 1
    assert len(result.char_errors) == 1
    assert result.char_errors[0] > 0


def test_missed_sequence() -> None:
    gold = [_seq_gold("1", "MKTG")]
    candidates = []  # nothing detected
    result = match_sequences(gold, candidates)
    assert result.missed == 1
    assert result.detected == 0


def test_duplicate_candidates_do_not_double_count() -> None:
    gold = [_seq_gold("1", "MKTG")]
    candidates = [_seq_candidate("1", "MKTG"), _seq_candidate("1", "MKTG")]
    result = match_sequences(gold, candidates)
    assert result.exact_matches == 1  # gold counted once
    assert result.false_positives == 0  # second candidate is the same key


def test_draft_annotation_excluded_from_denominator() -> None:
    gold = [_seq_gold("1", "MKTG", verified=False)]  # draft, not human-verified
    candidates = [_seq_candidate("1", "MKTG")]
    result = match_sequences(gold, candidates)
    assert result.total_gold == 0  # draft excluded


def _meas_gold(entity: str, target: str, metric: str = "MIC") -> Annotation:
    return Annotation(
        annotation_id=f"g-{entity}-{target}",
        document_id="sha256:doc",
        page_number=2,
        kind="measurement",
        metric=metric,
        entity_raw=entity,
        target_raw=target,
        value_raw=">128",
        unit_raw="µg/mL",
        human_verified=True,
        status="verified",
    )


def _meas_candidate(entity: str, target: str, metric: str = "MIC") -> Measurement:
    return Measurement(
        measurement_id=f"m-{entity}-{target}",
        document_id="sha256:doc",
        entity_id=entity,
        metric=metric,
        value=ActivityValue(kind="scalar", operator=">", number="128", raw=">128"),
        unit_raw="µg/mL",
        target_raw=target,
        evidence_kind="measured",
        evidence_ids=["ev"],
    )


def test_measurement_tuple_correct() -> None:
    gold = [_meas_gold("A", "E. coli")]
    candidates = [_meas_candidate("A", "E. coli")]
    result = match_measurements(gold, candidates)
    assert result.total_gold == 1
    assert result.correct == 1
    assert result.missed == 0


def test_measurement_wrong_entity_is_miss() -> None:
    gold = [_meas_gold("A", "E. coli")]
    candidates = [_meas_candidate("B", "E. coli")]  # wrong entity
    result = match_measurements(gold, candidates)
    assert result.correct == 0
    assert result.missed == 1
    assert result.false_positives == 1


def test_measurement_wrong_target_is_miss() -> None:
    gold = [_meas_gold("A", "E. coli")]
    candidates = [_meas_candidate("A", "S. aureus")]  # wrong target
    result = match_measurements(gold, candidates)
    assert result.correct == 0
    assert result.missed == 1