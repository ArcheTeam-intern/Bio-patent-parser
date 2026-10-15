"""Offline tests for the Docling normaliser.

These tests never import the live ``docling`` SDK. They construct synthetic
Docling JSON payloads (modelled on what ``DoclingDocument.save_as_json``
produces) and verify the normaliser produces a contract-compliant
:class:`NormalizedDocument`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bio_pdf.parsers.normalize_docling import (
    NormalizationLossError,
    normalize_docling_document,
    normalise_docling_file,
)


def _minimal_page_dict(
    *,
    page_number: int = 1,
    width: float = 612.0,
    height: float = 792.0,
    items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "page": page_number,
        "width": width,
        "height": height,
        "size": {"width": width, "height": height},
        "texts": items or [],
    }


def test_normalize_handles_minimal_payload() -> None:
    payload = {"pages": [_minimal_page_dict()]}
    doc = normalize_docling_document(
        payload,
        parser_run_id="run-1",
        document_id="sha256:doc",
    )
    assert len(doc.pages) == 1
    assert doc.pages[0].route == "layout_native"


def test_normalize_preserves_top_left_bbox() -> None:
    payload = {
        "pages": [
            _minimal_page_dict(
                items=[
                    {
                        "self_ref": "p1/t1",
                        "label": "text",
                        "bbox": {"l": 50.0, "t": 60.0, "r": 200.0, "b": 80.0},
                        "text": "Met Lys Thr",
                    },
                ],
            ),
        ],
    }
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    block = doc.pages[0].blocks[0]
    assert block.bbox == (50.0, 60.0, 200.0, 80.0)
    assert block.text_raw == "Met Lys Thr"


def test_normalize_accepts_list_bbox() -> None:
    payload = {
        "pages": [
            _minimal_page_dict(
                items=[
                    {
                        "self_ref": "p1/t1",
                        "label": "text",
                        "bbox": [10.0, 20.0, 100.0, 40.0],
                        "text": "ABC",
                    },
                ],
            ),
        ],
    }
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    assert doc.pages[0].blocks[0].bbox == (10.0, 20.0, 100.0, 40.0)


def test_normalize_drops_unknown_bbox_without_crashing() -> None:
    payload = {
        "pages": [
            _minimal_page_dict(
                items=[
                    {
                        "self_ref": "p1/t1",
                        "label": "text",
                        "bbox": {"left": 50, "top": 60, "right": 200, "bottom": 80},
                        "text": "drop me",
                    },
                ],
            ),
        ],
    }
    # Wrong key names should drop the bbox, not silently misposition the
    # block. Text must still be preserved so the document is not lost.
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    block = doc.pages[0].blocks[0]
    assert block.bbox is None
    assert block.text_raw == "drop me"


def test_normalize_classifies_block_kinds() -> None:
    payload = {
        "pages": [
            _minimal_page_dict(
                items=[
                    {"self_ref": "p1/t", "label": "section_header", "text": "Title"},
                    {"self_ref": "p1/l", "label": "list_item", "text": "item"},
                    {"self_ref": "p1/p", "label": "picture", "text": "Figure 1: structure"},
                    {"self_ref": "p1/f", "label": "formula", "text": "E=mc^2"},
                    {"self_ref": "p1/s", "label": "sequence_listing", "text": "MKT"},
                ],
            ),
        ],
    }
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    kinds = [b.kind for b in doc.pages[0].blocks]
    assert "title" in kinds
    assert "list" in kinds
    assert "picture" in kinds
    assert "formula" in kinds
    assert "sequence_listing" in kinds


def test_normalize_keeps_blank_pages() -> None:
    payload = {"pages": [_minimal_page_dict(items=[])]}
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    assert doc.pages[0].blocks == []


def test_normalize_requires_pages_list() -> None:
    with pytest.raises(NormalizationLossError):
        normalize_docling_document({}, parser_run_id="x", document_id="sha256:d")


def test_normalize_rejects_invalid_page_size() -> None:
    payload = {"pages": [{"page": 1, "size": {"width": 0, "height": 0}}]}
    with pytest.raises(NormalizationLossError):
        normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")


def test_normalize_table_cells_and_headers(tmp_path: Path) -> None:
    payload = {
        "pages": [
            _minimal_page_dict(
                items=[],
                width=400.0,
                height=400.0,
            ),
        ],
    }
    # Inject a table manually.
    payload["pages"][0]["tables"] = [
        {
            "caption": "MIC table",
            "bbox": [10.0, 20.0, 200.0, 80.0],
            "cells": [
                {"row_index": 0, "column_index": 0, "text": "Peptide"},
                {"row_index": 0, "column_index": 1, "text": "MIC"},
                {"row_index": 1, "column_index": 0, "text": "A"},
                {"row_index": 1, "column_index": 1, "text": "2"},
            ],
        }
    ]
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    tables = doc.pages[0].tables
    assert len(tables) == 1
    table = tables[0]
    assert table.row_count == 2
    assert table.column_count == 2
    assert any(c.cell_id in table.header_cell_ids for c in table.cells)


def test_normalise_docling_file_round_trips(tmp_path: Path) -> None:
    payload = {"pages": [_minimal_page_dict()]}
    path = tmp_path / "docling.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    doc = normalise_docling_file(path, parser_run_id="x", document_id="sha256:d")
    assert len(doc.pages) == 1


def test_normalize_does_not_invent_bbox_when_missing() -> None:
    payload = {
        "pages": [
            _minimal_page_dict(
                items=[
                    {"self_ref": "p1/t1", "label": "text", "text": "no bbox here"},
                ],
            ),
        ],
    }
    doc = normalize_docling_document(payload, parser_run_id="x", document_id="sha256:d")
    block = doc.pages[0].blocks[0]
    assert block.bbox is None
    # The text is still preserved even though the bbox is missing.
    assert block.text_raw == "no bbox here"

def test_normalize_skips_empty_text_groups_instead_of_crashing() -> None:
    """Real regression: the OCR'd EP patent produced empty docling text
    groups; the strict TextBlock contract (text_raw non-empty) made every
    layout attempt die with a ValidationError. Empty groups carry no
    evidence — they are skipped, never filled with a placeholder."""

    payload = {
        "pages": [
            _minimal_page_dict(
                items=[
                    {
                        "self_ref": "#/texts/1",
                        "label": "caption",
                        "text": "",
                        "prov": [{"page_no": 1, "bbox": [10, 20, 100, 40]}],
                    },
                    {
                        "self_ref": "#/texts/2",
                        "label": "paragraph",
                        "text": "   ",
                        "prov": [{"page_no": 1, "bbox": [10, 50, 100, 70]}],
                    },
                    {
                        "self_ref": "#/texts/3",
                        "label": "paragraph",
                        "text": "Actual text",
                        "prov": [{"page_no": 1, "bbox": [10, 80, 100, 100]}],
                    },
                ]
            )
        ]
    }
    doc = normalize_docling_document(payload, parser_run_id="run-1", document_id="sha256:doc")
    blocks = doc.pages[0].blocks
    assert [b.text_raw for b in blocks] == ["Actual text"]


def test_normalize_extracts_figures_with_resolved_captions() -> None:
    """Real regression: docling detects charts/curves as `pictures` but the
    pipeline dropped them, so no statistical figure could ever be exported."""

    payload = {
        "pages": {"2": {"page": 2, "size": {"width": 612.0, "height": 792.0}}},
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Growth curve of strain X",
                "prov": [{"page_no": 2, "bbox": [60, 700, 400, 720]}],
            }
        ],
        "pictures": [
            {
                "self_ref": "#/pictures/0",
                "label": "chart",
                "prov": [
                    {
                        "page_no": 2,
                        "bbox": {"l": 60, "t": 420, "r": 400, "b": 300, "coord_origin": "BOTTOMLEFT"},
                    }
                ],
                "captions": [{"$ref": "#/texts/0"}],
            },
            {
                "self_ref": "#/pictures/1",
                "label": "picture",
                "prov": [
                    {
                        "page_no": 2,
                        "bbox": {"l": 60, "t": 200, "r": 300, "b": 60, "coord_origin": "BOTTOMLEFT"},
                    }
                ],
                "captions": [{"$ref": "#/texts/missing"}],
            },
        ],
    }
    doc = normalize_docling_document(
        payload, parser_run_id="run-1", document_id="sha256:doc"
    )
    figs = doc.pages[0].figures
    assert len(figs) == 2
    growth, other = figs
    assert growth.caption == "Growth curve of strain X"
    assert growth.kind == "plot"  # caption keyword, not the raw label
    assert growth.bbox[0] == pytest.approx(60.0)
    # BOTTOMLEFT bbox -> normalized top-left origin
    assert growth.bbox[1] == pytest.approx(792.0 - 420.0)
    assert other.caption is None  # unresolved $ref never invents a caption
    assert other.kind == "picture"
    assert [f.figure_id for f in figs] == ["2:fig0", "2:fig1"]
