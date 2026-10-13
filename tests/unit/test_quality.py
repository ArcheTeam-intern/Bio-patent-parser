"""Page quality classification tests."""

from __future__ import annotations

from bio_pdf.contracts import PageQuality, QualityConfig
from bio_pdf.pdf.quality import classify_page, inspect_page_signals


def _signals_for(
        *,
        page_number: int = 1,
        chars: str = "hello",
        image_area_pt2: float = 0.0,
        page_area_pt2: float = 100.0,
        repeated_char_ratio: float = 0.0,
        hidden_text_flag: bool = False,
) -> PageQuality:
    return inspect_page_signals(
        page_number=page_number,
        text_chars=chars,
        image_area_pt2=image_area_pt2,
        page_area_pt2=page_area_pt2,
        repeated_char_ratio=repeated_char_ratio,
        hidden_text_flag=hidden_text_flag,
    )


def test_text_suspect_when_replacement_ratio_high() -> None:
    chars = "hello �� world"
    signals = _signals_for(chars=chars, image_area_pt2=0, page_area_pt2=100)
    decision = classify_page(signals, QualityConfig(replacement_ratio=0.02))
    assert decision.route == "text_suspect"


def test_scan_suspect_when_image_dominant_low_text() -> None:
    signals = _signals_for(
        chars="", image_area_pt2=95.0, page_area_pt2=100.0
    )
    decision = classify_page(signals, QualityConfig(scan_image_coverage=0.8))
    assert decision.route == "scan_suspect"


def test_blank_when_no_text_and_no_image() -> None:
    signals = _signals_for(chars="", image_area_pt2=0.0, page_area_pt2=100.0)
    decision = classify_page(signals, QualityConfig())
    assert decision.route == "blank"


def test_mixed_when_text_plus_image_coverage() -> None:
    signals = _signals_for(
        chars="this page has plenty of running text alongside an image",
        image_area_pt2=40.0,
        page_area_pt2=100.0,
    )
    decision = classify_page(signals, QualityConfig())
    assert decision.route == "mixed"


def test_native_route_for_readable_page() -> None:
    signals = _signals_for(
        chars="This page contains normal paragraphs of running prose text.",
        image_area_pt2=5.0,
        page_area_pt2=100.0,
    )
    decision = classify_page(signals, QualityConfig())
    assert decision.route == "native"


def test_unknown_when_signal_low_no_image() -> None:
    signals = _signals_for(
        chars="abcdefghi", image_area_pt2=0.0, page_area_pt2=100.0
    )
    decision = classify_page(signals, QualityConfig())
    assert decision.route == "unknown"


def test_hidden_text_flag_is_suspect_not_native() -> None:
    signals = _signals_for(
        chars="plenty of text from hidden OCR layer but invisible",
        hidden_text_flag=True,
        image_area_pt2=0.0,
        page_area_pt2=100.0,
    )
    decision = classify_page(signals, QualityConfig())
    assert decision.route in {"text_suspect", "scan_suspect"}


def test_image_coverage_is_clamped() -> None:
    # Caller passes an over-saturated area; classifier must clamp to <=1.
    signals = _signals_for(chars="x", image_area_pt2=200.0, page_area_pt2=100.0)
    assert 0.0 <= signals.image_coverage <= 1.0


def test_classification_records_a_reason() -> None:
    signals = _signals_for(chars="normal text on a normal page")
    decision = classify_page(signals, QualityConfig())
    assert decision.reasons, "decision must explain why a route was chosen"


def test_signals_reject_non_positive_page_area() -> None:
    import pytest

    with pytest.raises(ValueError):
        inspect_page_signals(
            page_number=1, text_chars="x", image_area_pt2=0, page_area_pt2=0
        )