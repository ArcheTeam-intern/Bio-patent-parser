"""HTTP adapter for the MinerU V1 SaaS API.

Endpoint contract (from the MinerU API docs at
https://mineru.net/apiManage/docs):

- ``POST https://mineru.net/api/v4/extract/task`` — submit a parse job.
  Body: ``{"url": "<public pdf url>", "model_version": "vlm"}``
  Header: ``Authorization: Bearer <token>``.
  Response: ``{"code": 0, "data": {"task_id": "..."}}``.

- ``GET  https://mineru.net/api/v4/extract/task/<task_id>`` — poll status.
  Response: ``{"code": 0, "data": {"task_id": "...", "state": "...",
  "full_md_link": "...", "zip_result_url": "..."}}``.

- ``GET  <full_md_link>`` — fetch the markdown result.

The adapter depends only on ``httpx`` (no SDK), so it runs in a minimal
business machine.

The MinerU endpoint accepts only **publicly accessible URLs** — local
PDFs must first be uploaded to a reachable bucket / tunnel.
"""

from __future__ import annotations

import json
import re
import time
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

#: Default MinerU base URL. Override via ``MinerUAdapter(base_url=...)``.
DEFAULT_BASE_URL = "https://mineru.net/api/v4"

#: Model version we submit by default. MinerU exposes ``vlm`` and
#: ``pipeline``; we use the vlm tier (strongest layout fidelity).
DEFAULT_MODEL_VERSION = "vlm"

#: Terminal states the polling loop will accept.
_COMPLETED_STATES = {"done", "success", "completed", "succeeded"}
_FAILED_STATES = {"failed", "error", "cancelled", "canceled"}
#: How many seconds we wait between polls when the response is not terminal.
DEFAULT_POLL_INTERVAL = 3.0
#: Hard cap on the number of polls before we raise a local timeout. The
#: remote job is *not* cancelled by this; the runner can resume with the
#: same ``task_id``.
DEFAULT_MAX_POLLS = 600  # ~30 min at the default interval


class MinerUAdapter:
    """Parser adapter for the MinerU V1 SaaS HTTP API."""

    name: str = "mineru"

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        model_version: str = DEFAULT_MODEL_VERSION,
        timeout: float = 600.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_polls: int = DEFAULT_MAX_POLLS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not token:
            raise EngineUnavailableError(
                "MinerUAdapter requires a Bearer token (set MINERU_TOKEN or pass token=...)",
                details={"hint": "mineru.net → 个人中心 → API 管理 → 创建 token"},
            )
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self._transport = transport

    # ---- plumbing -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=self.timeout,
            transport=self._transport,
        )

    # ---- public interface ------------------------------------------------

    def upload_local_file(
        self,
        file_path: Path,
        *,
        enable_formula: bool = True,
        enable_table: bool = True,
        language: str = "en",
        is_ocr: bool = True,
    ) -> str:
        """Upload a local PDF to MinerU's file-urls endpoint and return a public URL.

        The MinerU SaaS exposes ``POST /api/v4/file-urls/batch`` that returns
        pre-signed upload URLs; we PUT the file bytes to the first one, then
        read the public ``url`` field for the subsequent parse job.
        """

        if not file_path.exists():
            raise EngineUnavailableError(
                f"file not found: {file_path}",
                details={"path": str(file_path)},
            )

        body = {
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "language": language,
            "is_ocr": is_ocr,
            "model_version": self.model_version,
            "files": [
                {
                    "name": file_path.name,
                    "is_ocr": is_ocr,
                    "data_id": file_path.stem,
                }
            ],
        }

        with self._client() as client:
            try:
                response = client.post("/file-urls/batch", json=body)
            except httpx.HTTPError as exc:
                raise NetworkTransientError(
                    f"failed to request MinerU upload URL: {exc}",
                    details={"base_url": self.base_url},
                ) from exc

            if response.status_code in (401, 403):
                raise EngineUnavailableError(
                    "MinerU rejected the Bearer token on upload request",
                    details={"status": response.status_code, "body": response.text[:200]},
                )
            if response.status_code >= 400:
                raise EngineProtocolUnsupportedError(
                    f"MinerU file-urls returned {response.status_code}: {response.text[:300]}",
                    details={"status": response.status_code},
                )

            payload = response.json()
            if not isinstance(payload, dict) or "data" not in payload:
                raise EngineProtocolUnsupportedError(
                    "MinerU file-urls response has no 'data' field",
                    details={"body": str(payload)[:300]},
                )
            data_block = payload["data"]
            # Real shape: {"data": {"batch_id": "...", "file_urls": ["<put-url>"]}}
            # After PUT, the same URL becomes the public read URL for the
            # parse endpoint (the SDK puts the same URL under ``data.result``
            # or ``data.files`` on some plans; we try the common variants).
            file_urls = data_block.get("file_urls")
            if not isinstance(file_urls, list) or not file_urls:
                batch = (
                    data_block.get("batch_upload_file")
                    or data_block.get("files")
                    or data_block.get("result")
                )
                if isinstance(batch, list) and batch:
                    file_urls = [
                        slot.get("url") if isinstance(slot, dict) else slot
                        for slot in batch
                    ]
            if not file_urls:
                raise EngineProtocolUnsupportedError(
                    "MinerU file-urls response has no upload slots",
                    details={"data_keys": list(data_block.keys())},
                )
            upload_url = str(file_urls[0])
            if not upload_url.startswith(("http://", "https://")):
                raise EngineProtocolUnsupportedError(
                    "MinerU file-urls slot is not a URL",
                    details={"slot": upload_url[:200]},
                )

            # PUT the file to the pre-signed URL. **Do not set
            # ``Content-Type`` here**: the OSS signature was computed
            # without it, and adding it triggers SignatureDoesNotMatch 403.
            # We also bypass ``client`` (which carries base headers) and
            # call ``httpx.put`` directly so the MinerU auth header is
            # not forwarded to the OSS bucket.
            try:
                put_resp = httpx.put(
                    upload_url,
                    content=file_path.read_bytes(),
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                raise NetworkTransientError(
                    f"failed to PUT file to MinerU: {exc}",
                    details={"upload_url": upload_url[:200]},
                ) from exc

            if put_resp.status_code not in (200, 201, 204):
                raise EngineUnavailableError(
                    f"MinerU file PUT returned {put_resp.status_code}",
                    details={"upload_url": upload_url[:200], "body": put_resp.text[:200]},
                )

            # The same pre-signed URL is the public read URL the parse
            # endpoint accepts. It is signed with an expiry (~24h typically).
            return upload_url

    def parse(
        self,
        request: ParseRequest,
        *,
        source_url: str | None = None,
        local_file: Path | None = None,
    ) -> ParseArtifacts:
        """Submit to MinerU, poll, then download + normalise.

        Either ``source_url`` (a public PDF URL) or ``local_file`` (a local
        PDF that will be uploaded via :meth:`upload_local_file`) must be
        provided.

        Source identity: when ``local_file`` is given, the *uploaded bytes*
        are the parsed source — the request's ``document_id`` must match the
        local file's SHA-256, otherwise the run mixes identities from two
        different PDFs and is rejected. When only ``source_url`` is given the
        remote bytes are parsed and the caller-provided ``document_id`` is
        trusted, but the manifest records that no local-byte verification was
        possible.

        Raises:
            EngineProtocolUnsupportedError: When the response shape does not
                match the documented contract, or when local_file's hash
                does not match request.document_id.
            EngineUnavailableError: When the API rejects the token / URL.
            EngineTimeoutError: When polling times out locally. The remote
                job is *not* cancelled; the runner may resume with the same
                ``task_id``.
        """

        if local_file is not None and source_url is None:
            source_url = self.upload_local_file(local_file)
        if not source_url or not source_url.lower().startswith(("http://", "https://")):
            raise EngineProtocolUnsupportedError(
                "MinerU V1 requires a public http(s) URL or a local_file to upload",
                details={"source_url": source_url, "local_file": str(local_file) if local_file else None},
            )

        # Identity check: the document_id must describe the bytes we actually
        # submitted. A mismatch means text, hashes and screenshots would be
        # attributed to the wrong PDF.
        source_identity_verified = False
        if local_file is not None:
            from bio_pdf.ingest.inspector import compute_sha256

            local_sha = compute_sha256(local_file)
            if request.document_id not in (f"sha256:{local_sha}", local_sha):
                raise EngineProtocolUnsupportedError(
                    "MinerU run mixes source identities: local_file hash does not match document_id",
                    details={
                        "document_id": request.document_id,
                        "local_file_sha256": local_sha,
                    },
                )
            source_identity_verified = True

        request.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = request.output_dir / "raw_response.json"
        normalized_path = request.output_dir / "normalized.json"
        manifest_path = request.output_dir / "manifest.json"

        with self._client() as client:
            task_id = self._submit(client, source_url)
            final_state, result_payload = self._poll_and_download(client, task_id)

        raw_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "final_state": final_state,
                    "result": result_payload,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        # The MinerU result is markdown + a per-page JSON sidecar. The
        # Docling-compatible normaliser accepts a JSON dict with ``pages``;
        # MinerU's payload is structurally different (it carries
        # ``full_md_link`` and ``content_list`` rather than ``pages``), so we
        # project it into a Docling-shaped document before normalising.
        projected = _project_mineru_payload(
            result_payload, page_numbers=set(request.page_numbers)
        )
        document = normalize_docling_document(
            projected,
            parser_run_id=f"{request.job_id}:mineru",
            document_id=request.document_id,
        )

        # MinerU ignores server-side page-range requests; keep the original
        # page numbers and filter locally so the output honours the request.
        requested = set(request.page_numbers)
        produced = {p.page_number for p in document.pages}
        if requested:
            unexpected = sorted(produced - requested)
            if unexpected:
                document = document.model_copy(
                    update={
                        "pages": [p for p in document.pages if p.page_number in requested],
                        "errors": list(document.errors)
                        + [f"mineru returned pages outside the request: {unexpected}"],
                    }
                )

        # Write the strict contract object, not the upstream projection.
        normalized_path.write_text(document.model_dump_json(), encoding="utf-8")

        manifest = self._manifest(
            request=request,
            document=document,
            task_id=task_id,
            raw_path=raw_path,
            normalized_path=normalized_path,
            source_identity_verified=source_identity_verified,
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        return ParseArtifacts(
            normalized_path=normalized_path,
            raw_response_path=raw_path,
            manifest_path=manifest_path,
        )

    # ---- lifecycle steps -------------------------------------------------

    def _submit(self, client: httpx.Client, source_url: str) -> str:
        """POST the public URL to /extract/task and return the task id."""

        body = {"url": source_url, "model_version": self.model_version}
        try:
            response = client.post("/extract/task", json=body)
        except httpx.HTTPError as exc:
            raise NetworkTransientError(
                f"failed to submit MinerU task: {exc}",
                details={"base_url": self.base_url},
            ) from exc

        if response.status_code in (401, 403):
            raise EngineUnavailableError(
                "MinerU rejected the Bearer token",
                details={"status": response.status_code, "body": response.text[:200]},
            )
        if response.status_code >= 400:
            raise EngineUnavailableError(
                f"MinerU submit returned {response.status_code}: {response.text[:200]}",
                details={"status": response.status_code},
            )

        payload = response.json()
        # MinerU envelope: {"code": 0, "msg": "...", "data": {"task_id": "..."}}
        if not isinstance(payload, dict) or "data" not in payload:
            raise EngineProtocolUnsupportedError(
                "MinerU submit response has no 'data' field",
                details={"body": str(payload)[:200]},
            )
        data = payload["data"]
        task_id = data.get("task_id") or data.get("taskId") or data.get("id")
        if not task_id:
            raise EngineProtocolUnsupportedError(
                "MinerU submit response has no task id",
                details={"body": str(payload)[:200]},
            )
        return str(task_id)

    def _poll_and_download(
        self,
        client: httpx.Client,
        task_id: str,
    ) -> tuple[str, dict[str, Any]]:
        """Poll the task until it terminates; return ``(final_state, result)``."""

        for _ in range(self.max_polls):
            try:
                response = client.get(f"/extract/task/{task_id}")
            except httpx.HTTPError as exc:
                raise NetworkTransientError(
                    f"polling failed for task {task_id}: {exc}",
                    details={"task_id": task_id},
                ) from exc

            if response.status_code == 404:
                raise EngineProtocolUnsupportedError(
                    f"MinerU task {task_id} no longer exists (server-side eviction?)",
                    details={"task_id": task_id},
                )

            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise EngineProtocolUnsupportedError(
                    "MinerU poll response was not JSON",
                    details={"task_id": task_id, "body": response.text[:200]},
                ) from exc

            if not isinstance(payload, dict) or "data" not in payload:
                raise EngineProtocolUnsupportedError(
                    "MinerU poll response has no 'data' field",
                    details={"task_id": task_id, "body": str(payload)[:200]},
                )

            data = payload["data"]
            # A successful final response from MinerU has ``full_zip_url``
            # (or ``full_md_link``) but no ``state`` field at all. Treat
            # the presence of a result URL as the success signal.
            if data.get("full_zip_url") or data.get("zip_result_url") or data.get("full_md_link"):
                result = self._fetch_result(client, data, task_id)
                return "done", result

            state = str(
                data.get("state")
                or data.get("status")
                or "unknown"
            ).lower()

            if state in _FAILED_STATES:
                raise EngineUnavailableError(
                    f"MinerU task {task_id} failed with state {state!r}",
                    details={"task_id": task_id, "state": state, "data": data},
                )
            if state in _COMPLETED_STATES:
                result = self._fetch_result(client, data, task_id)
                return state, result

            time.sleep(self.poll_interval)

        raise EngineTimeoutError(
            f"timed out polling MinerU task {task_id} after {self.max_polls} attempts",
            details={"task_id": task_id, "max_polls": self.max_polls},
        )

    def _fetch_result(
        self,
        client: httpx.Client,
        data: dict[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        """Download the parsed result; embed as a synthetic document.

        MinerU V1 returns one of:
        - ``full_zip_url``: a zip containing ``*_content_list.json`` (per-block
          list) plus ``full.md`` and extracted images.
        - ``full_md_link``: a direct link to ``full.md`` (markdown only).
        - ``content_list``: the raw block list, inlined in the response.
        """

        zip_url = data.get("full_zip_url") or data.get("zip_result_url")
        if zip_url:
            return self._fetch_zip_result(client, zip_url, task_id)

        md_link = data.get("full_md_link") or data.get("md_link") or data.get("result_url")
        if not md_link:
            # Some MinerU plans put the content directly under data.
            return data

        try:
            response = client.get(md_link)
        except httpx.HTTPError as exc:
            raise NetworkTransientError(
                f"failed to download MinerU result for {task_id}: {exc}",
                details={"task_id": task_id, "md_link": md_link},
            ) from exc
        if response.status_code != 200:
            raise EngineUnavailableError(
                f"MinerU result download returned {response.status_code}",
                details={"task_id": task_id, "md_link": md_link},
            )

        try:
            return response.json()
        except json.JSONDecodeError:
            return {
                "format": "markdown",
                "task_id": task_id,
                "markdown": response.text,
            }

    def _fetch_zip_result(
        self,
        client: httpx.Client,
        zip_url: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Download the result zip, pick out ``*_content_list.json`` + ``full.md``."""

        import io
        import zipfile

        try:
            response = client.get(zip_url)
        except httpx.HTTPError as exc:
            raise NetworkTransientError(
                f"failed to download MinerU result zip for {task_id}: {exc}",
                details={"task_id": task_id, "zip_url": zip_url},
            ) from exc
        if response.status_code != 200:
            raise EngineUnavailableError(
                f"MinerU zip download returned {response.status_code}",
                details={"task_id": task_id, "zip_url": zip_url},
            )

        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                members = zf.namelist()
                content_list_name = next(
                    (m for m in members if m.endswith("_content_list.json")),
                    None,
                )
                content_list_v2_name = next(
                    (m for m in members if m.endswith("_content_list_v2.json")),
                    None,
                )
                full_md_name = "full.md" if "full.md" in members else None

                content_list: list[dict[str, Any]] = []
                if content_list_name is not None:
                    content_list = json.loads(zf.read(content_list_name).decode("utf-8"))

                full_md = ""
                if full_md_name is not None:
                    full_md = zf.read(full_md_name).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EngineProtocolUnsupportedError(
                f"MinerU result zip could not be parsed: {exc}",
                details={"task_id": task_id},
            ) from exc

        return {
            "format": "zip",
            "task_id": task_id,
            "content_list": content_list,
            "markdown": full_md,
        }

    # ---- manifest --------------------------------------------------------

    def _manifest(
        self,
        *,
        request: ParseRequest,
        document: NormalizedDocument,
        task_id: str,
        raw_path: Path,
        normalized_path: Path,
        source_identity_verified: bool = False,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "engine": self.name,
            "parser_run_id": document.parser_run_id,
            "document_id": document.document_id,
            "profile": request.profile,
            "pages": list(request.page_numbers),
            "options_ref": request.options_ref,
            "model_version": self.model_version,
            "remote_task_id": task_id,
            "base_url": self.base_url,
            "capabilities": {
                "tables": True,
                "ocr": True,
                "structure": True,
                "vlm": True,
            },
            "source_identity_verified": source_identity_verified,
            "artifacts": {
                "normalized": str(normalized_path.relative_to(request.output_dir)),
                "raw_response": str(raw_path.relative_to(request.output_dir)),
            },
            "page_coverage": {
                "requested": list(request.page_numbers),
                "produced": [p.page_number for p in document.pages],
            },
            "errors": list(document.errors),
        }


# ---------------------------------------------------------------------------
# MinerU → Docling shape projection
# ---------------------------------------------------------------------------


def _project_mineru_payload(
    payload: dict[str, Any],
    *,
    page_numbers: set[int],
) -> dict[str, Any]:
    """Map MinerU's result envelope into a Docling-shaped dict.

    The MinerU SaaS returns one of:
    - a top-level ``content_list`` (array of per-block items with a
      ``page_idx`` field, 0-based);
    - a ``markdown`` string (rendered output);
    - or a payload that *already* has a ``pages`` key (e.g. cached
      responses or alternative MinerU deployments).

    The normaliser in :mod:`bio_pdf.parsers.normalize_docling` expects
    ``{"pages": [{"page": N, "width": w, "height": h, "texts": [...]}, ...]}``.
    We build that here, with one entry per page MinerU reported.
    """

    if "content_list" in payload and isinstance(payload["content_list"], list):
        return _project_content_list(payload["content_list"])

    if "markdown" in payload and isinstance(payload["markdown"], str):
        # We have no per-page geometry; build a single synthetic page that
        # preserves the markdown as a single block. Downstream code can
        # split by line if it needs finer granularity.
        return {
            "pages": [
                {
                    "page": next(iter(page_numbers), 1),
                    "size": {"width": 612.0, "height": 792.0},
                    "width": 612.0,
                    "height": 792.0,
                    "texts": [
                        {
                            "self_ref": "mineru:md:0",
                            "label": "text",
                            "text": payload["markdown"],
                        }
                    ],
                }
            ]
        }

    if "pages" in payload and isinstance(payload["pages"], list) and payload["pages"]:
        return {"pages": payload["pages"]}

    # Last resort: empty pages list. The normaliser will raise a clear error.
    return {"pages": []}


_PAGE_HEADER_RE = re.compile(r"^\s*<!--\s*page\s+(\d+)\s*-->", re.IGNORECASE)

#: MinerU block types that carry an HTML ``table_body``.
_TABLE_TYPES = {"table", "table_body"}


def _project_table_item(item: dict[str, Any], page_no: int, seq: int) -> dict[str, Any] | None:
    """Convert a MinerU table item (``table_body`` HTML) into a Docling-shaped table.

    The HTML is parsed with a tolerant regex-based cell walker (no external
    dependency): ``rowspan``/``colspan`` attributes are honoured so merged
    headers keep their spans. Rows/cols that cannot be parsed are dropped
    from the structured view but the raw HTML is preserved on the page as a
    text block so no information is silently lost.
    """

    html = item.get("table_body") or ""
    if not html:
        return None

    bbox = item.get("bbox") or item.get("position")
    bbox_out: dict[str, float] | None = None
    try:
        if isinstance(bbox, dict):
            bbox_out = {
                "l": float(bbox.get("l", bbox.get("left", 0))),
                "t": float(bbox.get("t", bbox.get("top", 0))),
                "r": float(bbox.get("r", bbox.get("right", 0))),
                "b": float(bbox.get("b", bbox.get("bottom", 0))),
            }
        elif isinstance(bbox, (list, tuple)) and len(bbox) == 8:
            # MinerU sometimes emits 8-number bboxes (4 corner points).
            xs = [float(bbox[i]) for i in (0, 2, 4, 6)]
            ys = [float(bbox[i]) for i in (1, 3, 5, 7)]
            bbox_out = {"l": min(xs), "t": min(ys), "r": max(xs), "b": max(ys)}
        elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            bbox_out = {"l": float(bbox[0]), "t": float(bbox[1]),
                        "r": float(bbox[2]), "b": float(bbox[3])}
    except (TypeError, ValueError):
        bbox_out = None

    cells: list[dict[str, Any]] = []
    row_idx = -1
    # Walk <tr> rows, then <td>/<th> cells with their attributes.
    for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.IGNORECASE | re.DOTALL):
        row_idx += 1
        col_idx = 0
        for cell_m in re.finditer(r"<(td|th)\b([^>]*)>(.*?)</\1>", tr_m.group(1), re.IGNORECASE | re.DOTALL):
            attrs = cell_m.group(2) or ""
            inner = re.sub(r"<[^>]+>", " ", cell_m.group(3))
            inner = re.sub(r"\s+", " ", inner).strip()

            def _span(name: str) -> int:
                sm = re.search(rf"{name}\s*=\s*[\"']?(\d+)", attrs, re.IGNORECASE)
                return max(int(sm.group(1)), 1) if sm else 1

            cells.append(
                {
                    "start_row_offset_idx": row_idx,
                    "start_col_offset_idx": col_idx,
                    "row_span": _span("rowspan"),
                    "col_span": _span("colspan"),
                    "text": inner,
                }
            )
            col_idx += _span("colspan")

    # Empty placeholder cells (e.g. the void under a merged header) carry no
    # text and the contract requires text_raw to be non-empty; skip them.
    # Grid positions of the remaining cells are unaffected because they are
    # indexed by explicit row/col offsets.
    cells = [c for c in cells if c["text"]]

    if not cells:
        return None

    table: dict[str, Any] = {
        "self_ref": f"mineru:table:{page_no}:{seq}",
        "prov": [{"page_no": page_no}],
        "data": {"table_cells": cells},
    }
    if bbox_out is not None:
        table["prov"][0]["bbox"] = {**bbox_out, "coord_origin": "TOPLEFT"}

    # Keep the raw HTML available as evidence (attribute named like Docling
    # captions so downstream consumers can display it).
    caption = item.get("table_caption") or item.get("img_caption")
    if isinstance(caption, list):
        caption = " ".join(str(c) for c in caption)
    if caption:
        table["caption"] = str(caption)
    return table


def _project_content_list(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Docling-shaped pages dict from a MinerU ``content_list``."""

    pages: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        page_idx = item.get("page_idx")
        try:
            page_no = int(page_idx) + 1 if page_idx is not None else 1
        except (TypeError, ValueError):
            page_no = 1
        page = pages.setdefault(
            page_no,
            {
                "page": page_no,
                "size": {"width": 612.0, "height": 792.0},
                "width": 612.0,
                "height": 792.0,
                "texts": [],
                "tables": [],
            },
        )

        item_type = str(item.get("type") or item.get("label") or "text").lower()

        # Tables: the content lives in ``table_body`` HTML; projecting only
        # ``text``/``content`` would silently drop every table.
        if item_type in _TABLE_TYPES and (item.get("table_body") or ""):
            table_payload = _project_table_item(item, page_no, len(page["tables"]))
            if table_payload is not None:
                page["tables"].append(table_payload)
                continue

        text = item.get("text") or item.get("content") or ""
        if not text:
            continue

        bbox = item.get("bbox") or item.get("position")
        try:
            if isinstance(bbox, dict):
                bbox_out = {
                    "l": float(bbox.get("l", bbox.get("left", 0))),
                    "t": float(bbox.get("t", bbox.get("top", 0))),
                    "r": float(bbox.get("r", bbox.get("right", 0))),
                    "b": float(bbox.get("b", bbox.get("bottom", 0))),
                }
            elif isinstance(bbox, (list, tuple)) and len(bbox) == 8:
                xs = [float(bbox[i]) for i in (0, 2, 4, 6)]
                ys = [float(bbox[i]) for i in (1, 3, 5, 7)]
                bbox_out = {"l": min(xs), "t": min(ys), "r": max(xs), "b": max(ys)}
            elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                bbox_out = {"l": float(bbox[0]), "t": float(bbox[1]),
                            "r": float(bbox[2]), "b": float(bbox[3])}
            else:
                bbox_out = None
        except (TypeError, ValueError):
            bbox_out = None
        if bbox_out is not None:
            bbox_out = {**bbox_out, "coord_origin": "TOPLEFT"}

        label = item.get("type") or item.get("label") or "text"
        page["texts"].append(
            {
                "self_ref": f"mineru:p{page_no}:{len(page['texts'])}",
                "label": label,
                "bbox": bbox_out,
                "text": str(text),
            }
        )

    return {"pages": [pages[k] for k in sorted(pages.keys())]}


__all__ = ["MinerUAdapter", "DEFAULT_BASE_URL", "DEFAULT_MODEL_VERSION"]