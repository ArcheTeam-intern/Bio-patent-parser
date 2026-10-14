"""B04 integration: the plan is EXECUTED — a mixed document dispatches one
adapter call per (profile, contiguous pages) run.

Uses a lightweight adapter spy (plan B04 explicitly allows this for
dispatch/parameter proof; real-engine runs are the separate mixed-OCR
check). The spy records every ParseRequest it receives and returns a
minimal NormalizedDocument for exactly the requested pages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bio_pdf.parsers.base import ParseArtifacts, ParseRequest
from bio_pdf.workflow.handlers import LayoutParseStage
from bio_pdf.workflow.stages import StageContext


def _page_payload(number: int) -> dict:
    return {
        "page_number": number,
        "width_pt": 612.0,
        "height_pt": 792.0,
        "route": "layout_native",
        "blocks": [
            {
                "block_id": f"docling:p{number}:b0",
                "kind": "text",
                "text_raw": f"page {number} text",
                "bbox": [10.0, 10.0, 200.0, 30.0],
            }
        ],
        "tables": [],
    }


class _SpyAdapter:
    """Records requests; writes per-request merged output."""

    requests: list[ParseRequest] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def parse(self, request: ParseRequest) -> ParseArtifacts:
        _SpyAdapter.requests.append(request)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.1.0",
            "document_id": request.document_id,
            "parser_run_id": f"{request.job_id}:docling",
            "pages": [_page_payload(p) for p in request.page_numbers],
            "errors": [],
        }
        normalized = request.output_dir / "normalized.json"
        normalized.write_text(json.dumps(payload), encoding="utf-8")
        raw = request.output_dir / "raw_response.json"
        raw.write_text("{}", encoding="utf-8")
        return ParseArtifacts(
            normalized_path=normalized, raw_response_path=raw, manifest_path=normalized
        )


def _stage_context(tmp_path: Path, pages: tuple[int, ...]) -> StageContext:
    output_dir = tmp_path / "jobs" / "job-1"
    (output_dir / "stage_runs" / "run-1").mkdir(parents=True, exist_ok=True)
    # Inspection geometry (blank pages take their size from here).
    from bio_pdf.storage.artifacts import atomic_write_json

    atomic_write_json(
        output_dir / "ingest",
        "inspection.json",
        {
            "document_id": "sha256:doc",
            "sha256": "doc",
            "bytes": 1,
            "page_count": len(pages),
            "is_encrypted": False,
            "pages": [
                {"page_number": p, "width_pt": 612.0, "height_pt": 792.0, "rotation_deg": 0}
                for p in pages
            ],
        },
    )
    return StageContext(
        job_id="job-1",
        document_id="sha256:doc",
        source_path=str(tmp_path / "x.pdf"),
        page_numbers=pages,
        output_dir=str(output_dir),
        options={"engine": "docling_sdk", "workflow_config": {}},
        stage_run_id="run-1",
        document_sha256="doc",
        inputs={},
    )


def _write_quality(context: StageContext, tasks: list[dict]) -> None:
    """Write the plan where the stage reads it (legacy inspect path)."""

    from bio_pdf.storage.artifacts import atomic_write_json

    atomic_write_json(
        Path(context.output_dir) / "inspect",
        "quality.json",
        {"schema_version": "1.1.0", "pages": [], "parse_plan": {"tasks": tasks, "requested_pages": list(context.page_numbers), "ocr_policy": "auto", "engine": "docling_sdk"}},
    )


@pytest.fixture(autouse=True)
def _patch_adapter(monkeypatch):
    _SpyAdapter.requests = []
    import bio_pdf.parsers.docling_adapter as mod

    monkeypatch.setattr(mod, "DoclingAdapter", _SpyAdapter)


def test_mixed_document_dispatches_ocr_for_scan_pages_only(tmp_path: Path) -> None:
    context = _stage_context(tmp_path, (1, 2, 3))
    _write_quality(
        context,
        [
            {"page_number": 1, "kind": "native", "route": "native", "reasons": []},
            {"page_number": 2, "kind": "layout_ocr", "route": "scan_suspect", "reasons": []},
            {"page_number": 3, "kind": "blank", "route": "blank", "reasons": []},
        ],
    )
    outcome = LayoutParseStage().run(context)
    assert outcome.artifact_refs, "stage must produce merged artifacts"

    # Dispatch proof: page 2 ran under the OCR profile, page 1 under layout.
    by_profile = {
        r.profile: tuple(r.page_numbers) for r in _SpyAdapter.requests
    }
    assert (2,) == by_profile.get("layout_ocr"), by_profile
    assert (1,) in [pages for pages in by_profile.values()], by_profile
    assert all(3 not in pages for pages in by_profile.values())  # blank skipped

    merged = json.loads((Path(context.stage_dir()) / "normalized.json").read_text("utf-8"))
    merged_pages = {p["page_number"]: p for p in merged["pages"]}
    # Page 3 is the blank page: synthesized with real geometry, no engine run.
    assert sorted(merged_pages) == [1, 2, 3]
    assert merged_pages[3]["route"] == "blank"
    assert merged_pages[3]["blocks"] == []
    assert merged_pages[3]["width_pt"] == 612.0  # real geometry, not guessed

    manifest = json.loads((Path(context.stage_dir()) / "manifest.json").read_text("utf-8"))
    planned = {e["page"]: e for e in manifest["planned_vs_actual"]}
    assert planned[2]["actual_profile"] == "layout_ocr"
    assert planned[1]["produced"] is True


def test_config_language_reaches_each_run(tmp_path: Path) -> None:
    context = _stage_context(tmp_path, (1,))
    context = StageContext(
        **{
            **context.__dict__,
            "options": {
                "engine": "docling_sdk",
                "workflow_config": {
                    "parser_options": {"ocr_lang": "chi_sim", "tableformer_mode": "accurate"}
                },
            },
        }
    )
    _write_quality(
        context,
        [{"page_number": 1, "kind": "layout_ocr", "route": "scan_suspect", "reasons": []}],
    )
    LayoutParseStage().run(context)
    assert _SpyAdapter.requests[0].parser_options["ocr_lang"] == "chi_sim"
    assert _SpyAdapter.requests[0].parser_options["tableformer_mode"] == "accurate"


def test_page_outside_its_run_is_recorded_not_relabelled(tmp_path: Path) -> None:
    """A run returning an unrequested page keeps the real number + issue."""

    class _LeakySpy(_SpyAdapter):
        def parse(self, request: ParseRequest) -> ParseArtifacts:
            artifacts = super().parse(request)
            payload = json.loads(artifacts.normalized_path.read_text("utf-8"))
            payload["pages"].append(_page_payload(99))  # engine leaked page 99
            artifacts.normalized_path.write_text(json.dumps(payload), encoding="utf-8")
            return artifacts

    import bio_pdf.parsers.docling_adapter as mod

    mod.DoclingAdapter = _LeakySpy  # type: ignore[assignment]

    context = _stage_context(tmp_path, (1,))
    _write_quality(
        context,
        [{"page_number": 1, "kind": "native", "route": "native", "reasons": []}],
    )
    LayoutParseStage().run(context)
    manifest = json.loads((Path(context.stage_dir()) / "manifest.json").read_text("utf-8"))
    out_of_run = [e for e in manifest["planned_vs_actual"] if e.get("actual") == "out_of_run_page"]
    assert out_of_run and out_of_run[0]["page"] == 99

    merged = json.loads((Path(context.stage_dir()) / "normalized.json").read_text("utf-8"))
    assert [p["page_number"] for p in merged["pages"]] == [1]  # 99 not merged
