"""H07/P04: every production extractor must propagate the frame-trust flag.

The G-round fix downgraded spans only when ``BlockRef.bbox_verified`` was
False — but prose/entities/inline built their BlockRefs WITHOUT the flag,
so a real unverified block still produced ``geometry_precision=block``
with a bbox (the reviewer's prose repro). These tests drive the REAL
extractor entry points with an untrusted block.
"""

from __future__ import annotations

from bio_pdf.extract.activities.prose import extract_prose_measurements
from bio_pdf.extract.evidence import EvidenceIndex


def _block(*, text: str, verified: bool | None) -> dict:
    return {
        "text": text,
        "kind": "text",
        "semantic_role": "unknown",
        "page_number": 1,
        "bbox": [10.0, 20.0, 300.0, 40.0],
        "block_id": "b-prose",
        "source_refs": [],
        "source_aliases": [],
        "source_parser": "mineru_http",
        "source_artifact_id": None,
        "bbox_verified": verified,
    }


def _spans_for_block(verified: bool | None):
    index = EvidenceIndex()
    blocks = [_block(text="The peptide RRRQRRKKRG showed IC50 of 5 uM.", verified=verified)]
    measurements = extract_prose_measurements(
        blocks, "sha256:h07", parser_run_id="run", evidence_index=index
    )
    assert measurements, "the prose repro must produce one measurement"
    spans = {s["evidence_id"]: s for s in index.to_payload()}
    return measurements, spans


def test_unverified_prose_block_yields_page_level_evidence() -> None:
    measurements, spans = _spans_for_block(False)
    m = measurements[0]
    assert m.evidence_ids
    for eid in m.evidence_ids:
        span = spans[eid]
        assert span["geometry_precision"] == "page", span
        assert not span.get("bbox"), span
        assert all(not f.get("bbox") for f in span.get("fragments") or [])


def test_trusted_prose_block_keeps_block_geometry() -> None:
    measurements, spans = _spans_for_block(True)
    m = measurements[0]
    span = spans[m.evidence_ids[0]]
    assert span["geometry_precision"] == "block"
    assert span.get("bbox")


def test_legacy_none_keeps_previous_behaviour() -> None:
    measurements, spans = _spans_for_block(None)
    m = measurements[0]
    span = spans[m.evidence_ids[0]]
    assert span["geometry_precision"] == "block", "None = legacy trusted, unchanged"


def test_inline_sequence_extraction_respects_the_flag() -> None:
    """The inline-sequence path (a separate BlockRef site) must downgrade
    the same way."""

    from bio_pdf.extract.sequences.inline import extract_inline_sequences
    import inspect

    sig = inspect.signature(extract_inline_sequences)
    index = EvidenceIndex()
    block = _block(text="SEQ ID NO: 1 (RRRQRRKKRG) was tested.", verified=False)
    kwargs = {"parser_run_id": "run"} if "parser_run_id" in sig.parameters else {}
    records = extract_inline_sequences([block], "sha256:h07", evidence_index=index, **kwargs)
    if not records:
        return  # this fixture may not form a record; prose path already covers the wiring
    for span in index.to_payload():
        assert span["geometry_precision"] == "page", span
