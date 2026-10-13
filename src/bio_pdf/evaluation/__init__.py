"""Evaluation (M1C): annotation, matching, metrics, report."""

from bio_pdf.evaluation.annotation import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
)
from bio_pdf.evaluation.metrics import (
    character_error_rate,
    safe_ratio,
)
from bio_pdf.evaluation.matching import (
    SequenceMatchResult,
    match_measurements,
    match_sequences,
)
from bio_pdf.evaluation.report import generate_report

__all__ = [
    "Annotation",
    "AnnotationKind",
    "AnnotationStatus",
    "safe_ratio",
    "character_error_rate",
    "match_sequences",
    "match_measurements",
    "SequenceMatchResult",
    "generate_report",
]