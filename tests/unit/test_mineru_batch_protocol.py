"""C10: MinerU v4 batch protocol, identity resume, honest failures.

All HTTP is exercised through httpx.MockTransport — no network, no token.
The live cloud run is deliberately NOT simulated here; the execution report
marks it 未实测 (no MINERU_TOKEN configured in this environment).
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from bio_pdf.errors import (
    EngineProtocolUnsupportedError,
    EngineUnavailableError,
)
from bio_pdf.parsers.base import ParseRequest
from bio_pdf.parsers.mineru_adapter import (
    MinerUAdapter,
    _project_mineru_payload,
)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "x_content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "Hello world", "page_idx": 0},
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": "<table><tr><td>A</td><td>B</td></tr></table>",
                    },
                ]
            ),
        )
        zf.writestr("full.md", "# Hello\n")
    return buf.getvalue()


def _make_request(tmp_path: Path, pdf_bytes: bytes = b"%PDF-1.4 fake") -> ParseRequest:
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes)
    import hashlib

    sha = hashlib.sha256(pdf_bytes).hexdigest()
    return ParseRequest(
        job_id="job-x",
        document_id=f"sha256:{sha}",
        source_path=source,
        page_numbers=(1,),
        profile="layout_native",
        options_ref="configs/demo.toml",
        output_dir=tmp_path / "out",
    )


def test_batch_flow_uploads_polls_downloads_and_persists_identity(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            body = json.loads(request.content.decode())
            # Token must be present on platform calls only.
            assert request.headers.get("authorization", "").startswith("Bearer ")
            assert body["files"][0]["name"] == "source.pdf"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-1",
                        "file_urls": ["https://oss.example/put/1"],
                    },
                },
            )
        if request.method == "PUT" and url == "https://oss.example/put/1":
            # Signed upload URL: the platform token must NOT be forwarded.
            assert "authorization" not in {k.lower() for k in request.headers.keys()}
            return httpx.Response(200)
        if request.method == "GET" and url.endswith("/extract-results/batch/batch-1"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-1",
                        "extract_result": [
                            {
                                "file_name": "source.pdf",
                                "state": "done",
                                "full_zip_url": "https://oss.example/zip/1",
                                "extract_progress": {"extracted_pages": 1},
                            }
                        ],
                    },
                },
            )
        if request.method == "GET" and url == "https://oss.example/zip/1":
            return httpx.Response(200, content=_zip_bytes())
        raise AssertionError(f"unexpected request: {request.method} {url}")

    transport = httpx.MockTransport(handler)
    request = _make_request(tmp_path)
    adapter = MinerUAdapter(token="test-token", transport=transport, poll_interval=0.0)

    artifacts = adapter.parse(request, local_file=request.source_path)
    assert artifacts.normalized_path.is_file()
    assert artifacts.manifest_path.is_file()

    identity = json.loads((request.output_dir / "remote_identity.json").read_text())
    assert identity["batch_id"] == "batch-1"
    assert identity["file_name"] == "source.pdf"

    manifest = json.loads(artifacts.manifest_path.read_text())
    assert manifest["protocol"] == "v4-batch"
    assert manifest["engine"] == "mineru"
    assert manifest["remote_task_id"] == "batch-1"
    # The zipped content_list became real pages.
    normalized = json.loads(artifacts.normalized_path.read_text())
    assert any(p.get("blocks") for p in normalized["pages"])

    # RESUME: a second parse with the same output dir must NOT re-create the
    # remote task — it polls the persisted batch instead.
    uploads_before = [c for c in calls if c == ("POST", "https://mineru.net/api/v4/file-urls/batch")]
    adapter.parse(request, local_file=request.source_path)
    uploads_after = [c for c in calls if c == ("POST", "https://mineru.net/api/v4/file-urls/batch")]
    assert uploads_after == uploads_before, "resume must not re-upload"


def test_auth_failure_raises_engine_unavailable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "msg": "token invalid"})

    adapter = MinerUAdapter(
        token="bad", transport=httpx.MockTransport(handler), poll_interval=0.0
    )
    request = _make_request(tmp_path)
    with pytest.raises(EngineUnavailableError):
        adapter.parse(request, local_file=request.source_path)


def test_failed_state_reports_remote_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": {"batch_id": "b2", "file_urls": ["https://oss.example/put/2"]}},
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "data": {
                    "extract_result": [
                        {"file_name": "source.pdf", "state": "failed", "err_msg": "boom"}
                    ]
                }
            },
        )

    adapter = MinerUAdapter(
        token="t", transport=httpx.MockTransport(handler), poll_interval=0.0
    )
    request = _make_request(tmp_path)
    with pytest.raises(EngineUnavailableError) as excinfo:
        adapter.parse(request, local_file=request.source_path)
    assert "boom" in str(excinfo.value.details)


def test_markdown_only_result_is_rejected_not_faked_into_one_page() -> None:
    with pytest.raises(EngineProtocolUnsupportedError) as excinfo:
        _project_mineru_payload(
            {"markdown": "# whole document as one string"}, page_numbers={1}
        )
    assert excinfo.value.details["code"] == "MARKDOWN_ONLY_NO_PAGE_STRUCTURE"


def test_source_identity_mismatch_is_rejected_before_upload(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may be sent when the hash mismatches")

    adapter = MinerUAdapter(
        token="t", transport=httpx.MockTransport(handler), poll_interval=0.0
    )
    request = _make_request(tmp_path)
    tampered = ParseRequest(
        job_id=request.job_id,
        document_id="sha256:" + "0" * 64,
        source_path=request.source_path,
        page_numbers=request.page_numbers,
        profile=request.profile,
        options_ref=request.options_ref,
        output_dir=request.output_dir,
    )
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(tampered, local_file=tampered.source_path)


# ---- D06: stable identity across attempts, parameters travel ----------------


def _job_request(tmp_path: Path, attempt: str, *, pdf_bytes: bytes = b"%PDF-1.4 fake", **overrides) -> ParseRequest:
    """A request whose output_dir mimics a real per-attempt stage directory."""

    import hashlib as _h

    source = tmp_path / "source.pdf"
    rewrite = overrides.pop("rewrite_source", False)
    if rewrite or not source.exists():
        source.write_bytes(pdf_bytes)
    sha = _h.sha256(source.read_bytes()).hexdigest()
    out = tmp_path / "jobs" / "job-x" / "stage_runs" / attempt / "run-0"
    out.mkdir(parents=True, exist_ok=True)
    base = dict(
        job_id="job-x",
        document_id=f"sha256:{sha}",
        source_path=source,
        page_numbers=(30, 31, 32),
        profile="layout_ocr",  # type: ignore[arg-type]
        options_ref="configs/demo.toml",
        output_dir=out,
        parser_options={"ocr_lang": "ch", "do_table_structure": False},
    )
    base.update(overrides)
    return ParseRequest(**base)  # type: ignore[arg-type]


def _counting_handler(calls: list, bodies: list):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            calls.append("post")
            bodies.append(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={"data": {"batch_id": f"b{len(calls)}", "file_urls": [f"https://oss.example/put/{len(calls)}"]}},
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET" and "/extract-results/batch/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {
                                "file_name": "source.pdf",
                                "state": "done",
                                "full_zip_url": "https://oss.example/zip/x",
                            }
                        ]
                    }
                },
            )
        if request.method == "GET" and url == "https://oss.example/zip/x":
            return httpx.Response(200, content=_zip_bytes())
        raise AssertionError(f"unexpected: {request.method} {url}")

    return handler


def test_identity_survives_a_new_attempt_directory(tmp_path: Path) -> None:
    """D06 repro: a real workflow retry creates a NEW attempt dir — the
    remote task must NOT be submitted twice."""

    calls: list[str] = []
    bodies: list[dict] = []
    adapter = MinerUAdapter(
        token="t", transport=httpx.MockTransport(_counting_handler(calls, bodies)), poll_interval=0.0
    )
    first = _job_request(tmp_path, "attempt-1", pdf_bytes=b"%PDF-1.4 v1", rewrite_source=True)
    adapter.parse(first, local_file=first.source_path)

    second = _job_request(tmp_path, "attempt-2")  # same job, new attempt dir
    adapter.parse(second, local_file=second.source_path)

    assert calls.count("post") == 1, f"resume across attempts must poll, not resubmit: {calls}"

    # The stable identity lives at the JOB level, not inside an attempt.
    stable = tmp_path / "jobs" / "job-x" / "mineru_remote_identity.json"
    assert stable.is_file()
    identity = json.loads(stable.read_text(encoding="utf-8"))
    assert identity["phase"] == "completed"
    assert identity["source_sha256"]


def test_changed_source_or_params_submit_a_fresh_remote_task(tmp_path: Path) -> None:
    """D06 repro: reusing the output dir with a DIFFERENT source file or
    different options must create a new remote task, never mark the old
    result as verified for the new identity."""

    calls: list[str] = []
    bodies: list[dict] = []
    adapter = MinerUAdapter(
        token="t", transport=httpx.MockTransport(_counting_handler(calls, bodies)), poll_interval=0.0
    )
    first = _job_request(tmp_path, "attempt-1", pdf_bytes=b"%PDF-1.4 v1", rewrite_source=True)
    adapter.parse(first, local_file=first.source_path)
    assert calls.count("post") == 1

    # Same pages/options, but the source bytes changed.
    changed = _job_request(tmp_path, "attempt-2", pdf_bytes=b"%PDF-1.4 v2", rewrite_source=True)
    adapter.parse(changed, local_file=changed.source_path)
    assert calls.count("post") == 2, "a changed source must not reuse the old batch"

    # Same source again, but the parameters changed.
    other_params = _job_request(
        tmp_path, "attempt-3", parser_options={"ocr_lang": "en", "do_table_structure": True}
    )
    adapter.parse(other_params, local_file=other_params.source_path)
    assert calls.count("post") == 3, "changed parameters must not reuse the old batch"


def test_effective_parameters_reach_the_upload_body(tmp_path: Path) -> None:
    """D06 repro: ocr_lang=ch / do_table_structure=false / layout_native must
    be visible in the request body (previously stuck at en/table=true/ocr)."""

    calls: list[str] = []
    bodies: list[dict] = []
    adapter = MinerUAdapter(
        token="t", transport=httpx.MockTransport(_counting_handler(calls, bodies)), poll_interval=0.0
    )
    request = _job_request(
        tmp_path,
        "attempt-1",
        profile="layout_native",
        parser_options={"ocr_lang": "ch", "do_table_structure": False},
    )
    adapter.parse(request, local_file=request.source_path)

    body = bodies[0]
    assert body["language"] == "ch"
    assert body["enable_table"] is False
    file_entry = body["files"][0]
    assert file_entry["is_ocr"] is False, "layout_native must not request OCR"
    assert file_entry["page_ranges"] == "30-32"

    # layout_ocr turns OCR on and the page ranges stay compact.
    calls.clear(); bodies.clear()
    second = _job_request(tmp_path, "attempt-2")
    adapter.parse(second, local_file=second.source_path)
    assert bodies[0]["files"][0]["is_ocr"] is True
    assert bodies[0]["files"][0]["page_ranges"] == "30-32"


# ---- E7/P03: comparison measurement pairing -------------------------------


def _mic(value, row_ctx, unit="ug/ml"):
    return {
        "entity": "compoundX", "metric": "MIC", "target": "S. aureus",
        "value_raw": value, "unit_raw": unit, "unit_normalized": unit,
        "row_context": row_ctx, "column_context": None,
        "table_id": "1:t0", "page_number": 1,
    }


def test_reordered_identical_measurements_pair_without_diffs() -> None:
    """Recheck repro: A/B both {MIC 1ug/ml@1h, MIC 2ug/ml@2h} in opposite
    array order → zero value diffs and zero ambiguous force-pairs."""

    from bio_pdf.application.comparison_service import pair_measurements

    ms_a = [_mic("1", "1h"), _mic("2", "2h")]
    ms_b = [_mic("2", "2h"), _mic("1", "1h")]
    out = pair_measurements(ms_a, ms_b)
    assert out["paired"] == 2
    assert out["value_diffs"] == []
    assert out["ambiguous"] == 0
    assert out["keys_only_a"] == [] and out["keys_only_b"] == []


def test_repeated_identity_is_ambiguous_never_force_paired() -> None:
    from bio_pdf.application.comparison_service import pair_measurements

    ms_a = [_mic("1", "same"), _mic("9", "same")]  # same identity, two rows
    ms_b = [_mic("1", "same")]
    out = pair_measurements(ms_a, ms_b)
    assert out["ambiguous"] == 1
    # Ambiguous groups never produce a force-paired value diff.
    assert out["value_diffs"] == []
    assert out["paired"] == 0


def test_numeric_format_difference_is_not_a_value_difference() -> None:
    from bio_pdf.application.comparison_service import pair_measurements

    out = pair_measurements([_mic("1.0", "1h")], [_mic("1", "1h")])
    assert out["value_diffs"] == [] and out["paired"] == 1

    out = pair_measurements([_mic("1", "1h", unit="µg/mL")], [_mic("1", "1h", unit="ug/ml")])
    assert out["value_diffs"] == []

    out = pair_measurements([_mic("1", "1h")], [_mic("2", "1h")])
    assert len(out["value_diffs"]) == 1


# ---- E8/P04: page geometry is validated, never assumed --------------------


def _a4_zip_bytes() -> bytes:
    import io
    import zipfile

    content = json.dumps(
        [
            {"type": "text", "text": "Hello", "page_idx": 0, "bbox": [0, 0, 1000, 1000]},
            {"type": "text", "text": "World", "page_idx": 0, "bbox": [50, 50, 200, 70]},
        ]
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x_content_list.json", content)
        zf.writestr("full.md", "# x")
    return buf.getvalue()


def test_oversized_bbox_is_flagged_not_trusted(tmp_path: Path) -> None:
    """Recheck repro: A4 page with bbox [0,0,1000,1000] must produce an
    explicit geometry warning; the manifest never claims verified geometry."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as _canvas

    pdf = tmp_path / "source.pdf"
    cv = _canvas.Canvas(str(pdf), pagesize=A4)
    cv.drawString(50, 800, "x")
    cv.save()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            return httpx.Response(
                200,
                json={"data": {"batch_id": "bg", "file_urls": ["https://oss.example/put/g"]}},
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if "/extract-results/batch/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {"file_name": "source.pdf", "state": "done", "full_zip_url": "https://oss.example/zip/g"}
                        ]
                    }
                },
            )
        if url == "https://oss.example/zip/g":
            return httpx.Response(200, content=_a4_zip_bytes())
        raise AssertionError(url)

    import hashlib as _h

    request = ParseRequest(
        job_id="job-g",
        document_id="sha256:" + _h.sha256(pdf.read_bytes()).hexdigest(),
        source_path=pdf,
        page_numbers=(1,),
        profile="layout_native",  # type: ignore[arg-type]
        options_ref="configs/demo.toml",
        output_dir=tmp_path / "out",
        parser_options={},
    )
    adapter = MinerUAdapter(
        token="t", transport=httpx.MockTransport(handler), poll_interval=0.0
    )
    artifacts = adapter.parse(request, local_file=pdf)

    normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
    errors = " ".join(normalized.get("errors", []))
    assert "exceeds" in errors and "unverified" in errors, normalized.get("errors")

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    geometry = manifest["page_geometry"]
    # The page size WAS real (A4) but a box violated it → not verified.
    assert geometry["basis"] == "source-pdf"
    assert geometry["verified"] is False
    assert geometry["bbox_warnings"]
