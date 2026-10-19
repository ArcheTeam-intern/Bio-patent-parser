"""docling_child sparse-run merge: unique item identity across runs.

The live EP3423477 run requested pages {23,24,26,29,30,31,32} — three
contiguous runs. Docling numbers every run's items from ``#/texts/0``, so
the old flat concatenation produced several items with the SAME
``self_ref`` but different content, and the normalizer refused the whole
document with ``NORMALIZATION_ID_COLLISION`` (the Docling side of that
comparison failed).

This pins the corrected merge contract:

- every merged item gets a fresh global ``self_ref``;
- ``captions``/``footnotes`` refs are rewritten through the SAME run's
  map, so two runs reusing ``#/texts/0`` never cross-link;
- page filtering still drops items outside the requested set;
- the merged document normalizes without an id collision.
"""

from __future__ import annotations

from bio_pdf.parsers.docling_child import _merge_run_documents
from bio_pdf.parsers.normalize_docling import normalize_docling_document


def _run_doc(texts: list[str], picture_page: int, extra_page: int) -> dict:
    """A Docling 2.x shaped per-run export: refs restart at 0 every run."""

    return {
        "pages": {
            str(picture_page): {"page_no": picture_page, "size": {"width": 595.0, "height": 842.0}},
            str(extra_page): {"page_no": extra_page, "size": {"width": 595.0, "height": 842.0}},
        },
        "texts": [
            {
                "self_ref": f"#/texts/{i}",
                "label": "text",
                "prov": [{"page_no": picture_page}],
                "text": text,
            }
            for i, text in enumerate(texts)
        ],
        "tables": [],
        "pictures": [
            {
                "self_ref": "#/pictures/0",
                "label": "picture",
                "prov": [
                    {
                        "page_no": picture_page,
                        "bbox": {"l": 50.0, "t": 100.0, "r": 350.0, "b": 400.0,
                                 "coord_origin": "TOPLEFT"},
                    }
                ],
                "captions": [{"$ref": "#/texts/0"}],
            }
        ],
    }


def test_self_refs_are_unique_across_runs() -> None:
    merged = _merge_run_documents(
        [_run_doc(["run1 caption"], 23, 24), _run_doc(["run2 caption"], 29, 30)],
        requested={23, 24, 29, 30},
    )
    text_refs = [t["self_ref"] for t in merged["texts"]]
    assert text_refs == ["#/texts/0", "#/texts/1"]
    assert len(set(text_refs)) == len(text_refs)
    pic_refs = [p["self_ref"] for p in merged["pictures"]]
    assert pic_refs == ["#/pictures/0", "#/pictures/1"]


def test_self_refs_are_unique_within_a_single_multi_item_run() -> None:
    """The recheck's failure mode: a run carries MANY items but ``merged``
    only grows at run end — every item in that run was assigned the same
    index. Every item must get its own ref, and the next run must continue
    after it."""

    merged = _merge_run_documents(
        [
            _run_doc(["a1", "a2", "a3"], 23, 24),
            _run_doc(["b1", "b2"], 29, 30),
        ],
        requested={23, 24, 29, 30},
    )
    text_refs = [t["self_ref"] for t in merged["texts"]]
    assert text_refs == [
        "#/texts/0", "#/texts/1", "#/texts/2", "#/texts/3", "#/texts/4",
    ]
    assert len(set(text_refs)) == 5
    # Within-run captions still resolve to their own run's first item.
    by_ref = {t["self_ref"]: t["text"] for t in merged["texts"]}
    assert by_ref[merged["pictures"][0]["captions"][0]["$ref"]] == "a1"
    assert by_ref[merged["pictures"][1]["captions"][0]["$ref"]] == "b1"


def test_captions_resolve_within_their_own_run() -> None:
    merged = _merge_run_documents(
        [_run_doc(["run1 caption"], 23, 24), _run_doc(["run2 caption"], 29, 30)],
        requested={23, 24, 29, 30},
    )
    by_ref = {t["self_ref"]: t["text"] for t in merged["texts"]}
    for picture in merged["pictures"]:
        caption_ref = picture["captions"][0]["$ref"]
        assert caption_ref in by_ref
    # Run 1's picture → run 1's caption, run 2's picture → run 2's caption.
    assert by_ref[merged["pictures"][0]["captions"][0]["$ref"]] == "run1 caption"
    assert by_ref[merged["pictures"][1]["captions"][0]["$ref"]] == "run2 caption"


def test_items_outside_the_requested_pages_are_dropped() -> None:
    doc = _run_doc(["caption"], 23, 24)
    doc["texts"].append(
        {
            "self_ref": "#/texts/1",
            "label": "text",
            "prov": [{"page_no": 99}],
            "text": "outside the request",
        }
    )
    merged = _merge_run_documents([doc], requested={23, 24})
    texts = [t["text"] for t in merged["texts"]]
    assert "outside the request" not in texts
    assert texts == ["caption"]


def test_multiple_runs_normalize_without_id_collision() -> None:
    """The acceptance path: the merged document must survive the same
    uniqueness gate that refused the flat concatenation."""

    merged = _merge_run_documents(
        [_run_doc(["first run text"], 23, 24), _run_doc(["second run text"], 29, 30)],
        requested={23, 24, 29, 30},
    )
    document = normalize_docling_document(
        merged, parser_run_id="audit:sparse", document_id="sha256:patent"
    )
    assert sorted(p.page_number for p in document.pages) == [23, 24, 29, 30]
    block_ids = [b.block_id for p in document.pages for b in p.blocks]
    assert len(block_ids) == len(set(block_ids))
    # Both captions survived and resolved on the picture.
    captions = [f.caption for p in document.pages for f in p.figures]
    assert sorted(c for c in captions if c) == ["first run text", "second run text"]
