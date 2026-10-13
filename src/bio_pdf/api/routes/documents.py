"""Document routes: upload, sample import, metadata and source bytes."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from bio_pdf.api.contracts import DocumentResponse, SampleInfo, SamplesResponse
from bio_pdf.api.dependencies import ServiceDep, SettingsDep, workflow_error_to_http
from bio_pdf.contracts import DocumentInspection
from bio_pdf.errors import InputNotPdfError, WorkflowError
from bio_pdf.ingest.inspector import inspect_pdf

router = APIRouter(prefix="/documents", tags=["documents"])

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _source_store(data_dir: Path) -> Path:
    store = data_dir / "sources"
    store.mkdir(parents=True, exist_ok=True)
    return store


def _inspect_or_409(target: Path) -> DocumentInspection:
    try:
        return inspect_pdf(target)
    except InputNotPdfError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=415,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    except WorkflowError as exc:
        target.unlink(missing_ok=True)
        raise workflow_error_to_http(exc, status_code=422) from exc


@router.post("", response_model=DocumentResponse, status_code=201)
async def upload_document(
    service: ServiceDep,
    settings: SettingsDep,
    file: UploadFile,
) -> DocumentResponse:
    """Accept one PDF upload; reject non-PDF bytes with a specific reason."""

    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "RESOURCE_EXHAUSTED",
                    "message": f"upload exceeds limit of {settings.max_upload_bytes} bytes",
                    "details": {"bytes": size},
                },
            )
        digest.update(chunk)
        chunks.append(chunk)

    sha = digest.hexdigest()
    target = _source_store(settings.data_dir) / f"{sha}.pdf"
    if not target.exists():
        target.write_bytes(b"".join(chunks))

    inspection = _inspect_or_409(target)
    service.repository.upsert_document(
        document_id=inspection.document_id,
        sha256=inspection.sha256,
        source_path=str(target),
        bytes_count=inspection.bytes,
        metadata={"file_name": file.filename or target.name},
    )
    return _document_response(inspection, file.filename or target.name)


@router.post("/import-sample", response_model=DocumentResponse, status_code=201)
def import_sample(service: ServiceDep, settings: SettingsDep, sample_id: str) -> DocumentResponse:
    """Register a backend-controlled sample (never an arbitrary path)."""

    sample = _find_sample(settings, sample_id)
    if sample is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SAMPLE_NOT_FOUND", "message": f"unknown sample_id: {sample_id}"},
        )
    inspection = _inspect_or_409(sample.path)
    service.repository.upsert_document(
        document_id=inspection.document_id,
        sha256=inspection.sha256,
        source_path=str(sample.path),
        bytes_count=inspection.bytes,
        metadata={"file_name": sample.file_name, "category": sample.category},
    )
    return _document_response(inspection, sample.file_name)


@router.get("", response_model=SamplesResponse)
def list_samples(settings: SettingsDep) -> SamplesResponse:
    """List backend-controlled samples by category (missing → unavailable)."""

    samples: list[SampleInfo] = []
    for category in ("paper", "patent", "scan"):
        category_dir = settings.samples_dir / category
        found = sorted(category_dir.glob("*.pdf")) if category_dir.is_dir() else []
        if not found:
            samples.append(
                SampleInfo(
                    sample_id=f"{category}:none",
                    file_name="(样本待补充)",
                    category=category,  # type: ignore[arg-type]
                    bytes=0,
                    available=False,
                )
            )
            continue
        for path in found:
            samples.append(
                SampleInfo(
                    sample_id=f"{category}:{path.stem}",
                    file_name=path.name,
                    category=category,  # type: ignore[arg-type]
                    bytes=path.stat().st_size,
                    available=True,
                )
            )
    return SamplesResponse(samples=samples)


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(service: ServiceDep, document_id: str) -> DocumentResponse:
    row = service.repository.connection.execute(
        "SELECT * FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "document not found"})
    path = Path(row["source_path"])
    inspection = _inspect_or_409(path)
    import json as _json

    meta = _json.loads(row["metadata_json"] or "{}")
    return _document_response(inspection, str(meta.get("file_name") or path.name))


@router.get("/{document_id}/source")
def get_document_source(
    service: ServiceDep,
    document_id: str,
    request: Request,
    range_header: str | None = None,
) -> Response:
    """Serve the original PDF with SHA-256 ETag and single-range support."""

    row = service.repository.connection.execute(
        "SELECT sha256, source_path FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "document not found"})
    path = Path(row["source_path"])
    if not path.is_file():
        raise HTTPException(
            status_code=410,
            detail={"code": "ARTIFACT_MISSING", "message": "source file missing on disk"},
        )

    etag = f'"{row["sha256"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    data = path.read_bytes()
    total = len(data)
    range_header = range_header or request.headers.get("range")
    if range_header and (m := _RANGE_RE.match(range_header)):
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "" and end_s == "":
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        start = int(start_s) if start_s else max(0, total - int(end_s))
        end = int(end_s) if end_s else total - 1
        end = min(end, total - 1)
        if start > end or start >= total:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        body = data[start : end + 1]
        return Response(
            content=body,
            status_code=206,
            media_type="application/pdf",
            headers={
                "ETag": etag,
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
            },
        )

    return Response(
        content=data,
        media_type="application/pdf",
        headers={"ETag": etag, "Accept-Ranges": "bytes"},
    )


class _Sample:
    def __init__(self, path: Path, sample_id: str, category: str) -> None:
        self.path = path
        self.sample_id = sample_id
        self.category = category

    @property
    def file_name(self) -> str:
        return self.path.name


def _find_sample(settings, sample_id: str) -> _Sample | None:
    if ":" not in sample_id:
        return None
    category, _, stem = sample_id.partition(":")
    if category not in ("paper", "patent", "scan") or stem == "none":
        return None
    # Path components are validated: no separators can escape the category dir.
    if "/" in stem or "\\" in stem or ".." in stem:
        return None
    path = settings.samples_dir / category / f"{stem}.pdf"
    if not path.is_file():
        return None
    return _Sample(path, sample_id, category)


def _document_response(inspection: DocumentInspection, file_name: str) -> DocumentResponse:
    return DocumentResponse(
        document_id=inspection.document_id,
        sha256=inspection.sha256,
        bytes=inspection.bytes,
        page_count=inspection.page_count,
        file_name=file_name,
        is_encrypted=inspection.is_encrypted,
        pages=[json_page(p) for p in inspection.pages],
    )


def json_page(page) -> dict:
    return {
        "page_number": page.page_number,
        "width_pt": page.width_pt,
        "height_pt": page.height_pt,
        "rotation_deg": page.rotation_deg,
    }


__all__ = ["router"]
