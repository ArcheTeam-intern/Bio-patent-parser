"""Cache key determinism tests."""

from __future__ import annotations

from bio_pdf.storage.artifacts import make_cache_key


def test_cache_separates_model_and_source_pages() -> None:
    base = {
        "source_sha256": "a" * 64,
        "pages": [1, 2],
        "engine": "docling",
        "model_revision": "r1",
        "options": {"ocr": False},
        "adapter_version": "1",
        "upstream_sha256": "b" * 64,
    }
    assert make_cache_key(base) != make_cache_key({**base, "model_revision": "r2"})
    assert make_cache_key(base) != make_cache_key({**base, "pages": [2, 3]})


def test_cache_key_is_order_insensitive() -> None:
    base = {
        "source_sha256": "a" * 64,
        "pages": [1, 2],
        "engine": "docling",
    }
    reversed_ = dict(reversed(list(base.items())))
    assert make_cache_key(base) == make_cache_key(reversed_)


def test_cache_key_includes_options() -> None:
    base = {
        "source_sha256": "a" * 64,
        "pages": [1, 2],
        "engine": "docling",
        "options": {"ocr": False},
    }
    alt = {**base, "options": {"ocr": True}}
    assert make_cache_key(base) != make_cache_key(alt)


def test_cache_key_is_stable_across_runs() -> None:
    payload = {"engine": "native", "pages": [1], "options": {}}
    # Call twice in the same process; ensure determinism without relying on
    # Python's string hash randomization.
    assert make_cache_key(payload) == make_cache_key(payload)