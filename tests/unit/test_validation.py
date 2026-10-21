"""Validation rule tests (sequences, measurements, evidence)."""

from __future__ import annotations

from bio_pdf.contracts import (
    ActivityValue,
    Measurement,
    SequenceRecord,
)
from bio_pdf.validation.activities import validate_measurement
from bio_pdf.validation.evidence import validate_evidence
from bio_pdf.validation.sequences import validate_sequence


def _sequence(**overrides) -> SequenceRecord:
    base = dict(
        sequence_id="seq-1",
        document_id="sha256:doc",
        seq_id_no="1",
        molecule_type="protein",
        representation="one_letter",
        declared_length=4,
        observed_length=4,
        raw_sequence="MKTG",
        normalized_sequence="MKTG",
        is_complete=True,
        evidence_ids=["ev-1"],
    )
    base.update(overrides)
    return SequenceRecord(**base)


def test_sequence_length_match_passes() -> None:
    result = validate_sequence(_sequence())
    assert result.validation_state == "checks_passed"
    assert any(c.check_id == "SEQ_DECLARED_LENGTH_MATCH" and c.outcome == "pass" for c in result.checks)


def test_sequence_length_mismatch_fails() -> None:
    result = validate_sequence(_sequence(declared_length=10, observed_length=4))
    assert result.validation_state == "conflict"
    assert any(c.check_id == "SEQ_DECLARED_LENGTH_MATCH" and c.outcome == "fail" for c in result.checks)


def test_sequence_disallowed_alphabet_fails() -> None:
    result = validate_sequence(_sequence(normalized_sequence="MKT1", raw_sequence="MKT1", observed_length=4))
    assert result.validation_state == "conflict"
    assert any(c.check_id == "SEQ_ALLOWED_ALPHABET" and c.outcome == "fail" for c in result.checks)


def test_sequence_incomplete_fails() -> None:
    result = validate_sequence(_sequence(is_complete=False))
    assert any(c.check_id == "SEQ_COMPLETENESS" and c.outcome == "fail" for c in result.checks)


def _measurement(**overrides) -> Measurement:
    base = dict(
        measurement_id="meas-1",
        document_id="sha256:doc",
        entity_id="ent-1",
        metric="IC50",
        value=ActivityValue(kind="scalar", number="1.2", raw="1.2"),
        unit_raw="nM",
        target_raw="E. coli",
        evidence_kind="measured",
        evidence_ids=["ev-1"],
    )
    base.update(overrides)
    return Measurement(**base)


def test_measurement_valid_passes() -> None:
    result = validate_measurement(_measurement())
    assert result.validation_state == "checks_passed"


def test_measurement_unparsed_value_fails() -> None:
    result = validate_measurement(
        _measurement(value=ActivityValue(kind="unparsed", raw="???"))
    )
    assert any(c.check_id == "ACT_VALUE_PARSE" and c.outcome == "fail" for c in result.checks)


def test_measurement_categorical_nd_is_valid() -> None:
    result = validate_measurement(
        _measurement(value=ActivityValue(kind="categorical", qualifier="ND", raw="ND"))
    )
    # ND is a valid parse (explicit "not determined"), not a missing value.
    assert any(c.check_id == "ACT_VALUE_PARSE" and c.outcome == "pass" for c in result.checks)


def test_measurement_missing_unit_flagged() -> None:
    result = validate_measurement(_measurement(unit_raw=None))
    assert any(c.check_id == "ACT_UNIT_PRESENT" and c.outcome == "unknown" for c in result.checks)


def test_not_tested_cell_does_not_need_a_concentration_unit() -> None:
    """A "-" cell of a MIC table is a categorical "not tested".

    MIC requires a unit for a NUMERIC value; a not-tested marker has no
    concentration, so demanding μg/mL turned every dash of live Table 4c
    into an unfixable unit review item.
    """

    dash = ActivityValue(kind="categorical", qualifier="-", raw="-")
    result = validate_measurement(_measurement(metric="MIC", value=dash, unit_raw=None))
    assert "measurement has no unit" not in result.review_reasons
    assert result.validation_state == "checks_passed"

    # A numeric MIC still requires its unit.
    numeric = ActivityValue(kind="scalar", number="4", raw="4")
    result = validate_measurement(_measurement(metric="MIC", value=numeric, unit_raw=None))
    assert "measurement has no unit" in result.review_reasons


def test_evidence_missing_is_conflict() -> None:
    result = validate_evidence([], evidence_index={})
    assert result.validation_state == "conflict"
    assert any(c.check_id == "EVIDENCE_EXISTS" and c.outcome == "fail" for c in result.checks)


def test_evidence_exists_with_geometry_passes() -> None:
    class Span:
        page_number = 1
        bbox = (0, 0, 10, 10)

    result = validate_evidence(["ev-1"], evidence_index={"ev-1": Span()})
    assert any(c.check_id == "EVIDENCE_EXISTS" and c.outcome == "pass" for c in result.checks)


def test_evidence_missing_reference_is_conflict() -> None:
    result = validate_evidence(["ev-missing"], evidence_index={})
    assert result.validation_state == "conflict"

def test_categorical_susceptibility_does_not_require_unit() -> None:
    """C05: S/R/I results are categorical — no concentration unit exists."""

    from bio_pdf.contracts import ActivityValue, Measurement
    from bio_pdf.validation.activities import CHECK_UNIT, validate_measurement

    def _measurement(metric: str, value: ActivityValue) -> Measurement:
        return Measurement(
            measurement_id="m1",
            document_id="sha256:x",
            metric=metric,
            value=value,
            unit_raw=None,
            evidence_ids=["ev-1"],
        )

    categorical = ActivityValue(kind="categorical", qualifier="S", raw="S")
    result = validate_measurement(_measurement("antimicrobial_susceptibility", categorical))
    unit_check = next(c for c in result.checks if c.check_id == CHECK_UNIT)
    assert unit_check.outcome == "pass"
    assert "not applicable" in unit_check.details["message"]
    assert "measurement has no unit" not in result.review_reasons

    numeric = ActivityValue(kind="scalar", number="3.5", raw="3.5")
    result = validate_measurement(_measurement("ic50", numeric))
    unit_check = next(c for c in result.checks if c.check_id == CHECK_UNIT)
    assert unit_check.outcome == "unknown"
    assert "measurement has no unit" in result.review_reasons
