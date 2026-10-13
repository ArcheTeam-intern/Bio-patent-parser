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
    # One-to-one: the second duplicate candidate has no gold left to match
    # and is reported as a false positive, not silently absorbed.
    assert result.false_positives == 1


def test_duplicate_golds_cannot_reuse_one_candidate() -> None:
    gold = [_seq_gold("1", "MKTG"), _seq_gold("2", "MKTG")]
    candidates = [_seq_candidate("1", "MKTG")]
    result = match_sequences(gold, candidates)
    assert result.exact_matches == 1
    assert result.missed == 1  # the second gold cannot reuse the candidate


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

def test_wrong_value_with_matching_key_is_not_correct() -> None:
    # Review report reproduction: gold MIC >128 ug/mL vs candidate 0.001 nM
    # with the same key must NOT count as correct.
    gold = [_meas_gold("P1", "E. coli")]
    bad = _meas_candidate("P1", "E. coli")
    bad.value = ActivityValue(kind="scalar", number="0.001", raw="0.001")
    bad.unit_raw = "nM"
    result = match_measurements(gold, [bad])
    assert result.correct == 0
    assert result.wrong_value == 1
    assert result.missed == 0
    assert result.false_positives == 0


def test_range_gold_does_not_match_scalar_suffix_candidate() -> None:
    gold = [_meas_gold("P1", "E. coli")]
    gold[0].value_raw = "1-10"
    gold[0].unit_raw = "nM"
    truncated = _meas_candidate("P1", "E. coli")
    truncated.value = ActivityValue(kind="scalar", number="10", raw="10")
    truncated.unit_raw = "nM"
    result = match_measurements(gold, [truncated])
    assert result.correct == 0
    assert result.wrong_value == 1


def test_range_gold_matches_range_candidate() -> None:
    gold = [_meas_gold("P1", "E. coli")]
    gold[0].value_raw = "1-10"
    gold[0].unit_raw = "nM"
    good = _meas_candidate("P1", "E. coli")
    good.value = ActivityValue(kind="range", lower="1", upper="10", raw="1-10")
    good.unit_raw = "nM"
    result = match_measurements(gold, [good])
    assert result.correct == 1
    assert result.wrong_value == 0


def test_unit_mismatch_is_not_correct() -> None:
    gold = [_meas_gold("P1", "E. coli")]  # >128 µg/mL
    wrong_unit = _meas_candidate("P1", "E. coli")  # >128 — right shape, no unit
    wrong_unit.unit_raw = None
    result = match_measurements(gold, [wrong_unit])
    assert result.correct == 0
    assert result.wrong_value == 1


def test_one_to_one_candidate_consumption() -> None:
    # Two identical golds and one candidate: the candidate is consumed once.
    gold = [_meas_gold("P1", "E. coli"), _meas_gold("P1", "E. coli")]
    result = match_measurements(gold, [_meas_candidate("P1", "E. coli")])
    assert result.correct == 1
    assert result.missed == 1
    assert result.false_positives == 0
