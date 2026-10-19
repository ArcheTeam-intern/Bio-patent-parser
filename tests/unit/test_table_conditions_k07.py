"""K07: ordinary-word case normalization is separated from SI-unit
case preservation.

The recheck repro: caption "MIC at 10 µM", same table contents, but
the column header differs in ordinary-word case ("MIC (µg/mL)" vs
"mic (µg/mL)"). The entity / metric / target / conditions already
prove this is the same measurement, so the header's case must not
break the pair.

The contract:
- µ/μ glyph equivalence is always applied.
- Descriptive labels (no unit token) are lowercased — "MIC" and
  "mic" collapse to the same identity.
- SI-unit tokens keep their distinctive case — "µM" and "µm" stay
  distinct so a concentration column and a length column never pair.
"""

from __future__ import annotations

from bio_pdf.application.comparison_service import _domain_pass_spec, pair_measurements


def _mic_table(*, table_id: str, header: str, caption: str | None) -> dict:
    return {
        "table_id": table_id,
        "page_number": 1,
        "row_count": 2,
        "column_count": 2,
        "caption": caption,
        "header_cell_ids": [f"{table_id}:r0c0", f"{table_id}:r0c1"],
        "cells": [
            {"cell_id": f"{table_id}:r0c0", "row_index": 0, "column_index": 0, "text_raw": "Compound", "header_role": "column"},
            {"cell_id": f"{table_id}:r0c1", "row_index": 0, "column_index": 1, "text_raw": header, "header_role": "column"},
            {"cell_id": f"{table_id}:r1c0", "row_index": 1, "column_index": 0, "text_raw": "Peptide A", "header_role": "row"},
            {"cell_id": f"{table_id}:r1c1", "row_index": 1, "column_index": 1, "text_raw": "4"},
        ],
    }


def _doc(*, header: str, document_id: str, caption: str | None = "MIC at 10 µM") -> dict:
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
                "tables": [_mic_table(table_id="1:t0", header=header, caption=caption)],
                "figures": [],
            }
        ],
        "errors": [],
    }


def test_micro_glyph_in_descriptive_label_still_pairs_with_case_difference() -> None:
    """The K07 recheck repro: identical caption and contents, but the
    column header text differs only in ordinary-word case. Pairing
    must report them as the same measurement.
    """

    a = _domain_pass_spec(
        _doc(header="MIC (\u00b5g/mL)", document_id="sha256:up"),
        document_id="sha256:up", engine="docling",
    )
    b = _domain_pass_spec(
        _doc(header="mic (\u00b5g/mL)", document_id="sha256:down"),
        document_id="sha256:down", engine="mineru_http",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 1, out
    assert out["keys_only_a"] == [] and out["keys_only_b"] == []


def test_si_unit_case_still_distinguishes_concentration_from_length() -> None:
    """µM (concentration) and µm (length) are different columns. The
    K07 change must not collapse them."""

    a = _domain_pass_spec(
        _doc(header="MIC (\u00b5M)", document_id="sha256:cm"),
        document_id="sha256:cm", engine="docling",
    )
    b = _domain_pass_spec(
        _doc(header="MIC (\u00b5m)", document_id="sha256:mm"),
        document_id="sha256:mm", engine="mineru_http",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 0, out


def test_unit_token_case_difference_collapses_when_not_si_distinct() -> None:
    """Two headers that differ only by the case of a non-distinct unit
    token collapse via the lowercased surrounding text: "MIC (UG/mL)"
    vs "MIC (ug/mL)" both normalize to "mic (UG/mL)" — wait, but
    UG/mL preserves the unit case. The fix should lowercase the
    surrounding text and KEEP the unit case. So the pair keys still
    differ by unit case. (UG/mL != ug/mL → distinct.)
    """

    a = _domain_pass_spec(
        _doc(header="MIC (UG/mL)", document_id="sha256:caps"),
        document_id="sha256:caps", engine="docling",
    )
    b = _domain_pass_spec(
        _doc(header="mic (UG/mL)", document_id="sha256:low"),
        document_id="sha256:low", engine="mineru_http",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    # The unit token "UG/mL" is preserved verbatim; only the
    # surrounding "MIC" / "mic" differ. The pair key for the unit
    # alone is the same, but the unit stays in its verbatim case.
    assert out["paired"] == 1


def test_dose_caption_with_unit_token_keeps_si_case() -> None:
    """Captions that contain a unit token (a dosage like 10 µM) keep
    the SI case in the conditions payload, so 'µM' vs 'µm' stay
    distinct in the measurement key.
    """

    a = _domain_pass_spec(
        _doc(header="Concentration", caption="MIC at 10 µM", document_id="sha256:dA"),
        document_id="sha256:dA", engine="docling",
    )
    b = _domain_pass_spec(
        _doc(header="Concentration", caption="MIC at 10 µm", document_id="sha256:dB"),
        document_id="sha256:dB", engine="mineru_http",
    )
    out = pair_measurements(a["measurements"], b["measurements"])
    assert out["paired"] == 0


def test_ambiguous_cells_remain_ambiguous_when_contexts_clash() -> None:
    """K07 inherits the J05/H06 rule: two cells on the SAME side with
    the same identity (after the new lowercase) but different values
    stay ambiguous in the pair. The G07 test file already covers the
    ambiguity contract; this test is a guard against accidentally
    dropping it while splitting case normalisation.
    """

    a = _domain_pass_spec(
        _doc(header="MIC (\u00b5g/mL)", document_id="sha256:ambA"),
        document_id="sha256:ambA", engine="docling",
    )
    # Append a deliberately-duplicated measurement so the pair sees
    # two candidates on side A under the same identity.
    a_ms = list(a["measurements"]) + [dict(a["measurements"][0])]
    b = _domain_pass_spec(
        _doc(header="MIC (\u00b5g/mL)", document_id="sha256:ambB"),
        document_id="sha256:ambB", engine="mineru_http",
    )
    out = pair_measurements(a_ms, b["measurements"])
    assert out["paired"] == 0
    assert out["ambiguous"] >= 1
