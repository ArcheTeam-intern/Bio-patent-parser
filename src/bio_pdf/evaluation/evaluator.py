"""End-to-end evaluation entry (plan B10).

``evaluate_corpus`` consumes a v2 gold manifest and a records snapshot
(extract.json or a job's published extract), runs the one-to-one matchers
inside the manifest's verified scopes, and produces a machine-readable
metrics dict + a Markdown report.

Without verified gold the output is explicitly ``not_evaluated`` with
coverage/diagnostics — never 0% or 100%.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bio_pdf.evaluation.corpus import (
    CorpusScope,
    evaluation_status,
    load_corpus_scope,
)
from bio_pdf.evaluation.matching import match_measurements, match_sequences
from bio_pdf.evaluation.metrics import safe_ratio


def _load_records(records_path: Path) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """Load candidate sequences/measurements + evidence spans from a snapshot."""

    payload = json.loads(records_path.read_text(encoding="utf-8"))
    sequences = payload.get("sequences", [])
    measurements = payload.get("measurements", [])
    spans = payload.get("evidence_spans", [])
    return sequences, measurements, spans


def evaluate_corpus(
    *,
    gold_path: Path,
    records_path: Path,
) -> dict[str, Any]:
    scopes, annotations = load_corpus_scope(gold_path)
    status = evaluation_status(scopes, annotations)

    sequences_raw, measurements_raw, spans_raw = _load_records(records_path)
    evidence_index = {s.get("evidence_id"): s for s in spans_raw}

    in_scope_ids = [s.document_id for s in scopes if s.scope_verified]
    # Candidates are only scored inside verified scopes (unannotated
    # coverage is reported, never counted as FP).
    scope_docs = set(in_scope_ids) or None

    metrics: dict[str, Any] = {
        "status": status,
        "scopes": [s.to_dict() for s in scopes],
        "note": (
            "not_evaluated: no verified corpus scope — coverage and diagnostics "
            "only, never an accuracy claim"
            if status == "not_evaluated"
            else "evaluated against human-verified annotations in the listed scopes"
        ),
    }

    if status == "not_evaluated":
        metrics["coverage"] = {
            "annotated_documents": len(scopes),
            "verified_scopes": len(in_scope_ids),
            "candidate_sequences": len(sequences_raw),
            "candidate_measurements": len(measurements_raw),
        }
        return metrics

    from bio_pdf.contracts import Measurement, SequenceRecord

    sequences = []
    for raw in sequences_raw:
        try:
            item = {k: v for k, v in raw.items() if k not in ("_revision", "blockers", "allowed_actions", "review_reasons")}
            sequences.append(SequenceRecord.model_validate(item))
        except Exception:  # noqa: BLE001 - malformed candidate excluded loudly
            continue
    measurements = []
    for raw in measurements_raw:
        try:
            item = {k: v for k, v in raw.items() if k not in ("_revision", "blockers", "allowed_actions", "review_reasons")}
            measurements.append(Measurement.model_validate(item))
        except Exception:  # noqa: BLE001
            continue

    seq_result = match_sequences(
        annotations, sequences, scope=scope_docs, evidence_index=evidence_index
    )
    meas_result = match_measurements(annotations, measurements, scope=scope_docs)

    total_seq_candidates = seq_result.exact_matches + seq_result.false_positives
    total_meas_candidates = meas_result.correct + meas_result.false_positives
    metrics["sequences"] = {
        "total_gold": seq_result.total_gold,
        "exact_tp": seq_result.exact_matches,
        "detected": seq_result.detected,
        "exact_fn": seq_result.total_gold - seq_result.exact_matches,
        "exact_fp": seq_result.false_positives,
        "exact_precision": safe_ratio(seq_result.exact_matches, total_seq_candidates),
        "exact_recall": safe_ratio(seq_result.exact_matches, seq_result.total_gold),
        "char_errors": seq_result.char_errors,
    }
    metrics["measurements"] = {
        "total_gold": meas_result.total_gold,
        "exact_tp": meas_result.correct,
        "wrong_value": meas_result.wrong_value,  # diagnostic subset, NOT subtracted
        "exact_fn": meas_result.total_gold - meas_result.correct,
        "exact_fp": meas_result.false_positives,
        "field_errors": meas_result.field_errors,
        "exact_precision": safe_ratio(meas_result.correct, total_meas_candidates),
        "exact_recall": safe_ratio(meas_result.correct, meas_result.total_gold),
    }
    return metrics


def write_report(metrics: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    lines = ["# 评测报告", ""]
    lines.append(f"- status: **{metrics['status']}**")
    lines.append(f"- note: {metrics.get('note', '')}")
    if metrics["status"] == "evaluated":
        for kind in ("sequences", "measurements"):
            section = metrics[kind]
            lines.append("")
            lines.append(f"## {kind}")
            for key in (
                "total_gold",
                "exact_tp",
                "exact_fn",
                "exact_fp",
                "exact_precision",
                "exact_recall",
            ):
                lines.append(f"- {key}: {section.get(key)}")
            if kind == "measurements":
                lines.append(f"- wrong_value (diagnostic subset): {section.get('wrong_value')}")
    else:
        lines.append("")
        lines.append("## coverage")
        for key, value in (metrics.get("coverage") or {}).items():
            lines.append(f"- {key}: {value}")
        lines.append("")
        lines.append(
            "> 没有人工验证的 gold：不输出准确率/召回率，只报告覆盖与候选统计。"
        )
    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metrics_path, report_path


__all__ = ["evaluate_corpus", "write_report"]
