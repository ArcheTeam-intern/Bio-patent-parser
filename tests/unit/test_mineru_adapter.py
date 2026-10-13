"""Offline tests for the MinerU V1 SaaS adapter."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bio_pdf.errors import (
    EngineProtocolUnsupportedError,
    EngineTimeoutError,
    EngineUnavailableError,
)
from bio_pdf.parsers.base import ParseRequest
from bio_pdf.parsers.mineru_adapter import MinerUAdapter


def _build_request(tmp_path: Path) -> ParseRequest:
    return ParseRequest(
        job_id="job-1",
        document_id="sha256:doc",
        source_path=tmp_path / "input.pdf",
        page_numbers=(1, 2, 3),
        profile="layout_native",
        options_ref="options.json",
        output_dir=tmp_path / "artifacts",
    )


def _docling_shape_payload() -> dict:
    return {
        "pages": [
            {
                "page": 1,
                "size": {"width": 612.0, "height": 792.0},
                "width": 612.0,
                "height": 792.0,
                "texts": [
                    {
                        "self_ref": "p1/t1",
                        "label": "text",
                        "bbox": {"l": 50.0, "t": 60.0, "r": 200.0, "b": 80.0},
                        "text": "SEQ ID NO: 1",
                    },
                ],
            }
        ]
    }


def _handler(
    *,
    poll_sequence: list[str] | None = None,
    submit_task_id: str = "task-abc",
    result_payload: object | None = None,
    submit_status: int = 200,
    poll_status: int = 200,
    result_status: int = 200,
) -> callable:
    poll_sequence = list(poll_sequence or ["done"])
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # MinerU base path includes the /api/v4 prefix; strip it for matching.
        short = path.split("/api/v4", 1)[-1] or "/"
        if short == "/extract/task" and request.method == "POST":
            if submit_status != 200:
                return httpx.Response(submit_status, json={"code": submit_status, "msg": "err"})
            return httpx.Response(200, json={"code": 0, "data": {"task_id": submit_task_id}})
        if short.startswith("/extract/task/") and request.method == "GET":
            if poll_status != 200:
                return httpx.Response(poll_status, json={"code": poll_status, "msg": "err"})
            idx = min(calls["poll"], len(poll_sequence) - 1)
            calls["poll"] += 1
            state = poll_sequence[idx]
            data: dict = {"state": state, "task_id": submit_task_id}
            if state in {"done", "success", "completed", "succeeded"}:
                data["full_md_link"] = f"{request.url.scheme}://{request.url.netloc}/result.json"
            return httpx.Response(200, json={"code": 0, "data": data})
        if path.endswith("/result.json"):
            if result_status != 200:
                return httpx.Response(result_status, json={"code": result_status})
            if result_payload is None:
                return httpx.Response(200, json=_docling_shape_payload())
            if isinstance(result_payload, bytes):
                return httpx.Response(200, content=result_payload)
            return httpx.Response(200, json=result_payload)
        return httpx.Response(404)

    return handler


def _adapter(handler: callable, **kwargs) -> MinerUAdapter:
    return MinerUAdapter(
        token="test-token",
        poll_interval=0.0,
        max_polls=5,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_requires_token() -> None:
    with pytest.raises(EngineUnavailableError):
        MinerUAdapter(token="")


def test_full_lifecycle_produces_artifacts(tmp_path: Path) -> None:
    adapter = _adapter(_handler())
    artifacts = adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")
    assert artifacts.normalized_path.exists()
    assert artifacts.raw_response_path.exists()
    assert artifacts.manifest_path.exists()

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert manifest["engine"] == "mineru"
    assert manifest["remote_task_id"] == "task-abc"
    assert manifest["model_version"] == "vlm"

    normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
    assert len(normalized["pages"]) == 1


def test_bearer_token_in_request(tmp_path: Path) -> None:
    seen: list[str | None] = []
    inner = _handler()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return inner(request)

    adapter = _adapter(handler)
    adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")
    assert all(h is not None and h.startswith("Bearer ") for h in seen)


def test_rejects_non_http_source_url(tmp_path: Path) -> None:
    adapter = _adapter(_handler())
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_build_request(tmp_path), source_url="file:///etc/passwd")
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_build_request(tmp_path), source_url="")


def test_submit_401_raises_unavailable(tmp_path: Path) -> None:
    adapter = _adapter(_handler(submit_status=401))
    with pytest.raises(EngineUnavailableError):
        adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")


def test_submit_response_without_task_id_raises_protocol(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        short = request.url.path.split("/api/v4", 1)[-1] or "/"
        if short == "/extract/task":
            return httpx.Response(200, json={"code": 0, "data": {"unexpected": "shape"}})
        return _handler()(request)

    adapter = _adapter(handler)
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")


def test_poll_timeout_does_not_cancel_remote_job(tmp_path: Path) -> None:
    adapter = _adapter(_handler(poll_sequence=["running", "running", "running"]))
    with pytest.raises(EngineTimeoutError):
        adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")


def test_poll_404_raises_protocol(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        short = request.url.path.split("/api/v4", 1)[-1] or "/"
        if short.startswith("/extract/task/") and request.method == "GET":
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        return _handler()(request)

    adapter = _adapter(handler)
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")


def test_failed_state_raises_unavailable(tmp_path: Path) -> None:
    adapter = _adapter(_handler(poll_sequence=["failed"]))
    with pytest.raises(EngineUnavailableError):
        adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")


def test_result_fallback_to_markdown(tmp_path: Path) -> None:
    adapter = _adapter(_handler(result_payload={"markdown": "# heading\n\nbody"}))
    artifacts = adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")
    normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
    assert len(normalized["pages"]) == 1
    assert any("# heading" in b["text"] for b in normalized["pages"][0]["texts"])


def test_result_content_list_is_projected(tmp_path: Path) -> None:
    payload = {
        "content_list": [
            {
                "page_idx": 0,
                "type": "text",
                "text": "MKTG",
                "bbox": [10.0, 20.0, 100.0, 30.0],
            },
            {
                "page_idx": 1,
                "type": "text",
                "text": "AAAA",
            },
        ]
    }
    adapter = _adapter(_handler(result_payload=payload))
    artifacts = adapter.parse(_build_request(tmp_path), source_url="https://example.com/x.pdf")
    normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
    assert len(normalized["pages"]) == 2
    assert any("MKTG" in b["text"] for b in normalized["pages"][0]["texts"])
    assert any("AAAA" in b["text"] for b in normalized["pages"][1]["texts"])