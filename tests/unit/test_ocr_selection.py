"""OCR selection regressions for the uploaded scan (no model inference)."""

from __future__ import annotations

import importlib.util
import shutil

import pytest

from bio_pdf.errors import EngineUnavailableError
from bio_pdf.parsers.docling_adapter import _build_converter_from_request


@pytest.fixture()
def converter_options(monkeypatch):
    pytest.importorskip("docling")
    from docling import document_converter

    def capture(*, format_options):
        return next(iter(format_options.values())).pipeline_options

    monkeypatch.setattr(document_converter, "DocumentConverter", capture)
    return lambda options: _build_converter_from_request(
        {"profile": "layout_ocr", "parser_options": options}
    )


def availability(monkeypatch, *, tesseract=False, rapidocr=True):
    monkeypatch.setattr(shutil, "which", lambda _: "tesseract.exe" if tesseract else None)
    original = importlib.util.find_spec

    def spec(name, *args, **kwargs):
        if name in {"rapidocr", "onnxruntime"}:
            return object() if rapidocr else None
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", spec)


def test_auto_uses_rapidocr_when_tesseract_is_missing(monkeypatch, converter_options):
    availability(monkeypatch)
    options = converter_options({"ocr_engine": "auto", "ocr_lang": "en"})
    assert options.ocr_options.kind == "rapidocr"
    assert options.ocr_options.backend == "onnxruntime"
    assert options.ocr_options.lang == ["iso:en-Latn"]


def test_explicit_rapidocr_honors_chinese_language(monkeypatch, converter_options):
    availability(monkeypatch, tesseract=True)
    options = converter_options({"ocr_engine": "rapidocr", "ocr_lang": "chi_sim"})
    assert options.ocr_options.kind == "rapidocr"
    assert options.ocr_options.lang == ["iso:zh-Hans"]


def test_auto_keeps_available_tesseract(monkeypatch, converter_options):
    availability(monkeypatch, tesseract=True)
    options = converter_options({"ocr_engine": "auto", "ocr_lang": "en"})
    assert options.ocr_options.kind == "tesseract"
    assert options.ocr_options.lang == ["eng"]


def test_explicit_missing_tesseract_does_not_silently_switch(monkeypatch, converter_options):
    availability(monkeypatch)
    with pytest.raises(EngineUnavailableError, match="tesseract"):
        converter_options({"ocr_engine": "tesseract", "ocr_lang": "en"})


def test_missing_all_engines_fails_before_loading_models(monkeypatch, converter_options):
    availability(monkeypatch, rapidocr=False)
    with pytest.raises(EngineUnavailableError, match="OCR"):
        converter_options({"ocr_engine": "auto", "ocr_lang": "en"})
