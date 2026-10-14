"""Domain error codes used across the workflow.

The error code strings come from design §9 and are stable across languages
so the Go/TS migration can reuse the same vocabulary.
"""

from __future__ import annotations

from typing import Any


class WorkflowError(Exception):
    """Base error with a stable machine-readable ``code``.

    Subclasses or callers populate ``code`` from the design-defined list:

    ``INPUT_NOT_PDF, INPUT_CORRUPT, INPUT_ENCRYPTED, PAGE_RANGE_INVALID``,
    ``ENGINE_UNAVAILABLE, MODEL_MISSING, ENGINE_PROTOCOL_UNSUPPORTED``,
    ``ENGINE_TIMEOUT, NETWORK_TRANSIENT, RATE_LIMITED``,
    ``RESOURCE_EXHAUSTED``,
    ``PAGE_COVERAGE_MISMATCH, NORMALIZATION_LOSS``,
    ``SEQUENCE_CONFLICT, TABLE_ALIGNMENT_UNCERTAIN, LINK_UNRESOLVED``,
    ``MODEL_OUTPUT_INVALID, EVIDENCE_MISSING``,
    ``ARTIFACT_WRITE_FAILED, ARTIFACT_CORRUPT, ARTIFACT_CONFLICT,
    DATABASE_ERROR``,
    ``REMOTE_JOB_LOST``,
    ``REVIEW_REVISION_CONFLICT``.
    """

    code: str = "WORKFLOW_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class InputNotPdfError(WorkflowError):
    code = "INPUT_NOT_PDF"


class InputCorruptError(WorkflowError):
    code = "INPUT_CORRUPT"


class InputEncryptedError(WorkflowError):
    code = "INPUT_ENCRYPTED"


class PageRangeInvalidError(WorkflowError):
    code = "PAGE_RANGE_INVALID"


class EngineUnavailableError(WorkflowError):
    code = "ENGINE_UNAVAILABLE"


class ModelMissingError(WorkflowError):
    code = "MODEL_MISSING"


class EngineProtocolUnsupportedError(WorkflowError):
    code = "ENGINE_PROTOCOL_UNSUPPORTED"


class EngineTimeoutError(WorkflowError):
    code = "ENGINE_TIMEOUT"


class NetworkTransientError(WorkflowError):
    code = "NETWORK_TRANSIENT"


class RateLimitedError(WorkflowError):
    code = "RATE_LIMITED"


class ResourceExhaustedError(WorkflowError):
    code = "RESOURCE_EXHAUSTED"


class PageCoverageMismatchError(WorkflowError):
    code = "PAGE_COVERAGE_MISMATCH"


class NormalizationLossError(WorkflowError):
    code = "NORMALIZATION_LOSS"


class SequenceConflictError(WorkflowError):
    code = "SEQUENCE_CONFLICT"


class TableAlignmentUncertainError(WorkflowError):
    code = "TABLE_ALIGNMENT_UNCERTAIN"


class LinkUnresolvedError(WorkflowError):
    code = "LINK_UNRESOLVED"


class ModelOutputInvalidError(WorkflowError):
    code = "MODEL_OUTPUT_INVALID"


class EvidenceMissingError(WorkflowError):
    code = "EVIDENCE_MISSING"


class ArtifactWriteFailedError(WorkflowError):
    code = "ARTIFACT_WRITE_FAILED"


class ArtifactCorruptError(WorkflowError):
    code = "ARTIFACT_CORRUPT"


class ArtifactConflictError(WorkflowError):
    code = "ARTIFACT_CONFLICT"


class DatabaseError(WorkflowError):
    code = "DATABASE_ERROR"


class RemoteJobLostError(WorkflowError):
    code = "REMOTE_JOB_LOST"


class ReviewRevisionConflictError(WorkflowError):
    code = "REVIEW_REVISION_CONFLICT"


class JobRunningError(WorkflowError):
    """Resume attempted on a job whose lease is still valid (plan T03)."""

    code = "JOB_RUNNING"