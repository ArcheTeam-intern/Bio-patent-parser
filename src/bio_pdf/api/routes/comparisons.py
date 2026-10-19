"""Comparison experiment routes (C11/D07).

Experiments are separate from jobs: no review items, no publications. The
API returns DB-backed status the UI polls; a side's failure is data, not a
request failure.

K02 additions:

- ``create`` and ``retry`` run an ENGINE pre-check BEFORE any side is
  claimed, persisted, or any remote API is contacted. The pre-check is
  derived from the resolved :class:`RuntimeConfig` (K01); a missing
  MinerU token returns ``422 ENGINE_NOT_CONFIGURED`` with the same
  structured ``unavailable_reason`` the capability endpoint exposed.
  A pre-check failure creates zero new attempts and zero remote calls.
- WorkflowError → HTTP status mapping is no longer a blanket 404. Only
  ``NOT_FOUND`` becomes 404; ``ENGINE_NOT_CONFIGURED`` and other config
  / parameter / runtime errors keep their semantic HTTP code.
- The side's persisted ``error_info`` JSON carries the structured
  failure (engine/code/stage/retryable/message/attempt + batch_id and
  generation when remote identity exists). The legacy string ``error``
  column is kept as the human-readable fallback for old rows.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bio_pdf.api.dependencies import ServiceDep
from bio_pdf.application.comparison_service import ComparisonService, DEFAULT_ENGINES
from bio_pdf.errors import WorkflowError
from bio_pdf.runtime_config import get_runtime_config

router = APIRouter(prefix="/comparisons", tags=["comparisons"])

_ENGINE_CHOICES = ("docling", "mineru_http")
#: K02: HTTP status the workflow-error code should map to. Everything
#: else (parameter, config, internal) keeps a sensible default that the
#: caller can act on without consulting a status-code table.
_ERROR_STATUS_MAP = {
    "NOT_FOUND": 404,
    "ENGINE_NOT_CONFIGURED": 422,
    "UNKNOWN_ENGINE": 422,
    "NEED_TWO_ENGINES": 422,
    "COMPARISON_NOT_READY": 409,
    "SOURCE_MISSING": 409,
    "EXECUTION_BUDGET_EXCEEDED": 409,
    "BASELINE_CONFLICT": 409,
}


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


def _check_engines_configured(engines: list[str]) -> None:
    """K02: pre-check the requested engines against the resolved runtime
    config; raises ``HTTPException(422)`` with the structured
    ``unavailable_reason`` if any engine is not configured.

    The check is performed before ``ComparisonService.create()`` writes
    anything to the database, so a failure path creates zero new
    attempts and zero remote calls.
    """

    runtime = get_runtime_config()
    for engine in engines:
        if engine in ("docling", "docling_sdk"):
            continue
        if engine in ("mineru", "mineru_http"):
            if not runtime.mineru_configured():
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "ENGINE_NOT_CONFIGURED",
                        "stage": "configure",
                        "retryable": False,
                        "message": (
                            "MinerU 侧未配置：MINERU_TOKEN 在进程环境或 --env-file "
                            "中均不存在；该次请求在创建前被拒绝，未发起任何云端调用"
                        ),
                        "details": {
                            "engine": engine,
                            "missing": ["MINERU_TOKEN"],
                            "side_engines": list(engines),
                        },
                    },
                )
            continue
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNKNOWN_ENGINE",
                "message": f"engine {engine!r} is not comparable here",
                "details": {"choices": list(_ENGINE_CHOICES)},
            },
        )


def _http_status_for_workflow_error(exc: WorkflowError) -> int:
    detail = exc.details if isinstance(exc.details, dict) else {}
    code = str(detail.get("code") or exc.code)
    return _ERROR_STATUS_MAP.get(code, 400)


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
    # K02: pre-check BEFORE any DB write or thread start. A mineru
    # request without a configured token gets a clean 422 — zero new
    # attempts, zero remote calls.
    _check_engines_configured(list(request.engines))
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
    try:
        comparison_id = comparison.create(
            document_id=request.document_id,
            source_path=source_path,
            pages=sorted(set(int(p) for p in request.pages if int(p) > 0)),
            engines=list(request.engines),
        )
    except WorkflowError as exc:
        raise HTTPException(
            status_code=_http_status_for_workflow_error(exc),
            detail={
                "code": str((exc.details or {}).get("code") or exc.code),
                "message": exc.message,
                "details": exc.details or {},
            },
        ) from exc
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
        raise HTTPException(
            status_code=_http_status_for_workflow_error(exc),
            detail={
                "code": str((exc.details or {}).get("code") or exc.code),
                "message": exc.message,
                "details": exc.details or {},
            },
        ) from exc


@router.get("/{comparison_id}/diff")
def get_diff(service: ServiceDep, comparison_id: str) -> dict:
    try:
        return {"api_version": "v1", **_service(service).diff(comparison_id)}
    except WorkflowError as exc:
        raise HTTPException(
            status_code=_http_status_for_workflow_error(exc),
            detail={
                "code": str((exc.details or {}).get("code") or exc.code),
                "message": exc.message,
                "details": exc.details or {},
            },
        ) from exc


@router.post("/{comparison_id}/retry")
def retry_comparison(service: ServiceDep, comparison_id: str) -> dict:
    """Re-arm every failed side and let the claim pass run again.

    K02: a retry that would push a not-configured engine is rejected
    with 422 BEFORE any attempt is incremented or any claim is opened.
    The check is based on the CURRENT runtime config — if MinerU was
    unconfigured at create time but is configured now, retry succeeds
    and the failed side re-arms normally. The Docling side is left
    alone (its success status is unchanged).
    """

    comparison = _service(service)
    try:
        info = comparison.get(comparison_id)
    except WorkflowError as exc:
        raise HTTPException(
            status_code=_http_status_for_workflow_error(exc),
            detail={
                "code": str((exc.details or {}).get("code") or exc.code),
                "message": exc.message,
                "details": exc.details or {},
            },
        ) from exc
    failed_engines = [s["side"] for s in info.get("sides", []) if s["status"] == "failed"]
    _check_engines_configured(failed_engines or list(DEFAULT_ENGINES))
    try:
        return comparison.retry(comparison_id)
    except WorkflowError as exc:
        raise HTTPException(
            status_code=_http_status_for_workflow_error(exc),
            detail={
                "code": str((exc.details or {}).get("code") or exc.code),
                "message": exc.message,
                "details": exc.details or {},
            },
        ) from exc


__all__ = ["router"]