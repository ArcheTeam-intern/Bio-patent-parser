"""Parse plan and page-range tests (plan T04 acceptance)."""

from __future__ import annotations

import pytest

from bio_pdf.errors import EngineUnavailableError
from bio_pdf.workflow.parse_plan import build_parse_plan, contiguous_runs


def _page(page_number: int, route: str) -> dict:
    return {
        "signals": {"page_number": page_number},
        "route": {"route": route, "reasons": [f"route={route}"]},
    }


def test_contiguous_runs_splits_sparse_pages() -> None:
    assert contiguous_runs([2, 4]) == [(2, 2), (4, 4)]
    assert contiguous_runs([1, 2, 3, 7]) == [(1, 3), (7, 7)]
    assert contiguous_runs([]) == []
    assert contiguous_runs([5]) == [(5, 5)]


def test_plan_routes_scanned_pages_to_ocr_under_auto() -> None:
    pages = [_page(1, "native"), _page(2, "scan_suspect"), _page(3, "blank")]
    plan = build_parse_plan(
        pages, page_numbers=(1, 2, 3), engine="docling_sdk", ocr_policy="auto"
    )
    kinds = {t.page_number: t.kind for t in plan.tasks}
    assert kinds[1] == "native"
    assert kinds[2] == "layout_ocr"
    assert kinds[3] == "blank"


def test_plan_force_disabled_have_different_effective_plans() -> None:
    pages = [_page(1, "native"), _page(2, "scan_suspect")]

    with pytest.raises(EngineUnavailableError):
        build_parse_plan(
            pages, page_numbers=(1, 2), engine="native", ocr_policy="force"
        )

    forced = build_parse_plan(
        [_page(1, "native")], page_numbers=(1,), engine="docling_sdk", ocr_policy="force"
    )
    assert forced.tasks[0].kind == "layout_ocr"

    disabled = build_parse_plan(
        pages, page_numbers=(1, 2), engine="docling_sdk", ocr_policy="disabled"
    )
    scan_task = next(t for t in disabled.tasks if t.page_number == 2)
    assert scan_task.kind == "native"
    assert any("SCAN_WITH_OCR_DISABLED" in r for r in scan_task.reasons)
    assert disabled.warnings  # surfaced, not silent


def test_plan_unknown_route_defaults_native_with_reason() -> None:
    plan = build_parse_plan(
        [_page(1, "unknown")], page_numbers=(1,), engine="native", ocr_policy="auto"
    )
    assert plan.tasks[0].kind == "native"
    assert any("insufficient signals" in r for r in plan.tasks[0].reasons)
