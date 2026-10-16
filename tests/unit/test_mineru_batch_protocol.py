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

    # G08/P03: µ (U+00B5) vs u is a glyph equivalence; ug/mL vs ug/ml is a
    # SI CASE difference and must be reported, not flattened.
    out = pair_measurements([_mic("1", "1h", unit="µg/mL")], [_mic("1", "1h", unit="ug/mL")])
    assert out["value_diffs"] == []
    out = pair_measurements([_mic("1", "1h", unit="ug/mL")], [_mic("1", "1h", unit="ug/ml")])
    assert len(out["value_diffs"]) == 1

    out = pair_measurements([_mic("1", "1h")], [_mic("2", "1h")])
    assert len(out["value_diffs"]) == 1


# ---- E8/P04: page geometry is validated, never assumed --------------------


def _a4_zip_bytes() -> bytes:
    import io
    import zipfile

    # G09: [0,0,1000,1000] is a LEGAL content_list box (full page in the
    # 0–1000 protocol frame); the "oversized" acceptance needs a value that
    # cannot come from that protocol at all.
    content = json.dumps(
        [
            {"type": "text", "text": "Hello", "page_idx": 0, "bbox": [0, 0, 1500, 1200]},
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
    # F07/P03: the facts stay separate — the page size IS real (A4), the
    # bounds are violated, and the coordinate basis was never declared.
    assert geometry["basis"] == "source-pdf"
    assert geometry["page_size_verified"] is True
    assert geometry["bounds_valid"] is False
    assert geometry["coordinate_basis_verified"] is False
    # G09: the protocol basis IS declared, but the out-of-range box keeps it
    # from being verified (a partially converted frame is not advertised).
    assert geometry["declared_coordinate_basis"]["unit"] == "normalized_1000"
    assert geometry["bbox_warnings"]


# ---- F06/P02: cross-engine activity identity (no local table ids) ---------


def _mic_v2(
    value,
    row_ctx,
    *,
    unit="ug/ml",
    table_id="1:t0",
    anchor="header:mic|s. aureus",
    page=1,
    conditions=None,
    operand=None,
):
    m = {
        "entity": "compoundX", "metric": "MIC", "target": "S. aureus",
        "value_raw": value, "unit_raw": unit, "unit_normalized": unit,
        "row_context": row_ctx, "column_context": None,
        "table_id": table_id, "table_anchor": anchor, "page_number": page,
        "conditions": conditions or [],
    }
    if operand is not None:
        m["value_structured"] = operand
    return m


def test_local_table_id_change_does_not_break_pairing() -> None:
    """F06 recheck repro: same activity, conditions, page and value — only
    the LOCAL table id differs (each engine invents its own). Pairing must
    not flip from success to "everything missing"."""

    from bio_pdf.application.comparison_service import pair_measurements

    out = pair_measurements(
        [_mic_v2("1", "1h", table_id="1:t0")],
        [_mic_v2("1", "1h", table_id="0:t3")],
    )
    assert out["paired"] == 1
    assert out["keys_only_a"] == [] and out["keys_only_b"] == []
    assert out["value_diffs"] == []


def test_reordered_tables_pair_without_diffs() -> None:
    from bio_pdf.application.comparison_service import pair_measurements

    ms_a = [
        _mic_v2("1", "1h", table_id="1:t0", anchor="header:mic|s. aureus"),
        _mic_v2("2", "2h", table_id="2:t1", anchor="caption:kill kinetics"),
    ]
    ms_b = [
        _mic_v2("2", "2h", table_id="0:t4", anchor="caption:kill kinetics"),
        _mic_v2("1", "1h", table_id="0:t2", anchor="header:mic|s. aureus"),
    ]
    out = pair_measurements(ms_a, ms_b)
    assert out["paired"] == 2
    assert out["value_diffs"] == [] and out["ambiguous"] == 0


def test_split_table_with_duplicate_candidates_is_ambiguous_not_fake_diff() -> None:
    """A split/merged detection difference can leave duplicate candidates on
    one side; those must be reported as AMBIGUOUS with candidates — never
    force-paired into invented value differences (no dictionary last-wins)."""

    from bio_pdf.application.comparison_service import pair_measurements

    # B detected the same row twice (its table was split into two fragments).
    ms_b = [_mic_v2("1", "1h"), _mic_v2("1", "1h", table_id="0:t9")]
    out = pair_measurements([_mic_v2("1", "1h")], ms_b)
    assert out["paired"] == 0
    assert out["ambiguous"] == 1
    assert out["value_diffs"] == []
    assert out["ambiguous_candidates"][0]["candidates_b"][0]["value_raw"] == "1"


def test_different_experiment_conditions_are_not_force_paired() -> None:
    from bio_pdf.application.comparison_service import pair_measurements

    ms_a = [_mic_v2("1", "1h", conditions=[{"kind": "time", "value": "1 h"}])]
    ms_b = [_mic_v2("1", "1h", conditions=[{"kind": "time", "value": "2 h"}])]
    out = pair_measurements(ms_a, ms_b)
    assert out["paired"] == 0
    assert len(out["keys_only_a"]) == 1 and len(out["keys_only_b"]) == 1
    assert out["value_diffs"] == []


def test_unit_magnitude_difference_is_reported_not_flattened() -> None:
    """mg vs g is a 1000x difference — a reported UNIT difference, never
    flattened away by normalization."""

    from bio_pdf.application.comparison_service import pair_measurements

    out = pair_measurements(
        [_mic_v2("1", "1h", unit="mg/mL")],
        [_mic_v2("1", "1h", unit="ug/mL")],
    )
    assert len(out["value_diffs"]) == 1
    assert "unit" in out["value_diffs"][0]["kind"]
    assert out["value_diffs"][0]["unit_a"] == "mg/mL"
    assert out["value_diffs"][0]["unit_b"] == "ug/mL"


def test_operators_compare_structurally() -> None:
    """'≤5' vs '≤ 5' is formatting; '≤5' vs '5' is a real operator change."""

    from bio_pdf.application.comparison_service import pair_measurements

    le = {"kind": "scalar", "operator": "<=", "number": "5", "raw": "≤5"}
    le_spaced = {"kind": "scalar", "operator": "<=", "number": "5", "raw": "≤ 5"}
    plain = {"kind": "scalar", "operator": None, "number": "5", "raw": "5"}

    out = pair_measurements(
        [_mic_v2("≤5", "1h", operand=le)], [_mic_v2("≤ 5", "1h", operand=le_spaced)]
    )
    assert out["paired"] == 1 and out["value_diffs"] == []

    out2 = pair_measurements(
        [_mic_v2("≤5", "1h", operand=le)], [_mic_v2("5", "1h", operand=plain)]
    )
    assert len(out2["value_diffs"]) == 1
    assert out2["value_diffs"][0]["kind"] == "operator"
    assert out2["value_diffs"][0]["operator_a"] == "<="
    assert out2["value_diffs"][0]["operator_b"] is None


# ---- F07/P03: coordinate basis claims stay separate and honest ------------


def _zip_content(items) -> bytes:
    import io
    import zipfile

    # allow_nan keeps NaN in the payload — Python's json.loads reads it back,
    # reproducing a real protocol response with a non-finite box.
    content = json.dumps(items, allow_nan=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x_content_list.json", content)
        zf.writestr("full.md", "# x")
    return buf.getvalue()


def _mineru_parse(tmp_path: Path, items, *, monkeypatch=None, rotation=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as _canvas

    pdf = tmp_path / "source.pdf"
    cv = _canvas.Canvas(str(pdf), pagesize=A4)
    cv.drawString(50, 800, "x")
    cv.save()

    if monkeypatch is not None and rotation is not None:
        from bio_pdf.parsers import mineru_adapter as ma

        real = ma._source_page_geometry

        def rotated(source_path, pages):  # noqa: ANN001
            sizes = real(source_path, pages)
            return {p: (w, h, rotation) for p, (w, h, _r) in sizes.items()}

        monkeypatch.setattr(ma, "_source_page_geometry", rotated)

    zip_bytes = _zip_content(items)

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
            return httpx.Response(200, content=zip_bytes)
        raise AssertionError(url)

    import hashlib as _h

    request = ParseRequest(
        job_id="job-f07",
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
    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    return normalized, manifest


def test_content_list_coordinates_are_converted_per_protocol(tmp_path: Path) -> None:
    """G09/P04 (real protocol): content_list coordinates are 0–1000
    normalised by page size; they must be CONVERTED to page points and the
    basis verified — the recheck's real [142,108,605,122] sample style."""

    normalized, manifest = _mineru_parse(
        tmp_path, [{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [142, 108, 605, 122]}]
    )
    geometry = manifest["page_geometry"]
    assert geometry["page_size_verified"] is True
    assert geometry["coordinate_basis_verified"] is True
    assert geometry["basis_notes"] == []
    assert geometry["declared_coordinate_basis"]["unit"] == "normalized_1000"
    assert geometry["declared_coordinate_basis"]["protocol"] == "mineru-content-list-v1"

    # A4 = 595.28 x 841.89 pt; per-axis conversion with the SOURCE size.
    block = normalized["pages"][0]["blocks"][0]
    x0, y0, x1, y1 = block["bbox"]
    assert abs(x0 - 142 / 1000 * 595.276) < 1.0
    assert abs(y0 - 108 / 1000 * 841.89) < 1.0
    assert abs(x1 - 605 / 1000 * 595.276) < 1.0
    assert abs(y1 - 122 / 1000 * 841.89) < 1.0
    # The converted box fits the real page; bounds are valid.
    assert geometry["bounds_valid"] is True
    assert block["bbox_verified"] is True


def test_cached_pages_payload_without_basis_stays_unverified() -> None:
    """F07 semantics carried into G09: an undeclared 'pages' payload has no
    justified transform — its boxes stay raw, bbox_verified=False, and the
    manifest reports the basis as unverified."""

    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    payload = {
        "pages": [
            {
                "page": 1,
                "width": 595.0,
                "height": 842.0,
                "texts": [
                    {"self_ref": "#/texts/0", "label": "text", "text": "Hello",
                     "bbox": {"l": 10.0, "t": 20.0, "r": 100.0, "b": 30.0}}
                ],
            }
        ]
    }
    projected = _project_mineru_payload(
        payload, page_numbers={1}, page_order=[1], page_sizes={1: (595.0, 842.0, 0)}
    )
    projected.pop("_projection_warnings", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    block = document.pages[0].blocks[0]
    assert block.bbox_verified is False, "an undeclared frame is explicitly untrusted"
    descriptor = _page_geometry_descriptor(
        document, page_sizes={1: (595.0, 842.0, 0)}, declared_basis=None
    )
    assert descriptor["coordinate_basis_verified"] is False
    assert descriptor["basis_notes"]


def test_declared_pt_basis_verifies_coordinate_basis() -> None:
    """A payload that DECLARES unit=pt + origin provides the justified
    transform — only then may the basis be verified."""

    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    payload = {
        "content_list": [{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [10, 20, 100, 30]}],
        "coord_basis": {"unit": "pt", "origin": "top_left"},
    }
    projected = _project_mineru_payload(
        payload,
        page_numbers={1},
        page_order=[1],
        page_sizes={1: (595.0, 842.0, 0)},
        declared_basis={"unit": "pt", "origin": "top_left"},
    )
    from bio_pdf.errors import WorkflowError
    from bio_pdf.parsers.mineru_adapter import EngineProtocolUnsupportedError  # noqa: F401

    document = normalize_docling_document(
        {k: v for k, v in projected.items() if k != "_projection_warnings"},
        parser_run_id="run", document_id="doc",
    )
    descriptor = _page_geometry_descriptor(
        document, page_sizes={1: (595.0, 842.0, 0)}, declared_basis=payload["coord_basis"]
    )
    assert descriptor["coordinate_basis_verified"] is True
    assert descriptor["basis_notes"] == []


def test_px_and_normalized_units_are_not_silently_converted() -> None:
    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    for basis in (
        {"unit": "px", "origin": "top_left", "dpi": 144},
        {"unit": "normalized", "origin": "top_left", "scale": 1000},
    ):
        projected = _project_mineru_payload(
            {"content_list": [{"type": "text", "text": "x", "page_idx": 0, "bbox": [0, 0, 100, 100]}]},
            page_numbers={1},
            page_order=[1],
            page_sizes={1: (595.0, 842.0, 0)},
            declared_basis=basis,
        )
        document = normalize_docling_document(
            {k: v for k, v in projected.items() if k != "_projection_warnings"},
            parser_run_id="run", document_id="doc",
        )
        descriptor = _page_geometry_descriptor(
            document, page_sizes={1: (595.0, 842.0, 0)}, declared_basis=basis
        )
        assert descriptor["coordinate_basis_verified"] is False
        assert any("no conversion is invented" in note for note in descriptor["basis_notes"])


def test_rotation_blocks_basis_until_rotation_applied_is_declared(tmp_path: Path, monkeypatch) -> None:
    items = [{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [10, 20, 100, 30]}]
    _normalized, manifest = _mineru_parse(tmp_path, items, monkeypatch=monkeypatch, rotation=90)
    geometry = manifest["page_geometry"]
    assert 1 in geometry["rotated_pages"]
    assert geometry["coordinate_basis_verified"] is False

    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    basis = {"unit": "pt", "origin": "top_left", "rotation_applied": True}
    projected = _project_mineru_payload(
        {"content_list": items}, page_numbers={1}, page_order=[1],
        page_sizes={1: (595.0, 842.0, 90)}, declared_basis=basis,
    )
    document = normalize_docling_document(
        {k: v for k, v in projected.items() if k != "_projection_warnings"},
        parser_run_id="run", document_id="doc",
    )
    descriptor = _page_geometry_descriptor(
        document, page_sizes={1: (595.0, 842.0, 90)}, declared_basis=basis
    )
    assert descriptor["coordinate_basis_verified"] is True


def test_non_finite_bbox_is_dropped_loudly(tmp_path: Path) -> None:
    import math

    normalized, manifest = _mineru_parse(
        tmp_path,
        [{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [0, 0, float("nan"), 100]}],
    )
    geometry = manifest["page_geometry"]
    assert any("non-finite" in w for w in geometry["bbox_warnings"])
    assert geometry["bounds_valid"] is False
    assert any("non-finite" in e for e in normalized["errors"])
    # The box is dropped from the normalized view; the raw payload keeps it.
    block = normalized["pages"][0]["blocks"][0]
    assert block["bbox"] is None


def test_inverted_box_fails_bounds_but_basis_facts_stay_separate() -> None:
    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    basis = {"unit": "pt", "origin": "top_left"}
    projected = _project_mineru_payload(
        {"content_list": [{"type": "text", "text": "x", "page_idx": 0, "bbox": [100, 100, 20, 20]}]},
        page_numbers={1}, page_order=[1], page_sizes={1: (595.0, 842.0, 0)}, declared_basis=basis,
    )
    document = normalize_docling_document(
        {k: v for k, v in projected.items() if k != "_projection_warnings"},
        parser_run_id="run", document_id="doc",
    )
    descriptor = _page_geometry_descriptor(
        document, page_sizes={1: (595.0, 842.0, 0)}, declared_basis=basis
    )
    # The basis declaration IS justified (pt+origin, unrotated, sized)…
    assert descriptor["coordinate_basis_verified"] is True
    # …while the BOX itself is invalid — separate facts, not conflated.
    assert descriptor["bounds_valid"] is False
    assert any("inverted" in w for w in descriptor["bbox_warnings"])


# ---- G08/P03: SI unit case is semantic ------------------------------------


def test_si_case_and_dimension_are_not_flattened() -> None:
    """G08 recheck repro: 5 µM vs 5 µm must NOT pair as equal; the micro
    GLYPH forms are equivalent; case and dimension survive normalization."""

    from bio_pdf.application.comparison_service import pair_measurements

    # Glyph equivalence: micro sign vs Greek mu.
    out = pair_measurements([_mic("5", "1h", unit="µM")], [_mic("5", "1h", unit="\u03bcM")])
    assert out["paired"] == 1 and out["value_diffs"] == []

    # SI case / dimension: µM vs µm is concentration vs length.
    out = pair_measurements([_mic("5", "1h", unit="µM")], [_mic("5", "1h", unit="µm")])
    assert len(out["value_diffs"]) == 1
    assert "unit" in out["value_diffs"][0]["kind"]

    # M vs mM is a magnitude difference — reported, never unified.
    out = pair_measurements([_mic("5", "1h", unit="M")], [_mic("5", "1h", unit="mM")])
    assert len(out["value_diffs"]) == 1 and "unit" in out["value_diffs"][0]["kind"]

    # 1 mM vs 1000 µM: no magnitude conversion is implemented, so this is an
    # explicit unit difference (with the normalization note saying so) —
    # never a silent "equal".
    out = pair_measurements([_mic("1", "1h", unit="mM")], [_mic("1000", "1h", unit="µM")])
    assert len(out["value_diffs"]) >= 1
    assert "no magnitude conversion" in out["unit_normalization_note"]


def test_mcg_alias_is_explicitly_equivalent() -> None:
    from bio_pdf.application.comparison_service import pair_measurements

    out = pair_measurements([_mic("1", "1h", unit="mcg/mL")], [_mic("1", "1h", unit="ug/mL")])
    assert out["value_diffs"] == []

# ---- H03/S03: allocation/upload split — a lost PUT response recovers -----


def test_lost_put_response_reuses_the_same_batch(tmp_path: Path) -> None:
    """The reviewer's repro: POST returned the batch/slot, the PUT response
    was lost (server may have received the bytes). The retry must re-PUT the
    SAME slot — allocation (POST) count stays 1."""

    import io
    import zipfile

    from bio_pdf.errors import NetworkTransientError
    from bio_pdf.parsers.mineru_adapter import MinerUAdapter

    posts = {"file_urls": 0, "puts": 0, "zip_downloads": 0}
    zip_bytes = _a4_zip_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            posts["file_urls"] += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "batch_id": "bh03",
                        "file_urls": ["https://oss.example/put/h03"],
                    }
                },
            )
        if request.method == "PUT":
            posts["puts"] += 1
            if posts["puts"] == 1:
                raise httpx.ReadTimeout("lost PUT response")
            return httpx.Response(200)
        if "/extract-results/batch/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {"file_name": "source.pdf", "state": "done",
                             "full_zip_url": "https://oss.example/zip/h03"}
                        ]
                    }
                },
            )
        if url == "https://oss.example/zip/h03":
            posts["zip_downloads"] += 1
            return httpx.Response(200, content=zip_bytes)
        raise AssertionError(url)

    adapter = MinerUAdapter(token="t", transport=httpx.MockTransport(handler), poll_interval=0.0)

    first = _job_request(tmp_path, "attempt-1")
    try:
        adapter.parse(first, local_file=first.source_path)
    except NetworkTransientError:
        pass  # the lost PUT response surfaces as a transient failure
    else:
        raise AssertionError("the first attempt must report the lost PUT response")

    # The identity was persisted BEFORE the PUT: batch known, phase allocated.
    identity_path = tmp_path / "jobs" / "job-x" / "mineru_remote_identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity["batch_id"] == "bh03"
    assert identity["phase"] == "allocated"
    assert posts["file_urls"] == 1 and posts["puts"] == 1

    # The retry (new attempt dir) continues on the SAME batch/slot.
    second = _job_request(tmp_path, "attempt-2")
    try:
        adapter.parse(second, local_file=second.source_path)
    except Exception:  # noqa: BLE001 — zip download may still use this helper
        raise
    assert posts["file_urls"] == 1, "a retry must NOT allocate a second batch"
    assert posts["puts"] == 2, "the retry re-PUTs the same slot"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity["phase"] == "completed"


def test_process_exit_after_upload_skips_the_put(tmp_path: Path) -> None:
    """A crash AFTER the upload persists phase='uploaded' — the retry goes
    straight to polling (no second PUT, no second POST)."""

    from bio_pdf.parsers.mineru_adapter import MinerUAdapter

    posts = {"file_urls": 0, "puts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            posts["file_urls"] += 1
            return httpx.Response(
                200,
                json={"data": {"batch_id": "bh04", "file_urls": ["https://oss.example/put/h04"]}},
            )
        if request.method == "PUT":
            posts["puts"] += 1
            return httpx.Response(200)
        if "/extract-results/batch/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {"file_name": "source.pdf", "state": "running"}
                        ]
                    }
                },
            )
        raise AssertionError(url)

    adapter = MinerUAdapter(token="t", transport=httpx.MockTransport(handler), poll_interval=0.0)
    first = _job_request(tmp_path, "attempt-1")
    from bio_pdf.errors import EngineTimeoutError

    # Drain polls until the adapter gives up on the "running" state.
    adapter.max_polls = 2
    try:
        adapter.parse(first, local_file=first.source_path)
    except Exception:  # noqa: BLE001 — running-forever surfaces as a timeout
        pass
    assert posts["file_urls"] == 1 and posts["puts"] == 1
    identity_path = tmp_path / "jobs" / "job-x" / "mineru_remote_identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity["batch_id"] == "bh04"

    second = _job_request(tmp_path, "attempt-2")
    try:
        adapter.parse(second, local_file=second.source_path)
    except Exception:  # noqa: BLE001
        pass
    assert posts["file_urls"] == 1, "no re-allocation"
    assert posts["puts"] == 1, "the uploaded phase skips the PUT entirely"
