"""Job, stage, attempt and review contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from bio_pdf.contracts.common import NonEmptyStr, _StrictModel

JobStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "succeeded_with_review",
    "failed",
    "cancelled",
]

StageStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "skipped",
]

AttemptStatus = Literal["pending", "running", "succeeded", "failed"]

ReviewAction = Literal["confirm", "correct", "reject"]


def _utcnow() -> datetime:
    """Return the current UTC time (override-able for tests)."""

    return datetime.now(tz=timezone.utc)


class ArtifactRef(_StrictModel):
    """A reference to an immutable artifact on disk.

    ``relative_path`` is anchored at the configured ``artifact_root`` and
    must not contain ``..`` segments or absolute paths. The same path with
    a different ``sha256`` is reported as :data:`ARTIFACT_CONFLICT`.
    """

    artifact_id: NonEmptyStr
    sha256: NonEmptyStr
    relative_path: NonEmptyStr
    media_type: NonEmptyStr
    producer_stage_run_id: str | None = None


class Stage(_StrictModel):
    """One persisted stage execution.

    ``cache_key`` lets the runner skip re-running a stage whose inputs and
    rules have not changed. ``attempt`` counts from 1.
    """

    stage_run_id: NonEmptyStr
    job_id: NonEmptyStr
    stage_name: NonEmptyStr
    attempt: int = Field(ge=1)
    status: StageStatus = "pending"
    cache_key: NonEmptyStr | None = None
    artifact_manifest_ref: NonEmptyStr | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_json: dict[str, str] = Field(default_factory=dict)


class Job(_StrictModel):
    """A parse/extract job.

    The same ``document_id`` may have many ``Job`` rows over its history
    (different configs, retries). ``result_revision`` increments whenever
    the runner produces a new export revision.
    """

    job_id: NonEmptyStr
    document_id: NonEmptyStr
    config_hash: NonEmptyStr
    status: JobStatus = "queued"
    current_stage: NonEmptyStr | None = None
    result_revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class StageOutcome(_StrictModel):
    """The return value of a stage handler.

    ``review_required`` is a *count* of records the runner should add to
    the review queue, not a guarantee.
    """

    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    review_required: int = Field(default=0, ge=0)
    warnings: list[NonEmptyStr] = Field(default_factory=list)


class ValidationResult(_StrictModel):
    """The aggregate of a record's checks.

    ``review_reasons`` capture rationale beyond the structured checks so the
    review UI can present actionable guidance.
    """

    checks: list[dict[str, str]] = Field(default_factory=dict)
    validation_state: Literal[
        "unchecked",
        "checks_passed",
        "conflict",
        "verified_reference",
        "verified_human",
    ] = "unchecked"
    review_reasons: list[NonEmptyStr] = Field(default_factory=list)


class AcceptanceDecision(_StrictModel):
    """The decision produced by the acceptance policy.

    ``policy_version`` ties the decision to the exact policy text that
    produced it so we can reproduce historic decisions after policy
    upgrades.
    """

    acceptance_state: Literal[
        "candidate",
        "needs_review",
        "accepted",
        "rejected",
    ] = "candidate"
    verification_method: NonEmptyStr | None = None
    reason_codes: list[NonEmptyStr] = Field(default_factory=list)
    policy_version: NonEmptyStr = "v0"


class ReviewItem(_StrictModel):
    """An item waiting for human review.

    ``record_revision`` is the snapshot of the record that was being
    reviewed; review APIs reject decisions whose ``base_revision`` does
    not match the current record revision.
    """

    review_id: NonEmptyStr
    job_id: NonEmptyStr
    record_id: NonEmptyStr
    record_revision: int = Field(ge=1)
    reason_codes: list[NonEmptyStr] = Field(default_factory=list)
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)
    proposed_actions: list[NonEmptyStr] = Field(default_factory=list)
    status: Literal["open", "in_progress", "resolved", "skipped"] = "open"


class ReviewDecision(_StrictModel):
    """A human-issued decision appended to a review item.

    ``patch_json`` is the field-level correction when ``action='correct'``.
    ``base_revision`` ensures decisions cannot be silently applied to a
    record that has since changed.
    """

    decision_id: NonEmptyStr
    review_id: NonEmptyStr
    actor: NonEmptyStr
    action: ReviewAction
    patch_json: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)
    base_revision: int = Field(ge=1)
    created_at: datetime = Field(default_factory=_utcnow)