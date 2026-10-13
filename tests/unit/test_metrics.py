"""Metrics tests."""

from __future__ import annotations

from bio_pdf.evaluation.metrics import character_error_rate, safe_ratio


def test_zero_denominator_is_unknown_not_perfect_accuracy() -> None:
    assert safe_ratio(0, 0) is None
    assert safe_ratio(0, 2) == 0.0
    assert safe_ratio(1, 2) == 0.5


def test_safe_ratio_handles_positive() -> None:
    assert safe_ratio(3, 4) == 0.75


def test_character_error_rate_exact_match_is_zero() -> None:
    assert character_error_rate("MKTG", "MKTG") == 0.0


def test_character_error_rate_substitution() -> None:
    # One substitution in 4 residues → 0.25.
    assert character_error_rate("MKTG", "MKTG") == 0.0
    assert character_error_rate("MKTG", "MKTA") == 0.25


def test_character_error_rate_empty_reference() -> None:
    assert character_error_rate("", "MKTG") == 1.0


def test_character_error_rate_length_difference() -> None:
    # "MKTG" vs "MKT" — one deletion, length 4 → 0.25.
    assert character_error_rate("MKTG", "MKT") == 0.25