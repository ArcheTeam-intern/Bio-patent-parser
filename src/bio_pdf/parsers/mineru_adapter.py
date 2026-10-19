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

import hashlib
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
    RemoteBatchFailedError,
    UploadSlotExpiredError,
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


def _identity_lock(path: Path, *, timeout: float = 30.0):
    """J01/K04: exclusive lock around identity read-modify-write decisions.

    The lock is created with O_EXCL; a stale lock (crashed process) older
    than 120 s is removed. The K04 ``transition`` API wraps this internally
    so adapter code rarely needs the lock directly; this helper exists for
    the all-or-nothing allocation pass.
    """

    import contextlib
    import os
    import time

    @contextlib.contextmanager
    def _lock():
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout
        fd = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > 120.0:
                    with contextlib.suppress(OSError):
                        lock_path.unlink()
                    continue
                if time.time() > deadline:
                    raise EngineUnavailableError(
                        "timed out waiting for the remote-identity lock",
                        details={"lock": str(lock_path), "code": "IDENTITY_LOCK_TIMEOUT"},
                    )
                time.sleep(0.1)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            yield
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                lock_path.unlink()

    return _lock()


def _write_identity(path: Path, identity: dict[str, Any]) -> None:
    """Atomic-ish identity write for the all-or-nothing allocation pass.

    Subsequent transitions go through ``remote_identity_store.transition``
    which fences on (fingerprint, generation, owner_token, revision). The
    allocation is a CREATE — there is no prior version to fence against —
    so a plain atomic write is the correct primitive here.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _apply_identity_transition(
    identity_path: Path,
    *,
    identity_now: dict[str, Any],
    patch: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """K04: every post-allocation identity change goes through the fenced
    store. The expected version is built from the read snapshot; a stale
    write raises :class:`TransitionRejected` and the caller MUST re-read
    and decide whether the new state supersedes the patch.
    """

    from bio_pdf.parsers.remote_identity_store import (
        IdentityVersion,
        TransitionRejected,
        transition,
    )

    expected = IdentityVersion(
        fingerprint=str(identity_now.get("fingerprint") or ""),
        generation=int(identity_now.get("generation") or 0),
        owner_token=str(identity_now.get("owner_token") or ""),
        revision=int(identity_now.get("revision") or 0),
    )
    try:
        return transition(
            identity_path,
            expected_version=expected,
            patch=patch,
            history_label=label,
        )
    except TransitionRejected as exc:
        # The store's reason is the forensic record; the caller decides.
        return exc.current




def _page_geometry_descriptor(
    document: NormalizedDocument,
    *,
    page_sizes: dict[int, tuple[float, float, int]],
    declared_basis: Any,
    projection_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """F07/P03: geometry facts stay SEPARATE and honest.

    ``page_size_verified`` (we know the real page size), ``bounds_valid``
    (no box violates the page / is non-finite / is inverted) and
    ``coordinate_basis_verified`` (a JUSTIFIED unit+origin+rotation+crop
    transform exists — declared by the protocol payload) are three
    different claims. A box merely not exceeding the page does NOT prove
    the coordinate basis: an unknown pixel/normalized frame can land
    in-bounds by coincidence. While the basis is unverified the boxes stay
    RAW (no rescale/clamp) and precise evidence-location promises are
    blocked, not implied.
    """

    import math

    # Warnings raised during PROJECTION (a dropped non-finite box never
    # reaches the normalized document) still count against bounds validity.
    warnings: list[str] = list(projection_warnings or [])
    unverified_pages: list[int] = []
    produced = [p.page_number for p in document.pages]
    for page in document.pages:
        if page.width_pt <= 0 or page.height_pt <= 0:
            warnings.append(f"page {page.page_number}: unknown page size — geometry unverified")
            unverified_pages.append(page.page_number)
            continue
        items = list(page.blocks) + list(page.tables) + list(page.figures)
        for item in items:
            bbox = getattr(item, "bbox", None)
            if not bbox or len(bbox) < 4:
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in bbox[:4])
            except (TypeError, ValueError):
                warnings.append(
                    f"page {page.page_number}: non-numeric box — geometry unverified"
                )
                continue
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
                warnings.append(
                    f"page {page.page_number}: non-finite box coordinates "
                    f"({x0},{y0},{x1},{y1}) — box rejected, raw payload retained"
                )
                continue
            if x1 < x0 or y1 < y0:
                warnings.append(
                    f"page {page.page_number}: inverted box ({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}) "
                    "— bounds not valid"
                )
            tol = 2.0
            if (
                x0 < -tol or y0 < -tol
                or x1 > page.width_pt + tol or y1 > page.height_pt + tol
            ):
                item_id = getattr(item, "block_id", None) or getattr(item, "table_id", None) or getattr(item, "figure_id", "?")
                warnings.append(
                    f"page {page.page_number}: box {item_id} ({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}) "
                    f"exceeds the {page.width_pt:.0f}x{page.height_pt:.0f}pt page — "
                    "MinerU coordinate basis unverified"
                )

    page_size_verified = bool(produced) and all(
        pn in page_sizes and page_sizes[pn][0] > 0 and page_sizes[pn][1] > 0
        for pn in produced
    )

    spec = declared_basis if isinstance(declared_basis, dict) else None
    basis_notes: list[str] = []
    coordinate_basis_verified = False
    if spec is None:
        basis_notes.append(
            "no coordinate basis was declared by the protocol (unit/origin/rotation/crop "
            "unknown) — boxes are raw and precise evidence location is not guaranteed"
        )
    else:
        unit = str(spec.get("unit") or "").lower()
        origin = str(spec.get("origin") or "").lower()
        rotation_applied = bool(spec.get("rotation_applied"))
        crop_applied = bool(spec.get("crop_applied"))
        if unit == "normalized_1000":
            # G09/P04: the content_list protocol's documented frame — the
            # conversion was applied per page (see _project_bbox); it is a
            # verified basis exactly when page sizes were real, rotation
            # was handled, and the pages were not cropped.
            if crop_applied:
                basis_notes.append(
                    "a crop transform was applied upstream — page-space mapping unverified"
                )
            else:
                rotated = sorted(
                    pn
                    for pn in produced
                    if int(page_sizes.get(pn, (0.0, 0.0, 0))[2] or 0) % 360 != 0
                )
                if rotated and not rotation_applied:
                    basis_notes.append(
                        f"pages {rotated} are rotated and rotation_applied is not declared — "
                        "the box frame cannot be trusted"
                    )
                elif not page_size_verified:
                    basis_notes.append(
                        "page sizes are not verified — the box→page mapping cannot be trusted"
                    )
                else:
                    coordinate_basis_verified = True
        elif unit != "pt":
            basis_notes.append(
                f"declared unit {unit!r} is not pt — no conversion is invented; boxes stay raw"
            )
        elif origin not in ("top_left", "bottom_left"):
            basis_notes.append(
                "no origin was declared — a trusted page frame cannot be justified"
            )
        elif crop_applied:
            basis_notes.append(
                "a crop transform was applied upstream (not declared as page-space) — "
                "page-space mapping unverified"
            )
        else:
            rotated = sorted(
                pn
                for pn in produced
                if int(page_sizes.get(pn, (0.0, 0.0, 0))[2] or 0) % 360 != 0
            )
            if rotated and not rotation_applied:
                basis_notes.append(
                    f"pages {rotated} are rotated and rotation_applied is not declared — "
                    "the box frame cannot be trusted"
                )
            elif not page_size_verified:
                basis_notes.append(
                    "page sizes are not verified — the box→page mapping cannot be trusted"
                )
            else:
                coordinate_basis_verified = True

    # G09: boxes the protocol transform could not explain (outside the
    # 0–1000 range) keep the WHOLE basis unverified — a partially converted
    # frame must not be advertised as verified.
    range_failures = [w for w in (projection_warnings or []) if "protocol range" in w]
    if range_failures:
        coordinate_basis_verified = False
        basis_notes.append(
            f"{len(range_failures)} box(es) fall outside the 0–1000 content_list "
            "protocol range — those boxes stay raw and the basis is not verified"
        )

    return {
        "page_size_verified": page_size_verified,
        "coordinate_basis_verified": coordinate_basis_verified,
        "bounds_valid": not warnings,
        "bbox_warnings": warnings,
        "unverified_pages": sorted(set(unverified_pages)),
        "basis_notes": basis_notes,
    }


def _source_page_geometry(
    source_path: Path, pages: list[int]
) -> dict[int, tuple[float, float, int]]:
    """Real page size + rotation from the SOURCE PDF (D07).

    Returns ``{page_number: (width_pt, height_pt, rotation_deg)}`` for the
    requested pages. Missing/unreadable source or pages degrade to an empty
    map — the caller then falls back to neutral defaults and the manifest
    records that the geometry basis is unverified, never a guessed size.
    """

    out: dict[int, tuple[float, float, int]] = {}
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(source_path))
        try:
            for page_number in pages:
                if 1 <= page_number <= len(doc):
                    page = doc[page_number - 1]
                    width, height = page.get_size()
                    rotation = int(page.get_rotation() or 0)
                    out[page_number] = (float(width), float(height), rotation)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 — geometry is best-effort, never fatal
        return {}
    return out


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

    # ---- v4 batch protocol (C10) -----------------------------------------

    def allocate_upload_slot(
        self,
        file_path: Path,
        *,
        data_id: str,
        enable_formula: bool = True,
        enable_table: bool = True,
        language: str = "en",
        is_ocr: bool = True,
        page_ranges: str | None = None,
    ) -> tuple[str, list[str], str]:
        """``POST /file-urls/batch`` → ``(batch_id, signed_put_urls, file_name)``.

        H03/S03: ALLOCATION ONLY — no bytes are uploaded here. The caller
        persists the batch/slot identity (with its fingerprint) BEFORE the
        PUT, so a lost PUT response still recovers as "already allocated:
        re-PUT the same slot" instead of allocating a second batch.

        D06: the caller's effective parameters (language / table / OCR /
        page ranges) travel into the request — real options in, not silent
        defaults.
        """

        file_name = file_path.name
        files_entry: dict[str, Any] = {
            "name": file_name,
            "is_ocr": is_ocr,
            "data_id": data_id,
        }
        if page_ranges:
            files_entry["page_ranges"] = page_ranges
        body = {
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "language": language,
            "model_version": self.model_version,
            "files": [files_entry],
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
            if response.status_code == 429 or response.status_code >= 500:
                # I03: allocation-side temporary failure — retryable, and the
                # caller's identity stays in its pre-allocation phase.
                raise NetworkTransientError(
                    f"MinerU file-urls returned HTTP {response.status_code} (retryable)",
                    details={
                        "status": response.status_code,
                        "code": "UPLOAD_ALLOC_RETRYABLE",
                        "reason": "server-temporary",
                    },
                )
            if response.status_code >= 400:
                raise EngineProtocolUnsupportedError(
                    f"MinerU file-urls returned {response.status_code}: {response.text[:300]}",
                    details={"status": response.status_code},
                )

            payload = response.json()
            data_block = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data_block, dict):
                raise EngineProtocolUnsupportedError(
                    "MinerU file-urls response has no 'data' object",
                    details={"body": str(payload)[:300]},
                )
            batch_id = str(data_block.get("batch_id") or "")
            file_urls = data_block.get("file_urls")
            if not batch_id or not isinstance(file_urls, list) or not file_urls:
                raise EngineProtocolUnsupportedError(
                    "MinerU file-urls response lacks batch_id or upload slots",
                    details={"data_keys": sorted(data_block.keys())},
                )
            upload_url = str(file_urls[0])
            if not upload_url.startswith(("http://", "https://")):
                raise EngineProtocolUnsupportedError(
                    "MinerU file-urls slot is not a URL",
                    details={"slot": upload_url[:200]},
                )
        return batch_id, [str(u) for u in file_urls], file_name

    def upload_to_slot(self, upload_url: str, file_path: Path) -> None:
        """``PUT`` the file bytes to an ALREADY-ALLOCATED slot (H03).

        Idempotent by design: re-PUTting the same slot after a lost response
        is the recovery path, never a fresh allocation. No Content-Type (the
        OSS signature was computed without it) and — critically — no MinerU
        auth header: signed URLs must never see the platform token. A bare
        client keeps the injected transport (tests) while dropping the auth
        headers.
        """

        if not upload_url.startswith(("http://", "https://")):
            raise EngineProtocolUnsupportedError(
                "MinerU file-urls slot is not a URL",
                details={"slot": upload_url[:200]},
            )
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as raw_client:
                put_resp = raw_client.put(upload_url, content=file_path.read_bytes())
        except httpx.HTTPError as exc:
            # I03/S03: network failures are RETRYABLE with the SAME slot —
            # never evidence of expiry.
            raise NetworkTransientError(
                f"failed to PUT file to MinerU (retryable, same slot): {exc}",
                details={
                    "upload_url": upload_url[:200],
                    "code": "UPLOAD_RETRYABLE",
                    "reason": "network",
                },
            ) from exc
        status = put_resp.status_code
        if status in (200, 201, 204):
            return
        if status in (401, 403, 404):
            # Confirmed expiry/invalid signature — the ONLY reallocation
            # trigger.
            raise UploadSlotExpiredError(
                f"MinerU upload slot is no longer valid (HTTP {status})",
                details={
                    "upload_url": upload_url[:200],
                    "status": status,
                    "code": "UPLOAD_SLOT_EXPIRED",
                },
            )
        if status == 429 or status >= 500:
            raise NetworkTransientError(
                f"MinerU file PUT returned HTTP {status} (retryable, same slot)",
                details={
                    "upload_url": upload_url[:200],
                    "status": status,
                    "code": "UPLOAD_RETRYABLE",
                    "reason": "server-temporary",
                },
            )
        raise EngineProtocolUnsupportedError(
            f"MinerU file PUT returned HTTP {status}",
            details={"upload_url": upload_url[:200], "status": status, "body": put_resp.text[:200]},
        )

    def poll_batch_entry(
        self,
        client: httpx.Client,
        batch_id: str,
        *,
        file_name: str,
    ) -> dict[str, Any]:
        """Poll ``GET /extract-results/batch/{batch_id}`` for our file's entry."""

        for _ in range(self.max_polls):
            try:
                response = client.get(f"/extract-results/batch/{batch_id}")
            except httpx.HTTPError as exc:
                raise NetworkTransientError(
                    f"batch polling failed for {batch_id}: {exc}",
                    details={"batch_id": batch_id},
                ) from exc
            if response.status_code in (401, 403):
                raise EngineUnavailableError(
                    "MinerU rejected the Bearer token while polling batch",
                    details={"batch_id": batch_id, "status": response.status_code},
                )
            if response.status_code == 404:
                raise EngineProtocolUnsupportedError(
                    f"MinerU batch {batch_id} no longer exists (server-side eviction?)",
                    details={"batch_id": batch_id},
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise EngineProtocolUnsupportedError(
                    "MinerU batch poll response was not JSON",
                    details={"batch_id": batch_id, "body": response.text[:200]},
                ) from exc

            data_block = payload.get("data") if isinstance(payload, dict) else None
            entries = (data_block or {}).get("extract_result")
            if not isinstance(entries, list):
                raise EngineProtocolUnsupportedError(
                    "MinerU batch response has no extract_result list",
                    details={"data_keys": sorted((data_block or {}).keys())},
                )
            entry = next(
                (
                    e
                    for e in entries
                    if isinstance(e, dict) and str(e.get("file_name") or "") == file_name
                ),
                None,
            )
            if entry is None:
                # The batch result may still be filling in; keep polling.
                time.sleep(self.poll_interval)
                continue

            if entry.get("full_zip_url"):
                return entry
            state = str(entry.get("state") or "unknown").lower()
            if state in _FAILED_STATES:
                # J01: a REMOTE terminal failure is not a transport error —
                # polling this batch again can never succeed.
                raise RemoteBatchFailedError(
                    f"MinerU batch file {file_name} failed with state {state!r}",
                    details={
                        "batch_id": batch_id,
                        "state": state,
                        "entry": entry,
                        "code": "REMOTE_BATCH_FAILED",
                    },
                )
            if state in _COMPLETED_STATES and not entry.get("full_zip_url"):
                raise EngineProtocolUnsupportedError(
                    "MinerU reported completion without a result package URL",
                    details={"batch_id": batch_id, "entry": entry},
                )
            time.sleep(self.poll_interval)

        raise EngineTimeoutError(
            f"timed out polling MinerU batch {batch_id} after {self.max_polls} attempts",
            details={"batch_id": batch_id, "max_polls": self.max_polls},
        )

    # ---- v1 public-url flow (unchanged, only for already-public sources) --

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
            if response.status_code == 429 or response.status_code >= 500:
                # I03: allocation-side temporary failure — retryable, and the
                # caller's identity stays in its pre-allocation phase.
                raise NetworkTransientError(
                    f"MinerU file-urls returned HTTP {response.status_code} (retryable)",
                    details={
                        "status": response.status_code,
                        "code": "UPLOAD_ALLOC_RETRYABLE",
                        "reason": "server-temporary",
                    },
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
            return self._parse_via_batch(request, local_file)
        if not source_url or not source_url.lower().startswith(("http://", "https://")):
            raise EngineProtocolUnsupportedError(
                "MinerU requires a public http(s) URL or a local_file to upload",
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
        ordered_pages = sorted(int(p) for p in request.page_numbers)
        page_sizes = _source_page_geometry(request.source_path, ordered_pages)
        payload_basis = (
            result_payload.get("coord_basis") if isinstance(result_payload, dict) else None
        )
        projected = _project_mineru_payload(
            result_payload,
            page_numbers=set(request.page_numbers),
            page_order=ordered_pages or None,
            page_sizes=page_sizes,
            declared_basis=payload_basis,
        )
        projection_warnings = projected.pop("_projection_warnings", [])
        # G09: the EFFECTIVE basis is what governed the conversion — the
        # payload's own declaration, or the content_list protocol default.
        declared_basis = projected.pop("_coordinate_basis", None) or payload_basis
        document = normalize_docling_document(
            projected,
            parser_run_id=f"{request.job_id}:mineru",
            document_id=request.document_id,
        )

        # F07/P03: three separate geometry facts (same contract as the batch path).
        descriptor = _page_geometry_descriptor(
            document,
            page_sizes=page_sizes,
            declared_basis=declared_basis,
            projection_warnings=projection_warnings,
        )
        doc_errors = (
            list(document.errors) + list(projection_warnings) + list(descriptor["bbox_warnings"])
        )
        if descriptor["basis_notes"]:
            doc_errors.append(
                "mineru coordinate basis unverified: " + "; ".join(descriptor["basis_notes"])
            )
        if not descriptor["bounds_valid"]:
            doc_errors.append(
                "box bounds invalid: geometry bounds are not verified (raw boxes retained)"
            )
        document = document.model_copy(update={"errors": doc_errors})

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
        manifest["page_geometry"] = {
            "basis": "source-pdf" if page_sizes else "unknown-defaults",
            "declared_coordinate_basis": declared_basis,
            "page_size_verified": descriptor["page_size_verified"],
            "coordinate_basis_verified": descriptor["coordinate_basis_verified"],
            "bounds_valid": descriptor["bounds_valid"],
            "raw_bbox_retained": True,
            "bbox_warnings": descriptor["bbox_warnings"][:50],
            "unverified_pages": descriptor["unverified_pages"],
            "basis_notes": descriptor["basis_notes"],
            "page_count": len(document.pages),
            "rotated_pages": sorted(
                page_no
                for page_no, (_w, _h, rotation) in (page_sizes or {}).items()
                if rotation
            ),
        }
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
            # The result package URL is pre-signed; download WITHOUT the
            # platform token (bare client, injected transport preserved).
            with httpx.Client(timeout=self.timeout, transport=self._transport) as raw_client:
                response = raw_client.get(zip_url)
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

    def _effective_remote_params(self, request: ParseRequest) -> dict[str, Any]:
        """D06: map the effective local options onto MinerU's protocol.

        The acceptance found the upload body stuck on defaults (en / table /
        ocr always on) while the request asked for ``ocr_lang=ch``,
        ``do_table_structure=false`` and ``layout_native`` — parameters must
        travel, not be silently replaced.
        """

        options = dict(request.parser_options or {})
        language = str(options.get("ocr_lang") or options.get("language") or "en")
        # Map common language tags onto MinerU's codes.
        lang_lower = language.lower()
        if lang_lower.startswith("zh") or lang_lower in ("chinese", "ch"):
            language = "ch"
        elif lang_lower.startswith("en") or lang_lower in ("english", "en"):
            language = "en"
        enable_table = bool(options.get("do_table_structure", True))
        # OCR follows the PROFILE: layout_ocr pages exist precisely because
        # the text layer is untrusted for them.
        is_ocr = request.profile == "layout_ocr"
        enable_formula = bool(options.get("enable_formula", True))

        pages = sorted(int(p) for p in request.page_numbers)
        page_ranges: str | None = None
        if pages:
            # Compact runs: "30-33,40".
            runs: list[str] = []
            start = prev = pages[0]
            for p in pages[1:]:
                if p == prev + 1:
                    prev = p
                    continue
                runs.append(f"{start}-{prev}" if start != prev else str(start))
                start = prev = p
            runs.append(f"{start}-{prev}" if start != prev else str(start))
            page_ranges = ",".join(runs)

        return {
            "language": language,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
            "is_ocr": is_ocr,
            "page_ranges": page_ranges,
            "model_version": self.model_version,
        }

    def _stable_job_dir(self, request: ParseRequest) -> Path:
        """D06/G05: the cross-attempt state directory for this job.

        Attempt dirs (``jobs/<job>/stage_runs/<run>/run-N``) are recreated on
        every workflow retry, so the remote identity must live one level up
        at ``jobs/<job_id>/`` — otherwise every real retry re-submitted the
        document to the cloud.

        G05/S05: comparison sides pass an EXPLICIT ``remote_state_dir``
        (``comparisons/<cid>/<engine>/remote``) because their ``job_id`` is
        ``<cid>:<engine>`` and their attempt dirs are per-attempt — the
        ancestor search below could never find such a directory, so every
        comparison retry re-uploaded. The explicit directory wins whenever
        it is provided.
        """

        if request.remote_state_dir:
            return Path(request.remote_state_dir)

        for candidate in (request.output_dir, *request.output_dir.parents):
            if candidate.name == request.job_id:
                return candidate
        return request.output_dir

    def _parse_via_batch(self, request: ParseRequest, local_file: Path) -> ParseArtifacts:
        """C10/D06: the v4 batch flow for local files (upload → poll → zip).

        - SHA-256 is verified BEFORE anything is uploaded.
        - The remote identity lives in a CROSS-ATTEMPT stable directory
          (``jobs/<job_id>/mineru_remote_identity.json``) and is bound to a
          fingerprint of the full request identity — source SHA, page set,
          engine/model and effective parameters. A retry with an identical
          fingerprint POLLS the existing batch; ANY difference (re-uploaded
          source, changed pages, changed options, different engine version)
          submits a fresh remote task instead of mislabelling the old result.
        - ``full_zip_url`` is downloaded with the plain (non-authenticated)
          client, mirroring the upload path: signed URLs never see the
          platform token.
        """

        from datetime import datetime, timezone

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

        request.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = request.output_dir / "raw_response.json"
        normalized_path = request.output_dir / "normalized.json"
        manifest_path = request.output_dir / "manifest.json"

        params = self._effective_remote_params(request)
        fingerprint_payload = {
            "source_sha256": local_sha,
            "pages": sorted(int(p) for p in request.page_numbers),
            "profile": request.profile,
            "base_url": self.base_url,
            "params": params,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]

        stable_dir = self._stable_job_dir(request)
        identity_path = stable_dir / "mineru_remote_identity.json"

        identity: dict[str, Any] | None = None
        if identity_path.is_file():
            try:
                loaded = json.loads(identity_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict) and loaded.get("fingerprint") == fingerprint:
                identity = loaded

        # I02/S02 + J01: explicit recovery dispatch over EVERY persisted
        # phase. A fresh allocation happens when there is no identity, the
        # slot is CONFIRMED expired, or the REMOTE BATCH reached a terminal
        # failure (polling it again can never succeed — an explicit retry
        # must produce a new generation). The identity is persisted before
        # any upload; only uploaded/polling/completed may be polled.
        phase = str(identity.get("phase") or "") if identity is not None else ""
        if identity is None or phase in ("slot_expired", "terminal_failed"):
            # J01: the allocation decision is serialized per identity so two
            # concurrent retries cannot both submit a new generation.
            with _identity_lock(identity_path):
                # Re-read under the lock: a sibling retry may have just
                # created the new generation we were about to create.
                fresh: dict[str, Any] | None = None
                if identity_path.is_file():
                    try:
                        loaded_now = json.loads(identity_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        loaded_now = None
                    if isinstance(loaded_now, dict) and loaded_now.get("fingerprint") == fingerprint:
                        fresh = loaded_now
                if fresh is not None and str(fresh.get("phase") or "") not in (
                    "slot_expired",
                    "terminal_failed",
                ):
                    identity = fresh  # another retry already allocated
                else:
                    previous = fresh if fresh is not None else identity
                    generation = int((previous or {}).get("generation") or 0) + 1
                    history = list((previous or {}).get("batch_history") or [])
                    previous_batch = (previous or {}).get("batch_id")
                    if previous_batch and previous_batch not in history:
                        history.append(previous_batch)
                    batch_id, slots, file_name = self.allocate_upload_slot(
                        local_file,
                        data_id=local_sha[:16],
                        language=str(params["language"]),
                        enable_table=bool(params["enable_table"]),
                        enable_formula=bool(params["enable_formula"]),
                        is_ocr=bool(params["is_ocr"]),
                        page_ranges=params["page_ranges"],
                    )
                    identity = {
                        "batch_id": batch_id,
                        "data_id": local_sha[:16],
                        "file_name": file_name,
                        "upload_url": str(slots[0]),
                        "base_url": self.base_url,
                        "fingerprint": fingerprint,
                        "source_sha256": local_sha,
                        "pages": fingerprint_payload["pages"],
                        "profile": request.profile,
                        "params": params,
                        "phase": "allocated",
                        "generation": generation,
                        "batch_history": history,
                        # J01: which attempt/lease owns this generation.
                        "last_attempt_token": request.attempt_token,
                        # K04: the fenced store keys ``owner_token`` for
                        # compare-and-swap. We pin it to the attempt's
                        # lease token so a takeover or a non-owner write
                        # is rejected at the persistence layer.
                        "owner_token": request.attempt_token or f"alloc-{generation}",
                        "revision": 0,
                        "terminal_failures": list((previous or {}).get("terminal_failures") or []),
                        "submitted_at": datetime.now(tz=timezone.utc).isoformat(),
                    }
                    _write_identity(identity_path, identity)
            phase = str(identity.get("phase") or "allocated")

        if phase == "allocated":
            upload_url = str(identity.get("upload_url") or "")
            if not upload_url.startswith(("http://", "https://")):
                # A persisted allocation without a usable slot cannot be
                # uploaded — mark it expired so the NEXT attempt reallocates
                # instead of polling a batch that never received bytes.
                identity = _apply_identity_transition(
                    identity_path,
                    identity_now=identity,
                    patch={
                        "phase": "slot_expired",
                        "slot_expired_reason": "persisted identity has no usable slot URL",
                    },
                    label="slot_expired_no_url",
                )
                raise UploadSlotExpiredError(
                    "stored upload slot is unusable; reallocation required",
                    details={"code": "UPLOAD_SLOT_EXPIRED", "batch_id": identity.get("batch_id")},
                )
            try:
                self.upload_to_slot(upload_url, local_file)
            except UploadSlotExpiredError:
                # Confirmed expiry: persist the state BEFORE raising; the
                # next attempt's dispatch reallocates. If that allocation
                # fails too, the identity STAYS slot_expired — never polling.
                identity = _apply_identity_transition(
                    identity_path,
                    identity_now=identity,
                    patch={
                        "phase": "slot_expired",
                        "slot_expired_reason": "PUT returned 401/403/404 (slot invalid)",
                    },
                    label="slot_expired_put",
                )
                raise
            except NetworkTransientError:
                # 500/429/network: keep phase=allocated → the retry re-PUTs
                # the SAME slot (no second batch allocation).
                identity = _apply_identity_transition(
                    identity_path,
                    identity_now=identity,
                    patch={
                        "last_upload_error": {
                            "code": "UPLOAD_RETRYABLE",
                            "at": datetime.now(tz=timezone.utc).isoformat(),
                        },
                    },
                    label="upload_transient",
                )
                raise
            identity = _apply_identity_transition(
                identity_path,
                identity_now=identity,
                patch={"phase": "uploaded"},
                label="uploaded",
            )
            phase = "uploaded"

        if phase not in ("uploaded", "polling", "completed"):
            # Unknown persisted state: never fall through to polling a
            # batch whose upload status is unknown — require reallocation.
            identity = _apply_identity_transition(
                identity_path,
                identity_now=identity,
                patch={
                    "phase": "slot_expired",
                    "slot_expired_reason": f"unknown persisted phase {phase!r}",
                },
                label="slot_expired_unknown_phase",
            )
            raise EngineProtocolUnsupportedError(
                f"MinerU identity has an unknown phase {phase!r}; reallocation required",
                details={"code": "REMOTE_IDENTITY_PHASE_UNKNOWN", "phase": phase},
            )

        identity = _apply_identity_transition(
            identity_path,
            identity_now=identity,
            # J01: record the polling attempt's owner for auditability.
            patch={"phase": "polling", "last_attempt_token": request.attempt_token},
            label="polling",
        )

        try:
            with self._client() as client:
                entry = self.poll_batch_entry(
                    client, str(identity["batch_id"]), file_name=str(identity["file_name"])
                )
                result_payload = self._fetch_zip_result(
                    client, str(entry["full_zip_url"]), str(identity["batch_id"])
                )
        except RemoteBatchFailedError as exc:
            # J01: persist the terminal failure (append-only history) so the
            # retry's dispatch allocates a NEW generation instead of polling
            # this dead batch forever. K04: fenced by (fingerprint,
            # generation, owner_token, revision); a stale write is rejected.
            failures = list(identity.get("terminal_failures") or [])
            failures.append(
                {
                    "batch_id": identity.get("batch_id"),
                    "generation": identity.get("generation"),
                    "state": (exc.details or {}).get("state") if isinstance(exc.details, dict) else None,
                    "reason": exc.message[:300],
                    "attempt_token": request.attempt_token,
                    "at": datetime.now(tz=timezone.utc).isoformat(),
                }
            )
            identity = _apply_identity_transition(
                identity_path,
                identity_now=identity,
                patch={"terminal_failures": failures, "phase": "terminal_failed"},
                label="terminal_failed",
            )
            raise

        identity = _apply_identity_transition(
            identity_path,
            identity_now=identity,
            patch={
                "phase": "completed",
                "completed_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            label="completed",
        )

        # Also keep a copy next to this attempt's artifacts for audithers.
        (request.output_dir / "remote_identity.json").write_text(
            json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        raw_path.write_text(
            json.dumps(
                {
                    **identity,
                    "final_state": "done",
                    "extract_progress": entry.get("extract_progress"),
                    "result": result_payload,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        return self._finalize_outputs(
            request=request,
            result_payload=result_payload,
            remote_id=str(identity["batch_id"]),
            raw_path=raw_path,
            normalized_path=normalized_path,
            manifest_path=manifest_path,
            source_identity_verified=True,
            extra_manifest={
                "remote_identity": identity,
                "protocol": "v4-batch",
                "remote_identity_path": str(
                    identity_path.relative_to(stable_dir.parent.parent)
                    if stable_dir.name == request.job_id
                    else identity_path
                ),
            },
        )

    def _finalize_outputs(
        self,
        *,
        request: ParseRequest,
        result_payload: dict[str, Any],
        remote_id: str,
        raw_path: Path,
        normalized_path: Path,
        manifest_path: Path,
        source_identity_verified: bool,
        extra_manifest: dict[str, Any] | None = None,
    ) -> ParseArtifacts:
        """Shared tail: project → normalize → filter pages → write manifest."""

        ordered_pages = sorted(int(p) for p in request.page_numbers)
        page_sizes = _source_page_geometry(request.source_path, ordered_pages)
        payload_basis = (
            result_payload.get("coord_basis") if isinstance(result_payload, dict) else None
        )
        projected = _project_mineru_payload(
            result_payload,
            page_numbers=set(request.page_numbers),
            page_order=ordered_pages or None,
            page_sizes=page_sizes,
            declared_basis=payload_basis,
        )
        projection_warnings = projected.pop("_projection_warnings", [])
        # G09: the EFFECTIVE basis is what governed the conversion — the
        # payload's own declaration, or the content_list protocol default.
        declared_basis = projected.pop("_coordinate_basis", None) or payload_basis
        document = normalize_docling_document(
            projected,
            parser_run_id=f"{request.job_id}:mineru",
            document_id=request.document_id,
        )

        # F07/P03: three separate geometry facts — never conflated.
        descriptor = _page_geometry_descriptor(
            document,
            page_sizes=page_sizes,
            declared_basis=declared_basis,
            projection_warnings=projection_warnings,
        )
        doc_errors = (
            list(document.errors) + list(projection_warnings) + list(descriptor["bbox_warnings"])
        )
        if descriptor["basis_notes"]:
            doc_errors.append(
                "mineru coordinate basis unverified: " + "; ".join(descriptor["basis_notes"])
            )
        if not descriptor["bounds_valid"]:
            doc_errors.append(
                "box bounds invalid: geometry bounds are not verified (raw boxes retained)"
            )
        document = document.model_copy(update={"errors": doc_errors})

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
        normalized_path.write_text(document.model_dump_json(), encoding="utf-8")

        manifest = self._manifest(
            request=request,
            document=document,
            task_id=remote_id,
            raw_path=raw_path,
            normalized_path=normalized_path,
            source_identity_verified=source_identity_verified,
        )
        # F07/P03: how the page geometry was obtained — source-PDF sizes when
        # available, and the three SEPARATE verification facts. A box that
        # merely fits the page does not verify the coordinate basis.
        manifest["page_geometry"] = {
            "basis": "source-pdf" if page_sizes else "unknown-defaults",
            "declared_coordinate_basis": declared_basis,
            "page_size_verified": descriptor["page_size_verified"],
            "coordinate_basis_verified": descriptor["coordinate_basis_verified"],
            "bounds_valid": descriptor["bounds_valid"],
            "raw_bbox_retained": True,
            "bbox_warnings": descriptor["bbox_warnings"][:50],
            "unverified_pages": descriptor["unverified_pages"],
            "basis_notes": descriptor["basis_notes"],
            "page_count": len(document.pages),
            "rotated_pages": sorted(
                page_no
                for page_no, (_w, _h, rotation) in (page_sizes or {}).items()
                if rotation
            ),
        }
        if extra_manifest:
            manifest.update(extra_manifest)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return ParseArtifacts(
            normalized_path=normalized_path,
            raw_response_path=raw_path,
            manifest_path=manifest_path,
        )



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


def _project_bbox(
    bbox: Any,
    *,
    declared_origin: str | None,
    page_no: int,
    item_ref: str,
    warnings: list[str],
    basis_unit: str | None = None,
    page_size: tuple[float, float] | None = None,
    rotation_ok: bool = True,
) -> dict[str, Any] | None:
    """Coerce a raw MinerU bbox into the Docling-shaped dict (F07/G09).

    - The RAW box always travels along under ``raw_bbox`` so nothing is
      lost when the frame is not trusted.
    - A non-finite box is DROPPED with an explicit warning (the raw payload
      keeps it) — never silently serialized or clamped.
    - ``coord_origin`` is claimed ONLY for a justified basis (declared, or
      the content_list protocol's normalised frame).
    - G09/P04: for the MinerU ``content_list`` protocol the official
      exporter normalises every coordinate to 0–1000 by page width/height
      (``_build_bbox``: ``pt * 1000 / page_size``); the inverse transform is
      applied here per axis with the SOURCE PDF page size — text, tables,
      figures and cells all share it. Pages whose real size is unknown, or
      rotated pages without a declared rotation_applied, keep RAW values and
      ``bbox_verified=False`` (page-level navigation only).
    """

    import math

    try:
        if isinstance(bbox, dict):
            values = [
                float(bbox.get("l", bbox.get("left", 0))),
                float(bbox.get("t", bbox.get("top", 0))),
                float(bbox.get("r", bbox.get("right", 0))),
                float(bbox.get("b", bbox.get("bottom", 0))),
            ]
        elif isinstance(bbox, (list, tuple)) and len(bbox) == 8:
            xs = [float(bbox[i]) for i in (0, 2, 4, 6)]
            ys = [float(bbox[i]) for i in (1, 3, 5, 7)]
            values = [min(xs), min(ys), max(xs), max(ys)]
        elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            values = [float(v) for v in bbox]
        else:
            return None
    except (TypeError, ValueError):
        return None

    if not all(math.isfinite(v) for v in values):
        warnings.append(
            f"page {page_no}: {item_ref} has a non-finite bbox {values!r} — "
            "box dropped from the normalized view, raw payload retained"
        )
        return None

    out: dict[str, Any] = {
        "l": values[0],
        "t": values[1],
        "r": values[2],
        "b": values[3],
        "raw_bbox": list(bbox) if isinstance(bbox, (list, tuple)) else dict(bbox),
    }
    origin = str(declared_origin or "").lower()
    if origin in ("top_left", "topleft"):
        out["coord_origin"] = "TOPLEFT"
    elif origin in ("bottom_left", "bottomleft"):
        out["coord_origin"] = "BOTTOMLEFT"

    if basis_unit == "normalized_1000":
        # The protocol frame is only claimed when the transform can actually
        # be applied: real page size AND an unrotated (or rotation-applied)
        # page. Values outside 0–1000 could not have come from this
        # exporter — they stay raw and unverified.
        in_range = all(-2.0 <= v <= 1002.0 for v in values)
        if page_size and page_size[0] > 0 and page_size[1] > 0 and rotation_ok and in_range:
            width, height = page_size
            converted = [
                values[0] / 1000.0 * width,
                values[1] / 1000.0 * height,
                values[2] / 1000.0 * width,
                values[3] / 1000.0 * height,
            ]
            out["l"], out["t"], out["r"], out["b"] = (
                round(converted[0], 2),
                round(converted[1], 2),
                round(converted[2], 2),
                round(converted[3], 2),
            )
            out["bbox_verified"] = True
        else:
            if not in_range:
                warnings.append(
                    f"page {page_no}: {item_ref} bbox {values!r} is outside the 0–1000 "
                    "content_list protocol range — basis unverified, raw retained"
                )
            out["bbox_verified"] = False
    elif origin:
        # A declared basis with an origin: raw values are already page-space
        # pt (declared and trusted by the payload author).
        out["bbox_verified"] = True

    return out


def _project_mineru_payload(
    payload: dict[str, Any],
    *,
    page_numbers: set[int],
    page_order: list[int] | None = None,
    page_sizes: dict[int, tuple[float, float, int]] | None = None,
    declared_basis: Any = None,
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
    We build that here, with one entry per page MinerU reported. A reserved
    ``_projection_warnings`` key carries bbox drops back to the caller.
    """

    spec = declared_basis if isinstance(declared_basis, dict) else None
    warnings: list[str] = []

    if "content_list" in payload and isinstance(payload["content_list"], list):
        # G09/P04: the content_list exporter normalises to 0–1000 by page
        # size (official pipeline `_build_bbox`). That IS the protocol basis
        # unless the payload explicitly declares a different one.
        if spec and spec.get("unit"):
            effective_basis = spec
        else:
            effective_basis = {
                "unit": "normalized_1000",
                "origin": "top_left",
                "protocol": "mineru-content-list-v1",
                "transform": "coord / 1000 * source_page_size",
            }
        projected = _project_content_list(
            payload["content_list"],
            page_order=page_order,
            page_sizes=page_sizes,
            declared_basis=effective_basis,
            warnings=warnings,
        )
        projected["_projection_warnings"] = warnings
        projected["_coordinate_basis"] = effective_basis
        return projected

    if "markdown" in payload and isinstance(payload["markdown"], str) and payload["markdown"].strip():
        # C10 (plan §5.4): markdown alone carries NO per-page structure.
        # Stuffing the whole document into one fabricated page would lie
        # about page identity, page counts and every downstream page
        # analysis — refuse instead, with a machine-readable code.
        raise EngineProtocolUnsupportedError(
            "MinerU result contains only markdown and no per-page structure "
            "(content_list/page payload missing)",
            details={"code": "MARKDOWN_ONLY_NO_PAGE_STRUCTURE"},
        )

    if "pages" in payload and isinstance(payload["pages"], list) and payload["pages"]:
        pages = payload["pages"]
        # G09/P05: a cached/alternative payload declares its basis, or its
        # boxes are untrusted — mark unverified frames explicitly so
        # evidence degrades to page level instead of pretending precision.
        if spec and spec.get("unit"):
            marked = pages
        else:
            marked = []
            for page in pages:
                if not isinstance(page, dict):
                    marked.append(page)
                    continue
                page_copy = dict(page)
                for key in ("texts", "tables", "figures"):
                    items = page_copy.get(key)
                    if isinstance(items, list):
                        page_copy[key] = [
                            {**item, "bbox_verified": item.get("bbox_verified", False)}
                            if isinstance(item, dict)
                            else item
                            for item in items
                        ]
                marked.append(page_copy)
        return {"pages": marked, "_projection_warnings": warnings}

    # Last resort: empty pages list. The normaliser will raise a clear error.
    return {"pages": [], "_projection_warnings": warnings}


_PAGE_HEADER_RE = re.compile(r"^\s*<!--\s*page\s+(\d+)\s*-->", re.IGNORECASE)

#: MinerU block types that carry an HTML ``table_body``.
_TABLE_TYPES = {"table", "table_body"}

#: MinerU block types that are VISUAL regions. Their text lives in
#: ``image_caption`` / ``chart_caption`` and they carry a bbox — dropping
#: them loses every figure (and every FIGURE n caption) from the
#: normalized view.
_FIGURE_TYPES = {"image", "chart"}


def _project_figure_item(
    item: dict[str, Any],
    page_no: int,
    seq: int,
    *,
    declared_origin: str | None,
    warnings: list[str],
    basis_unit: str | None,
    page_size: tuple[float, float] | None,
    rotation_ok: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project one MinerU ``image`` / ``chart`` item into a Docling-shaped
    ``pictures`` entry, plus its caption text.

    MinerU keeps a visual region's title in ``image_caption`` /
    ``chart_caption`` (a list of strings; sub-figure labels such as ``"A"``
    share the field with the real ``FIGURE n | ...`` caption). The old
    projection dropped these items entirely because their ``content`` is
    empty, which removed every figure AND every figure caption from the
    normalized view.

    Returns ``(picture_dict, caption_text)``. ``picture_dict`` is ``None``
    when the region has no usable bbox (it cannot be cropped, so it is not
    exportable). ``caption_text`` is the joined caption regardless of
    whether a bbox was resolvable, so the caller can keep the title in the
    text layer even for an un-croppable region.
    """

    caption_list = (
        item.get("image_caption")
        or item.get("chart_caption")
        or item.get("table_caption")
        or []
    )
    if isinstance(caption_list, str):
        caption_parts = [caption_list]
    else:
        caption_parts = [str(c) for c in caption_list if str(c).strip()]
    caption_text = " ".join(caption_parts).strip() or None

    bbox = item.get("bbox") or item.get("position")
    if bbox is None:
        return None, caption_text
    bbox_out = _project_bbox(
        bbox,
        declared_origin=declared_origin,
        page_no=page_no,
        item_ref=f"figure {seq}",
        warnings=warnings,
        basis_unit=basis_unit,
        page_size=page_size,
        rotation_ok=rotation_ok,
    )
    if bbox_out is None:
        return None, caption_text

    label = "chart" if str(item.get("type") or "").lower() == "chart" else "picture"
    picture: dict[str, Any] = {
        "self_ref": f"mineru:figure:{page_no}:{seq}",
        "label": label,
        "prov": [{"page_no": page_no, "bbox": bbox_out}],
        # Docling's normalize reads ``captions`` as refs into the page's
        # text items; the caller appends the caption text item and passes
        # its self_ref back through ``captions`` below.
        "captions": [],
    }
    if bbox_out.get("bbox_verified") is not None:
        picture["bbox_verified"] = bbox_out.get("bbox_verified")
    return picture, caption_text


def _project_table_item(
    item: dict[str, Any],
    page_no: int,
    seq: int,
    *,
    declared_origin: str | None = None,
    warnings: list[str] | None = None,
    basis_unit: str | None = None,
    page_size: tuple[float, float] | None = None,
    rotation_ok: bool = True,
) -> dict[str, Any] | None:
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
    bbox_out: dict[str, Any] | None = (
        _project_bbox(
            bbox,
            declared_origin=declared_origin,
            page_no=page_no,
            item_ref=f"table {seq}",
            warnings=warnings if warnings is not None else [],
            basis_unit=basis_unit,
            page_size=page_size,
            rotation_ok=rotation_ok,
        )
        if bbox is not None
        else None
    )

    cells: list[dict[str, Any]] = []
    row_idx = -1
    # Occupancy grid: a rowspan/colspan cell occupies the positions of the
    # rows below it, so every row must place cells in the first free column
    # (plan T05). Without this, merged headers shift columns.
    occupied: set[tuple[int, int]] = set()
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

            rowspan = _span("rowspan")
            colspan = _span("colspan")
            # Skip columns already consumed by a previous row's rowspan.
            while (row_idx, col_idx) in occupied:
                col_idx += 1
            for r in range(row_idx, row_idx + rowspan):
                for c in range(col_idx, col_idx + colspan):
                    occupied.add((r, c))

            is_th = cell_m.group(1).lower() == "th"
            cells.append(
                {
                    "start_row_offset_idx": row_idx,
                    "start_col_offset_idx": col_idx,
                    "row_span": rowspan,
                    "col_span": colspan,
                    "is_header": is_th,
                    # G11/P07: the normaliser derives roles from the
                    # column_header/row_header flags — a th in the first row
                    # is a column header, a th in the first column is a row
                    # header (a (0,0) th is both).
                    "column_header": is_th and row_idx == 0,
                    "row_header": is_th and col_idx == 0,
                    "text": inner,
                }
            )
            col_idx += colspan

    # Empty placeholder cells (e.g. the void under a merged header) carry no
    # text and the contract requires text_raw to be non-empty; skip them.
    # Grid positions of the remaining cells are unaffected because they are
    # indexed by explicit row/col offsets.
    cells = [c for c in cells if c["text"]]

    if not cells:
        return None

    structure_issues: list[str] = []
    if not any(c.get("is_header") for c in cells):
        # G11/P07: td-only tables (real MinerU output) — infer the axes
        # from POSITION (first row = column headers, first column = row
        # headers) with an explicit note; without this every S/R/I cell had
        # an unknown role and susceptibility matrices were silently dropped
        # as inventories.
        for cell in cells:
            if cell["start_row_offset_idx"] == 0:
                cell["column_header"] = True
            if cell["start_col_offset_idx"] == 0:
                cell["row_header"] = True
        structure_issues.append(
            "HEADER_ROLES_INFERRED: td-only table — first row/column used as axes"
        )

    row_count = max(c["start_row_offset_idx"] + c.get("row_span", 1) for c in cells)
    col_count = max(c["start_col_offset_idx"] + c.get("col_span", 1) for c in cells)
    # G11/P07: structural sanity. The malformed real response projected a
    # 12x392 table with 4588 cells — that must be VISIBLE (issue + no
    # silent activity generation), not extracted as a healthy matrix.
    if row_count > 150 or col_count > 120 or len(cells) > 4000:
        structure_issues.append(
            f"GRID_SIZE_SUSPECT: {row_count}x{col_count} grid with {len(cells)} cells "
            "(possible colspan/rowspan mis-parse — region re-parse or human check required)"
        )
    # H04/P07-adjacent: an S/R/I VALUE inside the header row means the
    # drug/strain axes of this grid are semantically mangled (real rotated
    # TABLE 3 case) — pairing values under an "R" header would fabricate
    # relations, so this is a visible structural refusal.
    header_value_cells = [
        c
        for c in cells
        if c["start_row_offset_idx"] == 0
        and str(c.get("text") or "").strip() in ("S", "R", "I", "SDD")
    ]
    if header_value_cells:
        structure_issues.append(
            f"HEADER_SEMANTICS_BROKEN: {len(header_value_cells)} S/R/I value(s) inside the "
            "header row — drug/strain axes cannot be trusted "
            "(no activity extraction; region re-parse or human check)"
        )

    # H04/P01: raggedness is a property of ROWS, not cells. The old check
    # collected each CELL's end column — a perfectly regular 2x6 rectangle
    # produced ends {1..6} and was flagged severely distorted (killing its
    # legitimate activities). Effective width per row = the farthest end
    # column INCLUDING span coverage rows (a rowspan cell occupies the rows
    # below it, so those rows count as covered, not short).
    row_widths: dict[int, int] = {}
    for c in cells:
        end_col = c["start_col_offset_idx"] + c.get("col_span", 1)
        for covered_row in range(
            c["start_row_offset_idx"], c["start_row_offset_idx"] + c.get("row_span", 1)
        ):
            row_widths[covered_row] = max(row_widths.get(covered_row, 0), end_col)
    distinct_widths = sorted({row_widths[r] for r in sorted(row_widths)})
    if len(distinct_widths) > 1:
        spread = distinct_widths[-1] - distinct_widths[0]
        if spread > 3:
            # A severe width spread means the HTML grid is distorted
            # (merge/split mis-parse, rotated continuations) — activities
            # from it would be fabricated pairings, so extraction is
            # refused for this table and the issue invites a re-parse.
            structure_issues.append(
                f"RAGGED_ROWS_SEVERE: row widths range {distinct_widths[0]}..{distinct_widths[-1]} "
                "(structure distorted — no activity extraction; region re-parse or human check)"
            )
        elif spread > 1:
            # A one/two-column difference is usually a header/caption row,
            # NOT evidence of distortion — warn but keep extracting.
            structure_issues.append(
                f"RAGGED_ROWS: row widths range {distinct_widths[0]}..{distinct_widths[-1]} "
                "(minor row-width variation)"
            )

    table: dict[str, Any] = {
        "self_ref": f"mineru:table:{page_no}:{seq}",
        "prov": [{"page_no": page_no}],
        "data": {"table_cells": cells},
        "structure_issues": structure_issues,
    }
    if bbox_out is not None:
        # The origin comes from the (declared or protocol) basis — never
        # hard-claimed here.
        table["prov"][0]["bbox"] = dict(bbox_out)
        if "coord_origin" not in table["prov"][0]["bbox"]:
            table["prov"][0]["bbox"]["coord_origin"] = "TOPLEFT"
        table["bbox_verified"] = bbox_out.get("bbox_verified")

    # Keep the raw HTML available as evidence (attribute named like Docling
    # captions so downstream consumers can display it).
    caption = item.get("table_caption") or item.get("img_caption")
    if isinstance(caption, list):
        caption = " ".join(str(c) for c in caption)
    if caption:
        table["caption"] = str(caption)
    return table


def _project_content_list(
    items: list[dict[str, Any]],
    *,
    page_order: list[int] | None = None,
    page_sizes: dict[int, tuple[float, float, int]] | None = None,
    declared_basis: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a Docling-shaped pages dict from a MinerU ``content_list`` (D07).

    - ``page_idx`` is 0-based in the SUBMITTED page set: with a range like
      ``30-33,40`` it maps to ``page_order[idx]``. Labelling idx+1 directly
      (the old behaviour) put MinerU's own numbering onto the document —
      wrong for any non-contiguous request.
    - Page size comes from the SOURCE PDF (``page_sizes``), not a fixed
      612x792; each page's rotation is carried along.
    - F07/G09: boxes go through :func:`_project_bbox` — the declared basis
      (default: the content_list protocol's 0–1000 normalised frame)
      decides whether a justified conversion applies; raw boxes are
      retained and unverified frames flagged ``bbox_verified=False``.
    """

    warnings = warnings if warnings is not None else []
    spec = declared_basis if isinstance(declared_basis, dict) else None
    basis_unit = str(spec.get("unit") or "") if spec else None
    declared_origin = str(spec.get("origin") or "") if spec else None
    rotation_applied = bool(spec.get("rotation_applied")) if spec else False

    pages: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        page_idx = item.get("page_idx")
        try:
            idx = int(page_idx) if page_idx is not None else 0
        except (TypeError, ValueError):
            idx = 0
        if page_order:
            # An index outside the submitted set cannot be mapped back to a
            # real document page; keep it on a clearly-marked synthetic
            # number so the mismatch is visible, never a guessed real page.
            page_no = page_order[idx] if 0 <= idx < len(page_order) else (10_000 + idx)
        else:
            page_no = idx + 1

        real_size = (page_sizes or {}).get(page_no)
        width, height, rotation = real_size or (612.0, 792.0, 0)
        # G09: conversion needs a REAL page size; a rotated page is only
        # convertible when the payload says the rotation was applied.
        projection_size = (width, height) if real_size else None
        rotation_ok = (rotation % 360 == 0) or rotation_applied
        page = pages.setdefault(
            page_no,
            {
                "page": page_no,
                "size": {"width": width, "height": height},
                "width": width,
                "height": height,
                "rotation": rotation,
                "texts": [],
                "tables": [],
                "pictures": [],
            },
        )

        item_type = str(item.get("type") or item.get("label") or "text").lower()

        # Tables: the content lives in ``table_body`` HTML; projecting only
        # ``text``/``content`` would silently drop every table.
        if item_type in _TABLE_TYPES and (item.get("table_body") or ""):
            table_payload = _project_table_item(
                item, page_no, len(page["tables"]),
                declared_origin=declared_origin,
                warnings=warnings,
                basis_unit=basis_unit,
                page_size=projection_size,
                rotation_ok=rotation_ok,
            )
            if table_payload is not None:
                page["tables"].append(table_payload)
                continue

        # Visual regions: ``image`` / ``chart`` items carry their title in
        # ``image_caption`` / ``chart_caption`` and a bbox, but no text —
        # skipping them (the ``if not text`` branch below) removed every
        # figure and every FIGURE n caption from the normalized view.
        if item_type in _FIGURE_TYPES:
            picture, caption_text = _project_figure_item(
                item,
                page_no,
                len(page["pictures"]),
                declared_origin=declared_origin,
                warnings=warnings,
                basis_unit=basis_unit,
                page_size=projection_size,
                rotation_ok=rotation_ok,
            )
            if caption_text:
                # Keep the caption in the text layer at the region's own
                # reading position. NO synthetic fine-grained bbox: the
                # payload gives one box for the whole region, so the text
                # block stays page-level (bbox=None) and a consumer never
                # claims character/cell precision it cannot prove.
                caption_ref = f"mineru:p{page_no}:cap{len(page['texts'])}"
                page["texts"].append(
                    {
                        "self_ref": caption_ref,
                        "label": "caption",
                        "bbox": None,
                        "bbox_verified": False,
                        "text": str(caption_text),
                    }
                )
                if picture is not None:
                    picture["captions"] = [{"$ref": caption_ref}]
            if picture is not None:
                page["pictures"].append(picture)
            continue

        text = item.get("text") or item.get("content") or ""
        if not text:
            continue

        bbox = item.get("bbox") or item.get("position")
        bbox_out = (
            _project_bbox(
                bbox,
                declared_origin=declared_origin,
                page_no=page_no,
                item_ref=f"text {len(page['texts'])}",
                warnings=warnings,
                basis_unit=basis_unit,
                page_size=projection_size,
                rotation_ok=rotation_ok,
            )
            if bbox is not None
            else None
        )

        label = item.get("type") or item.get("label") or "text"
        # MinerU merges paragraph-style pages (the Chinese patent layout)
        # into ONE giant block whose lines are prefixed with the patent's
        # paragraph numbers ("[0009] <210> 1 [0010] <211> 100 ..."). The
        # ST.25 parser requires one tag per line starting at the line
        # head, so the merged block yielded ZERO declarations while the
        # same payload split by paragraph number yields 4 complete
        # sequences. Split here, at the projection boundary, so the
        # normalized view keeps one logical line per tag.
        for piece in _split_paragraph_numbered(str(text)):
            page["texts"].append(
                {
                    "self_ref": f"mineru:p{page_no}:{len(page['texts'])}",
                    "label": label,
                    "bbox": bbox_out,
                    "bbox_verified": (bbox_out or {}).get("bbox_verified"),
                    "text": piece,
                }
            )

    return {"pages": [pages[k] for k in sorted(pages.keys())]}


#: Chinese patents number every paragraph ("[0009]"); a merged MinerU block
#: carries dozens of them. Two or more paragraphs means the block spans
#: multiple logical lines.
_PARAGRAPH_MARK_RE = re.compile(r"\[\d{4}\]")
_PARAGRAPH_PREFIX_RE = re.compile(r"^\[\d{4}\]\s*")


def _split_paragraph_numbered(text: str) -> list[str]:
    """Split a merged block at patent paragraph numbers and strip the marks.

    Only applies when the block carries TWO OR MORE paragraph marks — a
    normal sentence that happens to contain one bracket group is left
    untouched. Every returned piece keeps the ORIGINAL block's bbox (the
    payload gives one box for the whole block), so position precision
    stays block-level rather than fabricated per line.
    """

    if len(_PARAGRAPH_MARK_RE.findall(text)) < 2:
        stripped = _PARAGRAPH_PREFIX_RE.sub("", text).strip()
        return [stripped] if stripped else []
    # ``split`` keeps the text BEFORE the first mark as element 0 — it must
    # survive too (a block may start with prose, not with "[0001]").
    parts = _PARAGRAPH_MARK_RE.split(text)
    out: list[str] = []
    head = parts[0].strip()
    if head:
        out.append(head)
    for piece in parts[1:]:
        cleaned = piece.strip()
        if cleaned:
            out.append(cleaned)
    return out


__all__ = ["MinerUAdapter", "DEFAULT_BASE_URL", "DEFAULT_MODEL_VERSION"]