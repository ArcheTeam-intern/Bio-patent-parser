"""MinerU paragraph-numbered block split (Chinese patent layout).

The Chinese patent CN114144196B prefixes every line with a paragraph
number ("[0009] <210> 1"). MinerU merges the whole listing into ONE
block, so the ST.25 parser — which requires a tag at the LINE HEAD —
saw zero declarations (0 sequences) while Docling, which keeps line
structure, recovered 2 partial ones.

Splitting at the paragraph marks (and stripping them) inside the
projection yields 5 declarations (4 complete) from the SAME cached
payload, which is strictly better than Docling on that document.

These tests pin the splitter and its boundary conditions.
"""

from __future__ import annotations

from bio_pdf.parsers.mineru_adapter import (
    _project_content_list,
    _project_mineru_payload,
    _split_paragraph_numbered,
)
from bio_pdf.parsers.normalize_docling import normalize_docling_document
from bio_pdf.workflow.handlers import _page_blocks
from bio_pdf.extract.sequences.inventory import build_sequence_inventory
from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan


def test_multi_paragraph_block_is_split_and_stripped() -> None:
    text = (
        "[0009] <210> 1 [0010] <211> 100 [0011] <212> PRT "
        "[0017] <400> [0018] Met Lys Thr Ile Ser"
    )
    pieces = _split_paragraph_numbered(text)
    assert pieces == [
        "<210> 1",
        "<211> 100",
        "<212> PRT",
        "<400>",
        "Met Lys Thr Ile Ser",
    ]


def test_single_paragraph_mark_only_strips_the_prefix() -> None:
    """A normal sentence with ONE bracket group is not a merged listing."""

    assert _split_paragraph_numbered("[0003] <120> 工程痘苗病毒") == ["<120> 工程痘苗病毒"]


def test_unmarked_text_is_untouched() -> None:
    assert _split_paragraph_numbered("Met Lys Thr Ile Ser") == ["Met Lys Thr Ile Ser"]


def test_block_starting_with_prose_keeps_the_prose() -> None:
    """``split`` returns the text before the first mark as element 0 — a
    block may start with prose, not with "[0001]"."""

    pieces = _split_paragraph_numbered(
        "序列表 [0002] <110> 公司 [0003] <120> 名称"
    )
    assert pieces[0] == "序列表"
    assert "<110> 公司" in pieces
    assert "<120> 名称" in pieces


def test_projection_splits_merged_chinese_block() -> None:
    items = [
        {
            "type": "text",
            "page_idx": 0,
            "bbox": [50, 80, 500, 700],
            "text": (
                "[0009] <210> 1 [0010] <211> 4 [0011] <212> PRT "
                "[0012] <213> Vaccinia virus [0017] <400> [0018] Ala Gly Leu Lys"
            ),
        }
    ]
    pages = _project_content_list(
        items, page_order=[1], page_sizes={1: (595.0, 780.0, 0)}
    )
    texts = [t["text"] for t in pages["pages"][0]["texts"]]
    assert texts == [
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<213> Vaccinia virus",
        "<400>",
        "Ala Gly Leu Lys",
    ]
    # Every piece keeps the ORIGINAL block's box — block-level precision,
    # never a fabricated per-line box.
    boxes = {str(t["bbox"]["t"]) for t in pages["pages"][0]["texts"]}
    assert len(boxes) == 1


def test_merged_chinese_listing_yields_a_complete_sequence() -> None:
    items = [
        {
            "type": "text",
            "page_idx": 0,
            "bbox": [50, 80, 500, 700],
            "text": (
                "[0009] <210> 1 [0010] <211> 4 [0011] <212> PRT "
                "[0012] <213> Vaccinia virus [0017] <400> [0018] Ala Gly Leu Lys"
            ),
        }
    ]
    projected = _project_mineru_payload(
        {"content_list": items}, page_numbers={1}, page_order=[1],
        page_sizes={1: (595.0, 780.0, 0)},
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    doc = normalize_docling_document(
        projected, parser_run_id="probe", document_id="sha256:zh"
    )
    blocks = _page_blocks(doc)
    _, scan = parse_st25_blocks_with_scan(blocks, doc.document_id)
    inventory = build_sequence_inventory(scan, requested_pages=[1])
    entries = inventory["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["declared_length"] == 4
    assert entry["observed_length"] == 4
    assert entry["molecule_type"] == "protein"
    assert entry["status"] == "complete"


def test_english_payload_without_marks_is_unchanged() -> None:
    items = [
        {"type": "text", "page_idx": 0, "bbox": [50, 80, 500, 100],
         "text": "Body paragraph one."},
        {"type": "table", "page_idx": 0, "bbox": [50, 200, 500, 400],
         "table_body": "<table><tr><td>A</td></tr></table>"},
    ]
    pages = _project_content_list(
        items, page_order=[1], page_sizes={1: (595.0, 780.0, 0)}
    )
    texts = [t["text"] for t in pages["pages"][0]["texts"]]
    assert texts == ["Body paragraph one."]
