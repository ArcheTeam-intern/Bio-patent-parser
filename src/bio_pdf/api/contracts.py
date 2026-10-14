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


class EngineCapability(BaseModel):
    name: str
    available: bool
    supports_tables: bool
    supports_ocr: bool
    supports_page_range: bool
    sends_bytes_offsite: bool
    ocr_available: bool = False
    ocr_engines: dict[str, bool] = Field(default_factory=dict)


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
    parser: str = "native"
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


class PageResponse(ApiEnvelope):
    job_id: str
    page_number: int
    width_pt: float
    height_pt: float
    route: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)


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


class ReviewsResponse(ApiEnvelope):
    reviews: list[ReviewItemView]
    total: int


class ReviewDecisionRequest(BaseModel):
    action: Literal["confirm", "correct", "reject"]
    base_revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=120)
    reason: str | None = None
    patch: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)


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
