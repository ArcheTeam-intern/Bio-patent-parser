"""MinerU figure/caption projection (2026-10-19 live-diff finding).

The live 12-page paper comparison exposed a projection gap: MinerU's
``content_list`` returns ``image`` / ``chart`` items whose title lives in
``image_caption`` / ``chart_caption`` and whose ``content``/``text`` is
empty. The old ``_project_content_list`` fell through to
``if not text: continue`` and dropped every visual region AND every
``FIGURE n`` caption — the normalized MinerU view reported ``figures=0``
while the payload carried 5 images + 37 charts, and eight figure captions
disappeared from the text layer.

These tests pin the corrected contract:

- every ``image``/``chart`` with a usable bbox becomes a Docling-shaped
  ``pictures`` entry (croppable, with a projected bbox);
- the caption ALSO re-enters the text layer as a page-level block
  (``bbox=None`` — no synthetic fine-grained coordinates);
- ``normalize_docling_document`` resolves the caption reference into
  ``Figure.caption``;
- an un-croppable region still keeps its caption in the text layer;
- a payload without visual regions is unchanged (backward compatible).
"""

from __future__ import annotations

from bio_pdf.parsers.mineru_adapter import (
    _project_content_list,
    _project_mineru_payload,
)
from bio_pdf.parsers.normalize_docling import normalize_docling_document

_PAGE_SIZE = (595.0, 780.0, 0)


def _content_list_with_figures() -> list[dict]:
    return [
        {"type": "text", "page_idx": 0, "text": "Body paragraph one.",
         "bbox": [50, 80, 500, 100]},
        {"type": "text", "page_idx": 0, "text": "Body paragraph two.",
         "bbox": [50, 110, 500, 130]},
        # Uncaptioned chart (a sub-panel).
        {"type": "chart", "page_idx": 0, "bbox": [120, 300, 400, 500],
         "content": "", "chart_caption": [], "chart_footnote": [],
         "img_path": "images/chart-a.jpg"},
        # Captioned image carrying the real FIGURE title.
        {"type": "image", "page_idx": 0, "bbox": [250, 450, 520, 700],
         "content": "",
         "image_caption": ["FIGURE 1 | Growth inhibition of ATCC bacteria."],
         "image_footnote": [], "img_path": "images/fig1.jpg"},
        # A sub-panel label only.
        {"type": "chart", "page_idx": 0, "bbox": [300, 320, 520, 430],
         "content": "", "chart_caption": ["A"], "chart_footnote": [],
         "img_path": "images/panel-a.jpg"},
        {"type": "text", "page_idx": 0, "text": "Body paragraph three.",
         "bbox": [50, 740, 500, 760]},
    ]


def test_image_and_chart_items_become_pictures() -> None:
    pages = _project_content_list(
        _content_list_with_figures(),
        page_order=[1],
        page_sizes={1: _PAGE_SIZE},
    )
    page = pages["pages"][0]
    assert [p["label"] for p in page["pictures"]] == ["chart", "picture", "chart"]
    # Every picture carries a projected bbox (0–1000 → pt).
    for pic in page["pictures"]:
        bbox = pic["prov"][0]["bbox"]
        assert 0 <= bbox["l"] < bbox["r"] <= _PAGE_SIZE[0] + 2
        assert 0 <= bbox["t"] < bbox["b"] <= _PAGE_SIZE[1] + 2


def test_figure_caption_returns_to_the_text_layer_as_a_page_level_block() -> None:
    pages = _project_content_list(
        _content_list_with_figures(),
        page_order=[1],
        page_sizes={1: _PAGE_SIZE},
    )
    page = pages["pages"][0]
    captions = [t for t in page["texts"] if t["label"] == "caption"]
    assert [c["text"] for c in captions] == [
        "FIGURE 1 | Growth inhibition of ATCC bacteria.",
        "A",
    ]
    # No synthetic fine-grained bbox: the payload has ONE box for the
    # whole region, so the caption block stays page-level.
    for cap in captions:
        assert cap["bbox"] is None
        assert cap["bbox_verified"] is False
    # The real FIGURE title is referenced by its picture (the sub-panel
    # label "A" stays a standalone page-level block).
    fig1 = next(p for p in page["pictures"] if p["label"] == "picture")
    ref = fig1["captions"][0]["$ref"]
    referenced = next(t for t in page["texts"] if t["self_ref"] == ref)
    assert referenced["text"] == "FIGURE 1 | Growth inhibition of ATCC bacteria."
    panel = next(p for p in page["pictures"] if p["label"] == "chart" and p["captions"])
    panel_ref = panel["captions"][0]["$ref"]
    assert next(t for t in page["texts"] if t["self_ref"] == panel_ref)["text"] == "A"


def test_normalized_document_resolves_figure_captions() -> None:
    projected = _project_mineru_payload(
        {"content_list": _content_list_with_figures()},
        page_numbers={1},
        page_order=[1],
        page_sizes={1: _PAGE_SIZE},
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    doc = normalize_docling_document(
        projected, parser_run_id="audit:test", document_id="sha256:test"
    )
    page = doc.pages[0]
    captions = [f.caption for f in page.figures if f.caption]
    assert "FIGURE 1 | Growth inhibition of ATCC bacteria." in captions
    # The caption text is ALSO in the block layer (Docling parity).
    assert any(
        "FIGURE 1 | Growth inhibition" in (b.text_raw or "") for b in page.blocks
    ), [b.text_raw for b in page.blocks]


def test_uncroppable_region_still_keeps_its_caption_in_the_text_layer() -> None:
    items = [
        {"type": "image", "page_idx": 0, "content": "",
         "image_caption": ["FIGURE 9 | No box means page-level only."],
         "img_path": "images/fig9.jpg"},
    ]
    pages = _project_content_list(items, page_order=[1], page_sizes={1: _PAGE_SIZE})
    page = pages["pages"][0]
    assert page["pictures"] == []  # not croppable → not a figure
    assert [t["text"] for t in page["texts"]] == [
        "FIGURE 9 | No box means page-level only."
    ]


def test_payload_without_visual_regions_is_unchanged() -> None:
    items = [
        {"type": "text", "page_idx": 0, "text": "Only text.",
         "bbox": [50, 80, 500, 100]},
    ]
    pages = _project_content_list(items, page_order=[1], page_sizes={1: _PAGE_SIZE})
    page = pages["pages"][0]
    assert page["pictures"] == []
    assert [t["text"] for t in page["texts"]] == ["Only text."]
    assert [t["label"] for t in page["texts"]] == ["text"]


def test_caption_text_does_not_erase_its_own_figure() -> None:
    """``filter_exportable_figures`` drops regions >=60% covered by text.
    A caption block carrying the figure's OWN bbox would erase every
    captioned figure; the page-level block (bbox=None) must not.
    """

    projected = _project_mineru_payload(
        {"content_list": _content_list_with_figures()},
        page_numbers={1},
        page_order=[1],
        # A large page height keeps every region above the 2.5% floor.
        page_sizes={1: (900.0, 1200.0, 0)},
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    doc = normalize_docling_document(
        projected, parser_run_id="audit:test", document_id="sha256:test"
    )
    figures = doc.pages[0].figures
    # The captioned figure survives its own caption (bbox=None never
    # counts as text coverage).
    assert any(f.caption for f in figures), [f.figure_id for f in figures]
    # The 2.5% tiny-region floor still applies to small panels; this test
    # asserts survival, not that every sub-panel is exported.
    assert len(figures) >= 2


def test_captioned_figure_bbox_matches_the_payload_region() -> None:
    projected = _project_mineru_payload(
        {"content_list": _content_list_with_figures()},
        page_numbers={1},
        page_order=[1],
        page_sizes={1: _PAGE_SIZE},
    )
    page = projected["pages"][0]
    fig1 = next(p for p in page["pictures"] if p["label"] == "picture")
    bbox = fig1["prov"][0]["bbox"]
    # 0–1000 normalised frame → page pt, per axis.
    expected_l = 250 * _PAGE_SIZE[0] / 1000
    expected_t = 450 * _PAGE_SIZE[1] / 1000
    expected_r = 520 * _PAGE_SIZE[0] / 1000
    expected_b = 700 * _PAGE_SIZE[1] / 1000
    assert abs(bbox["l"] - expected_l) < 1.0
    assert abs(bbox["t"] - expected_t) < 1.0
    assert abs(bbox["r"] - expected_r) < 1.0
    assert abs(bbox["b"] - expected_b) < 1.0
