"""Evaluation report generation (Markdown)."""

from __future__ import annotations

from typing import Any

from bio_pdf.evaluation.metrics import safe_ratio


def generate_report(
    *,
    config: dict[str, Any],
    sequence_metrics: dict[str, Any],
    measurement_metrics: dict[str, Any],
    page_stats: dict[str, Any],
) -> str:
    """Render a Markdown report with denominators and honest coverage notes.

    The report never claims statistical proof from a small (≈30 page) sample.
    """

    lines: list[str] = []
    lines.append("# Bio PDF Evaluation Report\n")
    lines.append(f"- **config**: `{config}`")
    lines.append(f"- **verified pages**: {page_stats.get('verified_pages', 0)}")
    lines.append(f"- **unreadable pages**: {page_stats.get('unreadable_pages', 0)}")
    lines.append("")

    lines.append("## Sequences\n")
    lines.append(f"- total gold: {sequence_metrics.get('total_gold', 0)}")
    lines.append(f"- exact matches: {sequence_metrics.get('exact_matches', 0)}")
    lines.append(f"- detected: {sequence_metrics.get('detected', 0)}")
    lines.append(f"- missed: {sequence_metrics.get('missed', 0)}")
    lines.append(f"- false positives: {sequence_metrics.get('false_positives', 0)}")
    lines.append(
        f"- exact precision: {_fmt(sequence_metrics.get('exact_precision'))}"
    )
    lines.append(
        f"- detection recall: {_fmt(sequence_metrics.get('detection_recall'))}"
    )
    lines.append("")

    lines.append("## Measurements\n")
    lines.append(f"- total gold: {measurement_metrics.get('total_gold', 0)}")
    lines.append(f"- tuple correct: {measurement_metrics.get('correct', 0)}")
    lines.append(f"- missed: {measurement_metrics.get('missed', 0)}")
    lines.append(f"- false positives: {measurement_metrics.get('false_positives', 0)}")
    lines.append(
        f"- tuple precision: {_fmt(measurement_metrics.get('precision'))}"
    )
    lines.append("")

    lines.append(
        "> With a ~30 page sample these numbers are *indicative*, not "
        "statistical proof. `auto_accept` remains off."
    )
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


__all__ = ["generate_report"]