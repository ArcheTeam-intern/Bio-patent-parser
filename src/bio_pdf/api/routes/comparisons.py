"""Comparison experiment routes (C11/D07).

Experiments are separate from jobs: no review items, no publications. The
API returns DB-backed status the UI polls; a side's failure is data, not a
request failure.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bio_pdf.api.dependencies import ServiceDep
from bio_pdf.application.comparison_service import ComparisonService, DEFAULT_ENGINES
from bio_pdf.errors import WorkflowError

router = APIRouter(prefix="/comparisons", tags=["comparisons"])

_ENGINE_CHOICES = ("docling", "mineru_http")


class ComparisonCreateRequest(BaseModel):
    document_id: str
    pages: list[int] = Field(default_factory=list)
    engines: list[str] = Field(default_factory=lambda: list(DEFAULT_ENGINES))


def _service(service: ServiceDep) -> ComparisonService:
    # ComparisonService opens its own Repository (connection lifetime =
    # service lifetime) so the daemon thread outliving the FastAPI request
    # can still write statuses — otherwise the per-request Repository is
    # closed by the dep teardown and every thread write fails.
    return ComparisonService.from_service(service)


@router.post("", status_code=202)
def create_comparison(service: ServiceDep, request: ComparisonCreateRequest) -> dict:
    for engine in request.engines:
        if engine not in _ENGINE_CHOICES:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UNKNOWN_ENGINE",
                    "message": f"engine {engine!r} is not comparable here",
                    "details": {"choices": list(_ENGINE_CHOICES)},
                },
            )
    if len(set(request.engines)) < 2:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NEED_TWO_ENGINES",
                "message": "a comparison needs two distinct engines",
                "details": {"engines": request.engines},
            },
        )
    row = service.repository.connection.execute(
        "SELECT source_path FROM documents WHERE document_id = ?",
        (request.document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "document not registered"},
        )
    source_path = Path(str(row["source_path"]))
    if not source_path.is_file():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_MISSING",
                "message": "document source file is no longer available",
                "details": {"source_path": str(source_path)},
            },
        )

    comparison = _service(service)
    comparison_id = comparison.create(
        document_id=request.document_id,
        source_path=source_path,
        pages=sorted(set(int(p) for p in request.pages if int(p) > 0)),
        engines=list(request.engines),
    )
    comparison.start(comparison_id)
    return comparison.get(comparison_id)


@router.get("")
def list_comparisons(service: ServiceDep, limit: int = 50) -> dict:
    return {"api_version": "v1", "comparisons": _service(service).list(limit=limit)}


@router.get("/{comparison_id}")
def get_comparison(service: ServiceDep, comparison_id: str) -> dict:
    try:
        return _service(service).get(comparison_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/{comparison_id}/diff")
def get_diff(service: ServiceDep, comparison_id: str) -> dict:
    try:
        return {"api_version": "v1", **_service(service).diff(comparison_id)}
    except WorkflowError as exc:
        detail = exc.details
        # WorkflowError's .code is the class code; per-instance overrides
        # live in details["code"]. Honor both.
        detail_code = exc.code
        if isinstance(detail, dict) and detail.get("code"):
            detail_code = str(detail["code"])
        status = 409 if detail_code == "COMPARISON_NOT_READY" else 404
        raise HTTPException(
            status_code=status,
            detail={"code": detail_code, "message": exc.message, "details": detail},
        ) from exc


@router.post("/{comparison_id}/retry")
def retry_comparison(service: ServiceDep, comparison_id: str) -> dict:
    try:
        return _service(service).retry(comparison_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": exc.message}) from exc


__all__ = ["router"]