"""Explicit OCR selection, readiness, and reproducible runtime metadata."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
from typing import Any

from bio_pdf.errors import EngineUnavailableError


def ocr_availability() -> dict[str, bool]:
    return {
        "tesseract": shutil.which("tesseract") is not None,
        "rapidocr": (
            importlib.util.find_spec("rapidocr") is not None
            and importlib.util.find_spec("onnxruntime") is not None
        ),
    }


def ocr_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in ("docling", "rapidocr", "onnxruntime"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def resolve_ocr_options(raw_options: dict[str, Any]) -> dict[str, Any]:
    """Auto selects an installed engine; explicit requests never switch."""
    requested = str(raw_options.get("ocr_engine") or "auto")
    available = ocr_availability()
    if requested == "auto":
        selected = next((name for name, ready in available.items() if ready), None)
    elif requested in available:
        selected = requested if available[requested] else None
    else:
        raise EngineUnavailableError(
            f"unsupported OCR engine: {requested}", details={"ocr_engine": requested}
        )
    if selected is None:
        raise EngineUnavailableError(
            f"OCR engine {requested!r} unavailable: install Tesseract or the rapidocr extra",
            details={"requested_engine": requested, "available": available},
        )

    language = str(raw_options.get("ocr_lang") or "en")
    if selected == "rapidocr":
        aliases = {
            "en": "iso:en", "eng": "iso:en", "english": "iso:en",
            "zh": "iso:zh-Hans", "zh-cn": "iso:zh-Hans", "zh-hans": "iso:zh-Hans",
            "chi_sim": "iso:zh-Hans", "ch": "iso:zh-Hans", "chinese": "iso:zh-Hans",
            "chi_tra": "iso:zh-Hant", "zh-tw": "iso:zh-Hant",
        }
        effective_language = aliases.get(language.lower(), language)
    else:
        effective_language = "eng" if language.lower() in {"en", "english"} else language
    return {
        "requested_engine": requested,
        "selected_engine": selected,
        "requested_language": language,
        "effective_language": effective_language,
        "backend": "onnxruntime" if selected == "rapidocr" else "cli",
        "fallback_reason": (
            "tesseract executable not found" if requested == "auto" and selected == "rapidocr" else None
        ),
        "versions": ocr_versions(),
    }
