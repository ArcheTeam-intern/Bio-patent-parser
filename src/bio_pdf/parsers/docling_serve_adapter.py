"""HTTP adapter for a self-hosted ``docling-serve`` instance.

Docling itself is a local library; this adapter talks to it through the
``docling-serve`` REST API so the business machine does not need the SDK or
models installed. It mirrors the MinerU adapter's lifecycle (upload → submit
job → poll → download) so both engines share one integration contract.

Reference (docling-serve):

- ``POST /v1/convert/file``           — sync multipart upload
- ``POST /v1/convert/file/async``     — submit an async task, returns task_id
- ``GET  /v1/status/poll/{task_id}``  — poll status
- ``GET  /v1/result/{task_id}``       — fetch finished result
- ``GET  /health``                    — liveness probe

The adapter only depends on ``httpx``. It never imports the ``docling`` SDK,
so it works in a minimal environment.

.. warning::
    The exact result envelope of ``/v1/result/{task_id}`` (whether the JSON
    is returned bare or wrapped in a ``{"document": ...}`` object) is
    version-dependent. This adapter accepts the two common shapes and raises
    :class:`EngineProtocolUnsupportedError` otherwise — it never guesses past
    a verifiable structure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from bio_pdf.contracts import NormalizedDocument
from bio_pdf.errors import (
    EngineProtocolUnsupportedError,
    EngineTimeoutError,
    EngineUnavailableError,
    NetworkTransientError,
)
from bio_pdf.parsers.base import ParseArtifacts, ParseRequest
from bio_pdf.parsers.normalize_docling import normalize_docling_document

#: Default base URL when running ``docling-serve`` on the same host.
DEFAULT_BASE_URL = "http://localhost:5001"

#: Poll statuses that mean the job has finished successfully.
_COMPLETED_STATES = {"completed", "success", "succeeded", "done"}
#: Poll statuses that mean the job failed on the server side.
_FAILED_STATES = {"failed", "error", "cancelled", "canceled"}


class DoclingServeAdapter:
    """Parser adapter that delegates to a ``docling-serve`` HTTP service."""

    name: str = "docling_serve"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        max_polls: int = 300,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        # ``transport`` is injectable for tests (httpx.MockTransport).
        self._transport = transport

    # ---- plumbing -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=self.timeout,
            transport=self._transport,
        )

    # ---- public interface ------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return the service's ``/health`` payload or raise on failure."""

        with self._client() as client:
            try:
                response = client.get("/health")
            except httpx.HTTPError as exc:
                raise EngineUnavailableError(
                    f"docling-serve is unreachable at {self.base_url}: {exc}",
                    details={"base_url": self.base_url},
                ) from exc
        if response.status_code >= 500:
            raise EngineUnavailableError(
                f"docling-serve health check returned {response.status_code}",
                details={"base_url": self.base_url},
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise EngineProtocolUnsupportedError(
                "docling-serve /health did not return JSON",
                details={"body": response.text[:200]},
            ) from exc

    def parse(self, request: ParseRequest) -> ParseArtifacts:
        request.output_dir.mkdir(parents=True, exist_ok=True)

        raw_response_path = request.output_dir / "raw_response.json"
        normalized_path = request.output_dir / "normalized.json"
        manifest_path = request.output_dir / "manifest.json"

        with self._client() as client:
            task_id = self._submit(client, request)
            status = self._poll(client, task_id)
            result_payload = self._download(client, task_id)

        # Persist the raw server response verbatim (no reshaping) so the
        # response can be re-audited later.
        raw_response_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "final_status": status,
                    "result": result_payload,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        document = normalize_docling_document(
            result_payload,
            parser_run_id=f"{request.job_id}:docling_serve",
            document_id=request.document_id,
        )

        # docling-serve does not take a page-range parameter on this endpoint;
        # filter locally so the output honours the requested source pages and
        # keeps their original numbers.
        requested = set(request.page_numbers)
        produced = {p.page_number for p in document.pages}
        if requested:
            unexpected = sorted(produced - requested)
            if unexpected:
                document = document.model_copy(
                    update={
                        "pages": [p for p in document.pages if p.page_number in requested],
                        "errors": list(document.errors)
                        + [f"docling_serve returned pages outside the request: {unexpected}"],
                    }
                )

        normalized_path.write_text(document.model_dump_json(), encoding="utf-8")

        manifest = self._manifest(
            request=request,
            document=document,
            task_id=task_id,
            raw_path=raw_response_path,
            normalised_path=normalized_path,
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        return ParseArtifacts(
            normalized_path=normalized_path,
            raw_response_path=raw_response_path,
            manifest_path=manifest_path,
        )

    # ---- lifecycle steps -------------------------------------------------

    def _submit(self, client: httpx.Client, request: ParseRequest) -> str:
        """Upload the PDF and submit a conversion task; return the task id."""

        do_ocr = request.profile == "layout_ocr"
        form = {
            "from_formats": "pdf",
            "to_formats": "json",
            "do_ocr": "true" if do_ocr else "false",
        }
        files = {"files": (request.source_path.name, request.source_path.read_bytes(), "application/pdf")}
        try:
            response = client.post("/v1/convert/file/async", data=form, files=files)
        except httpx.HTTPError as exc:
            raise NetworkTransientError(
                f"failed to submit docling-serve task: {exc}",
                details={"base_url": self.base_url},
            ) from exc

        if response.status_code in (401, 403):
            raise EngineUnavailableError(
                "docling-serve rejected the API key",
                details={"status": response.status_code},
            )
        if response.status_code >= 400:
            raise EngineUnavailableError(
                f"docling-serve submit returned {response.status_code}: {response.text[:200]}",
                details={"status": response.status_code},
            )

        payload = response.json()
        task_id = payload.get("task_id") or payload.get("taskId") or payload.get("id")
        if not task_id:
            raise EngineProtocolUnsupportedError(
                "docling-serve submit response has no task id",
                details={"body": str(payload)[:200]},
            )
        return str(task_id)

    def _poll(self, client: httpx.Client, task_id: str) -> str:
        """Poll the task status until it completes, fails, or times out.

        Returns the final status string. A local timeout only ends *local*
        waiting — it never concludes the remote task failed (the caller may
        resume with the same ``task_id``).
        """

        for _ in range(self.max_polls):
            try:
                response = client.get(f"/v1/status/poll/{task_id}")
            except httpx.HTTPError as exc:
                raise NetworkTransientError(
                    f"polling failed for task {task_id}: {exc}",
                    details={"task_id": task_id},
                ) from exc

            if response.status_code == 404:
                # Task vanished server-side (service restarted without a
                # persistent index) — surface it as a protocol error so the
                # runner can decide to rebuild an attempt.
                raise EngineProtocolUnsupportedError(
                    f"docling-serve task {task_id} no longer exists",
                    details={"task_id": task_id},
                )

            payload = response.json()
            status = str(
                payload.get("status")
                or payload.get("state")
                or payload.get("task_status")
                or "unknown"
            ).lower()

            if status in _FAILED_STATES:
                raise EngineUnavailableError(
                    f"docling-serve task {task_id} failed with status {status!r}",
                    details={"task_id": task_id, "status": status},
                )
            if status in _COMPLETED_STATES:
                return status

            import time

            time.sleep(self.poll_interval)

        raise EngineTimeoutError(
            f"timed out polling docling-serve task {task_id} after {self.max_polls} attempts",
            details={"task_id": task_id, "max_polls": self.max_polls},
        )

    def _download(self, client: httpx.Client, task_id: str) -> dict[str, Any]:
        """Download the finished result and normalise it to a document dict."""

        try:
            response = client.get(f"/v1/result/{task_id}")
        except httpx.HTTPError as exc:
            raise NetworkTransientError(
                f"failed to download docling-serve result: {exc}",
                details={"task_id": task_id},
            ) from exc

        if response.status_code != 200:
            raise EngineUnavailableError(
                f"docling-serve result download returned {response.status_code}",
                details={"task_id": task_id},
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise EngineProtocolUnsupportedError(
                "docling-serve result was not JSON (to_formats=json)",
                details={"task_id": task_id, "body": response.text[:200]},
            ) from exc

        return self._extract_document_payload(payload, task_id)

    def _extract_document_payload(
        self, payload: Any, task_id: str
    ) -> dict[str, Any]:
        """Unwrap the docling-serve result envelope to the Docling document.

        The result endpoint can nest several layers:

        - ``{"document": {...}, "status": ..., ...}`` (sync response);
        - ``ConvertDocumentResponse`` with the Docling JSON under
          ``json_content`` (dict or a JSON string);
        - a bare Docling document (has ``pages``).

        We unwrap repeatedly until we reach a dict with ``pages``, or give up
        with :class:`EngineProtocolUnsupportedError`.
        """

        for _ in range(6):
            if not isinstance(payload, dict):
                break
            if "pages" in payload:
                return payload
            if "json_content" in payload:
                jc = payload["json_content"]
                if isinstance(jc, str):
                    import json as _json

                    try:
                        payload = _json.loads(jc)
                    except _json.JSONDecodeError as exc:
                        raise EngineProtocolUnsupportedError(
                            "docling-serve json_content was not valid JSON",
                            details={"task_id": task_id},
                        ) from exc
                else:
                    payload = jc
                continue
            if isinstance(payload.get("document"), dict):
                payload = payload["document"]
                continue
            break

        raise EngineProtocolUnsupportedError(
            "docling-serve result has an unrecognised shape",
            details={"task_id": task_id, "keys": list(payload) if isinstance(payload, dict) else type(payload).__name__},
        )

    # ---- manifest --------------------------------------------------------

    def _manifest(
        self,
        *,
        request: ParseRequest,
        document: NormalizedDocument,
        task_id: str,
        raw_path: Path,
        normalised_path: Path,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "engine": self.name,
            "parser_run_id": document.parser_run_id,
            "document_id": document.document_id,
            "profile": request.profile,
            "pages": list(request.page_numbers),
            "options_ref": request.options_ref,
            "remote_task_id": task_id,
            "base_url": self.base_url,
            "capabilities": {
                "tables": True,
                "ocr": request.profile == "layout_ocr",
                "structure": True,
            },
            "artifacts": {
                "normalized": str(normalised_path.relative_to(request.output_dir)),
                "raw_response": str(raw_path.relative_to(request.output_dir)),
            },
            "page_coverage": {
                "requested": list(request.page_numbers),
                "produced": [p.page_number for p in document.pages],
            },
            "errors": list(document.errors),
        }


__all__ = ["DoclingServeAdapter", "DEFAULT_BASE_URL"]