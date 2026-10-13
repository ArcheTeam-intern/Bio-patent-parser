"""Activity value and unit parsing tests."""

from __future__ import annotations

from bio_pdf.extract.activities.values import normalize_unit, parse_activity_value


def test_comparator_interval_and_missing_value_are_distinct() -> None:
    greater = parse_activity_value(">128")
    assert greater.kind == "scalar"
    assert greater.operator == ">"
    assert greater.number == "128"

    interval = parse_activity_value("0.5–1.0")
    assert interval.kind == "range"
    assert interval.lower == "0.5"
    assert interval.upper == "1.0"

    missing = parse_activity_value("ND")
    assert missing.kind == "categorical"
    assert missing.number is None
    assert missing.qualifier == "ND"


def test_less_than_or_equal_unicode() -> None:
    value = parse_activity_value("≤1.2")
    assert value.kind == "scalar"
    # Unicode comparator is normalised to the ASCII contract form.
    assert value.operator == "<="
    assert value.number == "1.2"


def test_greater_than_or_equal() -> None:
    value = parse_activity_value("≥ 0.5")
    assert value.kind == "scalar"
    assert value.operator == ">="
    assert value.number == "0.5"


def test_plus_minus_value() -> None:
    value = parse_activity_value("1.2 ± 0.1")
    assert value.kind == "plus_minus"
    assert value.number == "1.2"
    assert value.uncertainty == "0.1"


def test_scientific_notation() -> None:
    value = parse_activity_value("1.2e-3")
    assert value.kind == "scalar"
    assert value.number == "1.2e-3"


def test_plain_scalar() -> None:
    value = parse_activity_value("4")
    assert value.kind == "scalar"
    assert value.number == "4"
    assert value.operator is None


def test_categorical_preserves_original() -> None:
    value = parse_activity_value("not tested")
    assert value.kind == "categorical"
    assert value.qualifier == "not tested"


def test_unparsed_value_keeps_raw() -> None:
    value = parse_activity_value("abc123xyz")
    assert value.kind == "unparsed"
    assert value.raw == "abc123xyz"


def test_unparseable_garbage_is_unparsed() -> None:
    value = parse_activity_value("~?~")
    assert value.kind == "unparsed"
    assert value.raw == "~?~"


def test_no_float_truncation_in_scientific_notation() -> None:
    # The string is preserved verbatim, not round-tripped through float().
    value = parse_activity_value("1.234567890123456e-9")
    assert value.number == "1.234567890123456e-9"


# ---------------------------------------------------------------------------
# normalize_unit
# ---------------------------------------------------------------------------


def test_normalize_unit_micro_sign() -> None:
    assert normalize_unit("µg/mL") == "ug/mL"
    assert normalize_unit("μg/mL") == "ug/mL"


def test_normalize_unit_known_unchanged() -> None:
    assert normalize_unit("nM") == "nM"


def test_normalize_unit_unknown_returns_none() -> None:
    assert normalize_unit("furlongs") is None


def test_normalize_unit_none_input() -> None:
    assert normalize_unit(None) is None


def test_normalize_unit_lowercase_slash() -> None:
    assert normalize_unit("ug/ml") == "ug/mL"