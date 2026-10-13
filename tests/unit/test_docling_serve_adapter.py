"""Offline tests for the docling-serve HTTP adapter.

These tests use :class:`httpx.MockTransport` so no network or live
``docling-serve`` instance is required. They verify the client lifecycle
(upload → poll → download), the API-key header, timeout/failure handling and
the result-shape guard — everything we can prove without a server.
"""

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
from bio_pdf.parsers.docling_serve_adapter import DoclingServeAdapter


def _docling_payload() -> dict:
    return {
        "pages": [
            {
                "page": 1,
                "width": 612.0,
                "height": 792.0,
                "size": {"width": 612.0, "height": 792.0},
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


def _request(tmp_path: Path) -> ParseRequest:
    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    return ParseRequest(
        job_id="job-1",
        document_id="sha256:doc",
        source_path=source,
        page_numbers=(1,),
        profile="layout_native",
        options_ref="options.json",
        output_dir=tmp_path / "artifacts",
    )


def _handler(
    *,
    poll_sequence: list[str] | None = None,
    result_payload: object | None = None,
    submit_task_id: str = "task-123",
) -> callable:
    """Build an httpx handler simulating a docling-serve lifecycle.

    ``poll_sequence`` is the ordered list of statuses returned by each poll
    call. ``result_payload`` is the JSON returned by ``/v1/result/{id}``.
    """

    poll_sequence = list(poll_sequence or ["running", "completed"])
    calls: dict[str, int] = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/convert/file/async":
            return httpx.Response(200, json={"task_id": submit_task_id})
        if path.startswith("/v1/status/poll/"):
            idx = min(calls["poll"], len(poll_sequence) - 1)
            calls["poll"] += 1
            status = poll_sequence[idx]
            return httpx.Response(200, json={"status": status})
        if path.startswith("/v1/result/"):
            if result_payload is None:
                return httpx.Response(200, json=_docling_payload())
            if isinstance(result_payload, bytes):
                return httpx.Response(200, content=result_payload)
            return httpx.Response(200, json=result_payload)
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    return handler


def _adapter(
    handler: callable, *, api_key: str | None = None, **kwargs
) -> DoclingServeAdapter:
    return DoclingServeAdapter(
        base_url="http://test",
        api_key=api_key,
        poll_interval=0.0,
        max_polls=5,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_full_lifecycle_produces_artifacts(tmp_path: Path) -> None:
    adapter = _adapter(_handler())
    artifacts = adapter.parse(_request(tmp_path))

    assert artifacts.normalized_path.exists()
    assert artifacts.raw_response_path.exists()
    assert artifacts.manifest_path.exists()

    normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
    assert normalized["document_id"] == "sha256:doc"
    assert len(normalized["pages"]) == 1

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert manifest["engine"] == "docling_serve"
    assert manifest["remote_task_id"] == "task-123"


def test_api_key_is_sent_as_header(tmp_path: Path) -> None:
    seen_keys: list[str | None] = []
    inner = _handler()

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx.Headers is case-insensitive; capture the resolved value.
        seen_keys.append(request.headers.get("X-Api-Key"))
        return inner(request)

    adapter = _adapter(handler, api_key="secret-key")
    adapter.parse(_request(tmp_path))
    assert "secret-key" in seen_keys


def test_no_api_key_means_no_header(tmp_path: Path) -> None:
    seen_keys: list[str | None] = []
    inner = _handler()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers.get("X-Api-Key"))
        return inner(request)

    adapter = _adapter(handler)
    adapter.parse(_request(tmp_path))
    assert all(v is None for v in seen_keys)


def test_poll_timeout_raises_engine_timeout(tmp_path: Path) -> None:
    adapter = _adapter(_handler(poll_sequence=["running", "running", "running"]))
    with pytest.raises(EngineTimeoutError):
        adapter.parse(_request(tmp_path))


def test_failed_status_returns_without_downloading_result(tmp_path: Path) -> None:
    result_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/result/"):
            result_requests.append(request.url.path)
        return _handler(poll_sequence=["failed"])(request)

    adapter = _adapter(handler)
    # A failed task must surface an engine error before any result download.
    with pytest.raises(EngineUnavailableError):
        adapter.parse(_request(tmp_path))
    assert result_requests == []


def test_task_404_raises_protocol_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/status/poll/"):
            return httpx.Response(404, json={"detail": "not found"})
        return _handler()(request)

    adapter = _adapter(handler)
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_request(tmp_path))


def test_result_non_json_raises_protocol_error(tmp_path: Path) -> None:
    adapter = _adapter(_handler(result_payload=b"not json at all"))
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_request(tmp_path))


def test_result_wrapped_in_document_key_is_accepted(tmp_path: Path) -> None:
    wrapped = {"document": _docling_payload()}
    adapter = _adapter(_handler(result_payload=wrapped))
    artifacts = adapter.parse(_request(tmp_path))
    normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
    assert len(normalized["pages"]) == 1


def test_result_unrecognised_shape_raises_protocol_error(tmp_path: Path) -> None:
    adapter = _adapter(_handler(result_payload={"foo": "bar"}))
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_request(tmp_path))


def test_health_reachable() -> None:
    adapter = _adapter(_handler())
    result = adapter.health()
    assert result["status"] == "ok"


def test_health_unreachable_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _adapter(handler)
    with pytest.raises(EngineUnavailableError):
        adapter.health()


def test_submit_rejects_when_no_task_id(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/convert/file/async":
            return httpx.Response(200, json={"unexpected": "shape"})
        return _handler()(request)

    adapter = _adapter(handler)
    with pytest.raises(EngineProtocolUnsupportedError):
        adapter.parse(_request(tmp_path))