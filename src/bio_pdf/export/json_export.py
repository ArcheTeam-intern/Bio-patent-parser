"""JSON result export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bio_pdf.contracts import Measurement, SequenceRecord


def export_result_json(
    sequences: list[SequenceRecord],
    measurements: list[Measurement],
    *,
    accepted_only: bool = False,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Build a result summary with candidate/review counts.

    When ``accepted_only`` is True, only records with ``acceptance_state ==
    'accepted'`` are returned in the payload. An empty accepted list is NOT
    reported as "no sequences" — the summary always carries the candidate and
    review counts.
    """

    accepted_sequences = [s for s in sequences if s.acceptance_state == "accepted"]
    accepted_measurements = [m for m in measurements if m.acceptance_state == "accepted"]

    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "summary": {
            "sequences_total": len(sequences),
            "measurements_total": len(measurements),
            "sequences_accepted": len(accepted_sequences),
            "measurements_accepted": len(accepted_measurements),
            "sequences_candidates": sum(1 for s in sequences if s.acceptance_state in {"candidate", "needs_review"}),
            "sequences_needs_review": sum(1 for s in sequences if s.acceptance_state == "needs_review"),
        },
        "sequences": [
            s.model_dump(mode="json")
            for s in (accepted_sequences if accepted_only else sequences)
        ],
        "measurements": [
            m.model_dump(mode="json")
            for m in (accepted_measurements if accepted_only else measurements)
        ],
    }

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return result


__all__ = ["export_result_json"]