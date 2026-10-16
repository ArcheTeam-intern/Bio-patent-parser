"""G07/P02: production experiment-condition extraction.

The recheck repro: the comparison code could serialize conditions, but the
upstream table extraction hard-coded ``conditions=[]`` — so two tables
differing only by "incubation 1 h" vs "2 h" paired as identical
experiments. These tests go through the REAL extraction chain (normalized
document → _domain_pass_spec → pair_measurements), never a hand-filled
conditions field.
"""

from __future__ import annotations

from bio_pdf.application.comparison_service import _domain_pass_spec, pair_measurements
from bio_pdf.contracts import NormalizedDocument
from bio_pdf.workflow.handlers import _table_context_conditions


def _cell(cell_id: str, row: int, col: int, text: str, *, role: str = "unknown") -> dict:
    return {
        "cell_id": cell_id,
        "row_index": row,
        "column_index": col,
        "text_raw": text,
        "header_role": role,
    }


def _mic_table(*, table_id: str, caption: str | None) -> dict:
    return {
        "table_id": table_id,
        "page_number": 1,
        "row_count": 2,
        "column_count": 2,
        "caption": caption,
        "header_cell_ids": [f"{table_id}:r0c0", f"{table_id}:r0c1"],
        "cells": [
            _cell(f"{table_id}:r0c0", 0, 0, "Compound", role="column"),
            _cell(f"{table_id}:r0c1", 0, 1, "MIC (µg/mL)", role="column"),
            _cell(f"{table_id}:r1c0", 1, 0, "Peptide A", role="row"),
            _cell(f"{table_id}:r1c1", 1, 1, "4"),
        ],
    }


def _doc(*, caption: str | None, document_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "document_id": document_id,
        "parser_run_id": "stub:engine",
        "pages": [
            {
                "page_number": 1,
                "width_pt": 595.0,
                "height_pt": 842.0,
                "route": "native",
                "blocks": [],
                "tables": [_mic_table(table_id="1:t0", caption=caption)],
                "figures": [],
            }
        ],
        "errors": [],
    }


def test_caption_conditions_reach_production_measurements() -> None:
    """The production chain (normalized doc → domain pass) must put the
    caption's incubation time into the measurement's conditions."""

    summary = _domain_pass_spec(
        _doc(caption="MIC after incubation for 1 h", document_id="sha256:one"),
        document_id="sha256:one",
        engine="docling",
    )
    assert summary["measurements"], summary
    conds = summary["measurements"][0]["conditions"]
    assert any(c["kind"] == "time" and "1 h" in c["value"] for c in conds), conds
    assert summary["measurements"][0]["conditions_status"] == "extracted"


def test_footnote_reference_conditions_are_resolved() -> None:
    document = NormalizedDocument.model_validate(
        {
            **_doc(caption=None, document_id="sha256:two"),
        }
    )
    from bio_pdf.contracts import TextBlock

    document.pages[0].blocks.append(
        TextBlock.model_validate(
            {
                "block_id": "note-1",
                "kind": "text",
                "semantic_role": "unknown",
                "text_raw": "All assays performed at 37 °C by broth microdilution.",
                "bbox": [40.0, 700.0, 400.0, 710.0],
            }
        )
    )
    document.pages[0].tables[0].footnote_refs.append("note-1")
    conditions = _table_context_conditions(document.pages[0].tables[0], document)
    kinds = {c["kind"] for c in conditions}
    assert "temperature" in kinds and "method" in kinds
    assert all(c.get("block_id") == "note-1" for c in conditions)


def test_different_incubation_captions_are_not_identical_experiments() -> None:
    """The recheck's acceptance: same table, captions '1 h' vs '2 h' — the
    pairing must NOT report them as the same experiment (no value diff, no
    pair; each side stands alone)."""

    a = _domain_pass_spec(
        _doc(caption="MIC after incubation for 1 h", document_id="sha256:a"),
        document_id="sha256:a",
        engine="docling",
    )
    b = _domain_pass_spec(
        _doc(caption="MIC after incubation for 2 h", document_id="sha256:b"),
        document_id="sha256:b",
        engine="docling",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 0
    assert len(out["keys_only_a"]) == 1 and len(out["keys_only_b"]) == 1
    assert out["value_diffs"] == []


def test_same_caption_pairs_with_verified_conditions() -> None:
    a = _domain_pass_spec(
        _doc(caption="MIC after incubation for 1 h", document_id="sha256:a"),
        document_id="sha256:a",
        engine="docling",
    )
    b = _domain_pass_spec(
        _doc(caption="MIC after incubation for 1 h", document_id="sha256:b"),
        document_id="sha256:b",
        engine="docling",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 1
    assert out["pairs_with_unverified_conditions"] == 0


def test_conditionless_pair_is_reported_as_unverified() -> None:
    """No caption, no header conditions: the pair is COUNTED as 'conditions
    unverified' — never displayed as experimentally identical."""

    a = _domain_pass_spec(
        _doc(caption=None, document_id="sha256:a"), document_id="sha256:a", engine="docling"
    )
    b = _domain_pass_spec(
        _doc(caption=None, document_id="sha256:b"), document_id="sha256:b", engine="docling"
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 1
    assert out["pairs_with_unverified_conditions"] == 1
    assert "never displayed as experimentally identical" in out["conditions_note"]


def test_column_header_conditions_belong_to_their_own_column() -> None:
    """A time condition inside a COLUMN header applies only to that
    column's measurements (1 h vs 2 h columns in one table stay distinct)."""

    table = _mic_table(table_id="1:t0", caption=None)
    table["cells"][1] = _cell("1:t0:r0c1", 0, 1, "MIC at 1 h (µg/mL)", role="column")
    table["column_count"] = 3
    table["cells"].append(_cell("1:t0:r1c2", 1, 2, "9"))
    table["cells"].insert(2, _cell("1:t0:r0c2", 0, 2, "MIC at 2 h (µg/mL)", role="column"))
    doc = _doc(caption=None, document_id="sha256:cols")
    doc["pages"][0]["tables"] = [table]
    summary = _domain_pass_spec(doc, document_id="sha256:cols", engine="docling")
    by_col = {}
    for m in summary["measurements"]:
        key = m["column_context"] or ""
        by_col.setdefault(key, m)
    one_h = next(m for m in summary["measurements"] if m["value_raw"] == "4")
    two_h = next(m for m in summary["measurements"] if m["value_raw"] == "9")
    assert any(c["value"] == "1 h" for c in one_h["conditions"])
    assert any(c["value"] == "2 h" for c in two_h["conditions"])
    assert not any(c["value"] == "2 h" for c in one_h["conditions"])


# ---- H06/P03: condition identity is semantic, evidence is traceability ---


def test_same_condition_different_evidence_still_pairs() -> None:
    """The reviewer's production repro: identical '1 h' header conditions
    extracted via two engines (different evidence ids) must pair — and both
    sides keep their evidence references in the payload."""

    same_doc = _doc(caption=None, document_id="sha256:h06")
    same_doc["pages"][0]["tables"][0] = _mic_table(table_id="1:t0", caption=None)
    same_doc["pages"][0]["tables"][0]["cells"][1] = _cell(
        "1:t0:r0c1", 0, 1, "MIC at 1 h (µg/mL)", role="column"
    )
    a = _domain_pass_spec(same_doc, document_id="sha256:h06", engine="docling")
    b = _domain_pass_spec(same_doc, document_id="sha256:h06", engine="mineru_http")
    # The two engines produce different evidence ids for the same header.
    ev_a = [c["evidence"] for m in a["measurements"] for c in m["conditions"] if "evidence" in c]
    ev_b = [c["evidence"] for m in b["measurements"] for c in m["conditions"] if "evidence" in c]
    assert (ev_a != ev_b) or not ev_a, "repro requires engine-specific evidence ids"

    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 1
    assert out["keys_only_a"] == [] and out["keys_only_b"] == []
    # Evidence references are NOT deleted to make pairing work — the
    # payloads still carry them on both sides.
    assert ev_a or ev_b
    assert any("evidence" in c for m in a["measurements"] for c in m["conditions"])


def test_condition_value_difference_still_unpairs() -> None:
    a = _domain_pass_spec(
        _doc(caption="MIC after incubation for 1 h", document_id="sha256:a"),
        document_id="sha256:a",
        engine="docling",
    )
    b = _domain_pass_spec(
        _doc(caption="MIC after incubation for 2 h", document_id="sha256:b"),
        document_id="sha256:b",
        engine="mineru_http",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 0
    assert out["value_diffs"] == []
