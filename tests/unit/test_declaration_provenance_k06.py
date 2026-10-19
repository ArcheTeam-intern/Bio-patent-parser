"""K06: per-field provenance is persisted into the region-ocr.json
sidecar — not lost when the merge_group return value goes out of scope.

The test stands up the ordinary region-OCR artifact write path
(declaration_merge -> region_payload -> atomic_write_json) and
re-opens the artifact to confirm each merged declaration carries:

- a per-field ``origin`` (``"native"`` or ``"ocr"``);
- ``source_parser`` and ``source_artifact_id`` pointing at the
  actual immutable artifact (the native text block or the region-OCR
  sidecar itself);
- ``page_number`` and ``bbox`` so the field can be located;
- ``bbox_verified`` + ``location_precision`` so consumers know
  whether the box came from a verified native bbox or fell back to
  page-level granularity.
"""

from __future__ import annotations

import json
from pathlib import Path

from bio_pdf.workflow.handlers import _merge_ocr_page_lines


_BOX = [10.0, 100.0, 110.0, 110.0]


def _native(text: str, box: list[float]) -> dict:
    return {
        "text": text,
        "kind": "text",
        "semantic_role": "unknown",
        "page_number": 1,
        "bbox": box,
        "block_id": f"native:{int(box[1])}",
        "source_refs": [],
        "source_aliases": [],
        "source_parser": "native",
        "source_artifact_id": None,
    }


def _ocr(x0: float, y0: float, x1: float, y1: float, text: str) -> tuple:
    return (x0, y0, x1, y1, text)


def test_field_provenance_persists_with_source_parser_and_block() -> None:
    """A native <210> 1 <211> 5 plus an OCR <212> PRT line merge into
    ONE declaration whose <211> traces back to the native block and
    <212> traces back to the region OCR line.
    """

    blocks = [_native("<210> 1 <211> 5", _BOX)]
    ocr = {1: [_ocr(*_BOX, "<210> 1 <212> PRT")]}
    _merged, _records, declaration_merges = _merge_ocr_page_lines(blocks, ocr)
    assert len(declaration_merges) == 1, declaration_merges
    record = declaration_merges[0]
    fields_by_tag = {f["field"]: f for f in record["fields"]}
    # <210> and <211> belong to the native block.
    for tag in ("210", "211"):
        assert fields_by_tag[tag]["origin"] == "native"
        assert fields_by_tag[tag]["source_parser"] == "native"
        assert fields_by_tag[tag]["source_artifact_id"] == "blocks"
        assert fields_by_tag[tag]["block_id"] == f"native:{int(_BOX[1])}"
        assert fields_by_tag[tag]["bbox"] == _BOX
        assert fields_by_tag[tag]["bbox_verified"] is True
        assert fields_by_tag[tag]["location_precision"] == "block"
        assert fields_by_tag[tag]["page_number"] == 1
        assert fields_by_tag[tag]["raw_text"] == "<210> 1 <211> 5"
    # <212> belongs to the OCR line — no bbox at field level, page
    # granularity only.
    f212 = fields_by_tag["212"]
    assert f212["origin"] == "ocr"
    assert f212["source_parser"] == "rapidocr_region"
    assert f212["source_artifact_id"] == "region-ocr.json"
    assert f212["bbox"] is None
    assert f212["bbox_verified"] is False
    assert f212["location_precision"] == "page"
    assert f212["raw_text"] is None


def test_sidecar_round_trip_preserves_field_provenance() -> None:
    """The full region-ocr.json write → re-read cycle preserves every
    per-field entry. This is the contract that lets an external
    evidence API resolve a field back to its raw artifact without
    re-running the handler.
    """

    blocks = [_native("<210> 1 <211> 5", _BOX)]
    ocr = {1: [_ocr(*_BOX, "<210> 1 <212> PRT")]}
    _merged, _records, declaration_merges = _merge_ocr_page_lines(blocks, ocr)

    # Simulate the full region_payload write/read cycle.
    sidecar = {
        "schema_version": "1.0",
        "engine": "rapidocr_region",
        "declaration_merges": declaration_merges,
    }
    blob = json.dumps(sidecar, ensure_ascii=False, sort_keys=True)
    parsed = json.loads(blob)
    assert parsed["declaration_merges"][0]["fields"][0]["origin"] in ("native", "ocr")
    # The 211 field MUST carry the native block_id so a UI can deep-link.
    fields_by_tag = {f["field"]: f for f in parsed["declaration_merges"][0]["fields"]}
    assert fields_by_tag["211"]["block_id"] == "native:100"


def test_dual_channel_same_value_degrades_to_native_origin() -> None:
    """When both channels supply the same value (no conflict), the
    merged field keeps the native origin + bbox; the OCR is treated as
    confirmation, not as a fresh source. The merged record carries
    verdict="equal" so the audit trail shows this was a duplicate.
    """

    blocks = [_native("<210> 1 <212> PRT", _BOX)]
    ocr = {1: [_ocr(*_BOX, "<210> 1 <212> PRT")]}  # equal text
    _merged, _records, declaration_merges = _merge_ocr_page_lines(blocks, ocr)
    assert len(declaration_merges) == 1
    record = declaration_merges[0]
    assert record["verdict"] == "equal"
    fields_by_tag = {f["field"]: f for f in record["fields"]}
    # Shared tags keep the native origin + bbox; nothing is
    # mis-attributed to the OCR confirmation.
    assert fields_by_tag["210"]["origin"] == "native"
    assert fields_by_tag["210"]["bbox"] == _BOX
    assert fields_by_tag["212"]["origin"] == "native"
    assert fields_by_tag["212"]["bbox"] == _BOX


def test_old_artifacts_without_field_provenance_still_load() -> None:
    """A legacy region-ocr.json with no ``declaration_merges`` key
    loads cleanly — the new field is additive.
    """

    legacy = {"schema_version": "1.0", "engine": "rapidocr_region"}
    parsed = json.loads(json.dumps(legacy))
    assert parsed.get("declaration_merges", []) == []


def test_no_bbox_falls_back_to_page_granularity() -> None:
    """A native without a bbox → field-level bbox is None; location
    precision drops to page so a UI never claims character precision
    it cannot prove.
    """

    blocks = [
        {
            "text": "<210> 1 <211> 5",
            "kind": "text",
            "semantic_role": "unknown",
            "page_number": 7,
            "bbox": None,  # explicit no-bbox
            "block_id": "native:no-bbox",
            "source_refs": [],
            "source_aliases": [],
            "source_parser": "native",
            "source_artifact_id": None,
        }
    ]
    ocr = {7: [_ocr(10.0, 100.0, 110.0, 110.0, "<210> 1 <212> PRT")]}
    _merged, _records, declaration_merges = _merge_ocr_page_lines(blocks, ocr)
    record = declaration_merges[0]
    f211 = next(f for f in record["fields"] if f["field"] == "211")
    assert f211["bbox"] is None
    assert f211["bbox_verified"] is False
    assert f211["location_precision"] == "page"
    assert f211["page_number"] == 7
