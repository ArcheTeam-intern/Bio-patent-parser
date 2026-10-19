"""HTTP API contracts (Pydantic request/response models).

The API layer has its own versioned envelope so the frontend can rely on a
stable surface independent of internal contract evolution. Domain records
(embedded in results) reuse the strict :mod:`bio_pdf.contracts` models —
there is exactly one definition of every domain enum.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from bio_pdf.contracts import CheckResult, Measurement, SequenceRecord

API_VERSION = "v1"


class ApiEnvelope(BaseModel):
    """Every success response carries the API version."""

    api_version: str = API_VERSION


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(ApiEnvelope):
    error: ErrorBody


class EngineUnavailableReason(BaseModel):
    """K02: structured explanation of why an engine is not runnable here.

    The previous surface only returned ``available: bool`` — callers had
    no actionable difference between "engine does not exist", "engine
    not configured" and "engine failed the last call". The reason keeps
    its contract small: a stable ``code`` (one of ENGINE_NOT_CONFIGURED,
    ENGINE_UNAVAILABLE, ENGINE_PROTOCOL_UNSUPPORTED, ENGINE_TIMEOUT,
    REMOTE_BATCH_FAILED, NETWORK_TRANSIENT, UPLOAD_SLOT_EXPIRED, …),
    a ``stage`` (configure / allocate / upload / poll / download /
    normalize / domain / unknown), a ``missing`` list of variables, and
    a human ``message``.
    """

    code: str
    stage: Literal[
        "configure", "allocate", "upload", "poll",
        "download", "normalize", "domain", "unknown",
    ] = "unknown"
    retryable: bool = False
    message: str
    missing: list[str] = Field(default_factory=list)
    #: When the failure has an associated remote identity, carry the
    #: batch / generation / attempt so the UI can show which cloud slot
    #: the failure belongs to. Never the token.
    batch_id: str | None = None
    generation: int | None = None
    attempt: int | None = None


class EngineCapability(BaseModel):
    name: str
    available: bool
    supports_tables: bool
    supports_ocr: bool
    supports_page_range: bool
    sends_bytes_offsite: bool
    ocr_available: bool = False
    ocr_engines: dict[str, bool] = Field(default_factory=dict)
    #: K02: alternative engine names callers may use. ``mineru`` is kept
    #: as an alias for ``mineru_http`` so existing UI strings do not
    #: silently mis-target the wrong adapter.
    aliases: list[str] = Field(default_factory=list)
    #: K02: lifecycle slots this engine can fill.
    usage: list[Literal["comparison", "primary", "adopt"]] = Field(
        default_factory=lambda: ["primary"]
    )
    #: K02: explicit configured state — different from ``available``:
    #  ``configured`` means "credentials / settings present in the
    #  process environment", which is the only signal a UI can act on
    #  without contacting the cloud. ``available`` folds in the runtime
    #  reachability and is also computed without ever calling the cloud.
    configured: bool = False
    #: K02: "not_checked" because the capability endpoint NEVER probes
    #: the cloud; "invalid" if a previous run returned 401/403 (cached
    #: only, expires after restart).
    credential_validation: Literal["not_checked", "valid", "invalid"] = "not_checked"
    #: K02: structured unavailability reason, only set when ``available``
    #: is False; ``None`` when the engine is fully runnable.
    unavailable_reason: EngineUnavailableReason | None = None


class CapabilitiesResponse(ApiEnvelope):
    engines: list[EngineCapability]
    schema_version: str
    rule_version: str
    max_upload_bytes: int
    samples_available: bool


class SampleInfo(BaseModel):
    sample_id: str
    file_name: str
    category: Literal["paper", "patent", "scan"]
    bytes: int
    available: bool


class SamplesResponse(ApiEnvelope):
    samples: list[SampleInfo]


class DocumentResponse(ApiEnvelope):
    document_id: str
    sha256: str
    bytes: int
    page_count: int
    file_name: str
    is_encrypted: bool
    pages: list[dict[str, Any]] = Field(default_factory=list)


class JobCreateRequest(BaseModel):
    document_id: str
    #: auto walks the engine preference chain (MinerU -> Docling ->
    #: native) at creation time and persists the resolved engine.
    parser: str = "auto"
    page_numbers: list[int] | None = None
    targets: list[Literal["sequences", "activities"]] = ["sequences", "activities"]
    ocr_policy: Literal["auto", "force", "disabled"] = "auto"
    document_type_hint: Literal["auto", "paper", "patent"] = "auto"


class StageRunView(BaseModel):
    stage_name: str
    attempt: int
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    error: dict[str, str] = Field(default_factory=dict)


class JobResponse(ApiEnvelope):
    job_id: str
    document_id: str
    status: str
    current_stage: str | None = None
    result_revision: int = 0
    open_reviews: int = 0
    created_at: str
    updated_at: str
    stages: list[StageRunView] = Field(default_factory=list)
    export_artifact_id: str | None = None
    #: Effective config identity for the run-details view (design §7.7).
    config_hash: str = ""
    engine: str = ""


class WorkerPresenceView(BaseModel):
    worker_id: str
    pid: int | None = None
    seconds_since_heartbeat: float
    online: bool


class QueueStatusResponse(ApiEnvelope):
    """Live-ops view: are queued jobs actually being picked up?"""

    data_dir: str
    queued_jobs: int
    running_jobs: int
    workers: list[WorkerPresenceView] = Field(default_factory=list)
    online_worker_count: int
    stale_after_seconds: float


class EvidenceSpanView(BaseModel):
    evidence_id: str
    document_id: str
    page_number: int | None = None
    bbox: list[float] | None = None
    raw_text: str | None = None


class RecordView(BaseModel):
    """A sequence or measurement record with its review summary."""

    record_id: str
    kind: Literal["sequence", "measurement"]
    record: dict[str, Any]
    review_reasons: list[str] = Field(default_factory=list)
    open_review_id: str | None = None


class RecordsResponse(ApiEnvelope):
    job_id: str
    revision: int
    sequences: list[dict[str, Any]] = Field(default_factory=list)
    measurements: list[dict[str, Any]] = Field(default_factory=list)
    record_pages: dict[str, list[int]] = Field(default_factory=dict)
    #: Tables per page for the tables-tab overview; empty when the job has
    #: no normalized output yet.
    table_pages: dict[int, int] = Field(default_factory=dict)
    #: C02 declaration-level sequence inventory (``{"entries", "summary"}``).
    #: None for jobs extracted before the inventory existed — the UI shows
    #: "未评估" rather than inventing numbers.
    sequence_inventory: dict[str, Any] | None = None
    #: C04 entities / relations with real evidence ids, plus per-status
    #: counts. Empty lists mean "not extracted", never "none exist".
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    relation_summary: dict[str, Any] = Field(default_factory=dict)


class PageResponse(ApiEnvelope):
    job_id: str
    page_number: int
    width_pt: float
    height_pt: float
    route: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    figures: list[dict[str, Any]] = Field(default_factory=list)


class FiguresResponse(ApiEnvelope):
    job_id: str
    #: One entry per detected visual region (chart/plot/diagram/photo).
    figures: list[dict[str, Any]] = Field(default_factory=list)
    #: Engine limitation note for native-only jobs (empty when figures exist
    #: or the job used a layout engine).
    note: str | None = None
    #: C07: total figures across the document (before any page filter) and
    #: the applied page filter (None = 全文).
    total: int = 0
    page_number: int | None = None


class ArtifactInfo(BaseModel):
    artifact_id: str
    relative_path: str
    media_type: str
    bytes: int | None = None


class ArtifactsResponse(ApiEnvelope):
    job_id: str
    artifacts: list[ArtifactInfo]


class ReviewItemView(BaseModel):
    review_id: str
    job_id: str
    record_id: str
    record_kind: Literal["sequence", "measurement", "unknown"] = "unknown"
    record_revision: int
    reason_codes: list[str] = Field(default_factory=list)
    status: str
    evidence_ids: list[str] = Field(default_factory=list)
    #: D05: server-computed stable group identity (job + page/table cluster)
    #: — the UI groups by THIS, never by display text (two jobs on the same
    #: page are two groups) — and the baseline this item belongs to.
    group_id: str = ""
    baseline_id: str | None = None


class ReviewsResponse(ApiEnvelope):
    reviews: list[ReviewItemView]
    total: int
    #: C06 grouping: one entry per (job, page/table cluster) over the WHOLE
    #: filtered queue — "N 条 / M 组" without pretending the page is the queue.
    groups: list[dict[str, Any]] = Field(default_factory=list)


class ReviewDecisionRequest(BaseModel):
    action: Literal["confirm", "correct", "reject"]
    base_revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=120)
    reason: str | None = None
    patch: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    #: E1/S01: the baseline the client made its decision against. When
    #: given it must equal the live publication baseline, checked inside
    #: the decision transaction (409 REVIEW_BASELINE_CONFLICT otherwise).
    baseline_id: str | None = None


class ReviewDecisionResponse(ApiEnvelope):
    review_id: str
    action: str
    record_revision: int
    accepted: bool | None = None
    validation_state: str | None = None
    message: str | None = None


__all__ = [
    "API_VERSION",
    "ApiEnvelope",
    "ErrorResponse",
    "CapabilitiesResponse",
    "EngineCapability",
    "EngineUnavailableReason",
    "SampleInfo",
    "SamplesResponse",
    "DocumentResponse",
    "JobCreateRequest",
    "JobResponse",
    "StageRunView",
    "EvidenceSpanView",
    "RecordView",
    "RecordsResponse",
    "PageResponse",
    "ArtifactInfo",
    "ArtifactsResponse",
    "ReviewItemView",
    "ReviewsResponse",
    "ReviewDecisionRequest",
    "ReviewDecisionResponse",
    "CheckResult",
    "Measurement",
    "SequenceRecord",
]
