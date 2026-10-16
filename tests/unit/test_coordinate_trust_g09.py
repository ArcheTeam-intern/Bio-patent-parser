"""G09/P04+P05: coordinate trust — protocol conversion, bottom-left
consistency, and evidence/API downgrade for unverified frames."""

from __future__ import annotations

from bio_pdf.extract.evidence import BlockRef, span_from_block_match


def _run_content_list(items, *, basis=None, page_size=(595.0, 842.0, 0)):
    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    projected = _project_mineru_payload(
        {"content_list": items, **({"coord_basis": basis} if basis else {})},
        page_numbers={1},
        page_order=[1],
        page_sizes={1: page_size},
        declared_basis=basis,
    )
    warnings = projected.pop("_projection_warnings", [])
    effective = projected.pop("_coordinate_basis", None) or basis
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    descriptor = _page_geometry_descriptor(
        document,
        page_sizes={1: page_size},
        declared_basis=effective,
        projection_warnings=warnings,
    )
    return document, descriptor, warnings


def test_bottom_left_declared_basis_flips_text_and_tables_consistently() -> None:
    """The recheck's isolation: with a declared bottom-left basis the TEXT
    flips correctly — the TABLE used to keep its raw box (forced TOPLEFT).
    Both must now be consistent."""

    items = [
        {"type": "text", "text": "Hello", "page_idx": 0, "bbox": [10, 20, 110, 70]},
        {
            "type": "table",
            "page_idx": 0,
            "bbox": [10, 20, 110, 70],
            "table_body": "<table><tr><td>MIC</td><td>4</td></tr></table>",
        },
    ]
    basis = {"unit": "pt", "origin": "bottom_left"}
    document, descriptor, _warnings = _run_content_list(
        items, basis=basis, page_size=(600.0, 800.0, 0)
    )
    page = document.pages[0]
    text = next(b for b in page.blocks if b.text_raw == "Hello")
    table = page.tables[0]
    # The normalizer flips BOTTOMLEFT using page height: t,b → 800-b, 800-t.
    assert list(text.bbox) == [10.0, 730.0, 110.0, 780.0]
    assert list(table.bbox) == [10.0, 730.0, 110.0, 780.0], "tables must use the SAME transform"
    assert descriptor["coordinate_basis_verified"] is True


def test_unverified_frame_evidence_degrades_to_page_level() -> None:
    """G09/P05: a span built from an unverified block keeps its page and
    characters but claims NO box — page-level navigation only."""

    block = BlockRef(
        document_id="doc",
        parser_run_id="run",
        block_id="b1",
        page_number=3,
        bbox=(10.0, 20.0, 100.0, 30.0),
        bbox_verified=False,
    )
    span = span_from_block_match(
        block,
        text_raw="Ala",
        char_start=0,
        char_end=3,
        full_text="Ala",
    )
    assert span.geometry_precision == "page"
    assert span.bbox is None
    assert span.location_precision == "page"
    assert all(fragment.bbox is None for fragment in span.fragments)

    # A trusted frame behaves exactly as before.
    trusted = BlockRef(
        document_id="doc",
        parser_run_id="run",
        block_id="b2",
        page_number=3,
        bbox=(10.0, 20.0, 100.0, 30.0),
    )
    span2 = span_from_block_match(
        trusted, text_raw="Ala", char_start=0, char_end=3, full_text="Ala"
    )
    assert span2.geometry_precision == "block"
    assert span2.bbox == (10.0, 20.0, 100.0, 30.0)


def test_rotated_page_keeps_raw_and_unverified() -> None:
    items = [{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [142, 108, 605, 122]}]
    document, descriptor, _warnings = _run_content_list(
        items, page_size=(595.0, 842.0, 90)
    )
    block = document.pages[0].blocks[0]
    assert list(block.bbox) == [142.0, 108.0, 605.0, 122.0], "rotated pages keep RAW values"
    assert block.bbox_verified is False
    assert descriptor["coordinate_basis_verified"] is False
    assert any("rotated" in note for note in descriptor["basis_notes"])
