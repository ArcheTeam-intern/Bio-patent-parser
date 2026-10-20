"""Evidence geometry for parsers without per-cell boxes (MinerU).

MinerU returns a table's own bbox but NO per-cell coordinates. Before
this, every activity value degraded to PAGE precision, so "jump to the
value" only moved the viewer to the right page with no highlighted area.

The contract stays honest:

- a real cell box → ``geometry_precision="cell"``;
- only the table box → ``"block"`` (the TABLE is highlighted; nobody
  claims to know where inside it the cell sits);
- nothing → ``"page"``.

A cell box from an UNVERIFIED frame is withheld (existing G09 rule) and
the table box is withheld with it.
"""

from __future__ import annotations

from bio_pdf.extract.evidence import span_from_cell


def _span(*, bbox=None, table_bbox=None):
    return span_from_cell(
        document_id="sha256:test",
        parser_run_id="run:test",
        page_number=23,
        table_id="23:t1",
        cell_id="23:t1:c6",
        text_raw=">128",
        bbox=bbox,
        table_bbox=table_bbox,
    )


def test_cell_box_yields_cell_precision() -> None:
    span = _span(bbox=(10.0, 20.0, 40.0, 30.0), table_bbox=(5.0, 5.0, 500.0, 700.0))
    assert span.geometry_precision == "cell"
    assert span.location_precision == "cell"
    assert span.bbox == (10.0, 20.0, 40.0, 30.0)


def test_table_box_only_yields_block_precision() -> None:
    """MinerU's case: the table region is known, the cell area is not."""

    span = _span(bbox=None, table_bbox=(5.0, 5.0, 500.0, 700.0))
    assert span.geometry_precision == "block"
    assert span.location_precision == "block"
    # The span locates the TABLE, never a fabricated cell rectangle.
    assert span.bbox == (5.0, 5.0, 500.0, 700.0)
    assert span.fragments[0].bbox == (5.0, 5.0, 500.0, 700.0)
    assert span.fragments[0].cell_id == "23:t1:c6"


def test_no_boxes_falls_back_to_page() -> None:
    span = _span(bbox=None, table_bbox=None)
    assert span.geometry_precision == "page"
    assert span.location_precision == "page"
    assert span.bbox is None
    # Page identity is still present — the viewer can at least navigate.
    assert span.page_number == 23


def test_text_precision_is_independent_of_geometry() -> None:
    span = _span(bbox=None, table_bbox=(5.0, 5.0, 500.0, 700.0))
    assert span.text_precision == "block"
    assert span.raw_text == ">128"
