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
