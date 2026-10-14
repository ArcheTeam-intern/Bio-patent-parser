"""record_pages resolution for the records API (frontend page sync).

Page locations come only from real evidence fragments inside the extract
artifact. Missing or unreadable extracts degrade to empty lists — the
records response itself must keep working.
"""

from __future__ import annotations

import json
from pathlib import Path

from bio_pdf.application.result_snapshot import SnapshotService
from bio_pdf.storage.repository import Repository


def _service(tmp_path: Path) -> tuple[SnapshotService, Repository, Path]:
    root = tmp_path / "data"
    (root / "jobs" / "job-x").mkdir(parents=True)
    repo = Repository(tmp_path / "bio.sqlite")
    return SnapshotService(repo, root), repo, root


def _write_extract(root: Path, spans: list[dict]) -> None:
    out = root / "jobs" / "job-x" / "extract"
    out.mkdir(parents=True, exist_ok=True)
    (out / "extract.json").write_text(
        json.dumps({"evidence_spans": spans}), encoding="utf-8"
    )


def _snapshot() -> dict:
    return {
        "baseline_id": "baseline-1",
        "result_revision": 1,
        "sequences": [
            {
                "sequence_id": "seq-1",
                "evidence_ids": ["ev-a"],
                "field_evidence": {},
            }
        ],
        "measurements": [
            {
                "measurement_id": "meas-1",
                "evidence_ids": ["ev-b"],
                "field_evidence": {"value": ["ev-c"], "unit_raw": ["ev-missing"]},
            },
            {
                "measurement_id": "meas-2",
                "evidence_ids": [],
                "field_evidence": {},
            },
        ],
    }


def test_pages_union_top_level_fragments_and_fields(tmp_path: Path) -> None:
    service, repo, root = _service(tmp_path)
    try:
        _write_extract(
            root,
            [
                # Span-level page plus a cross-page fragment.
                {"evidence_id": "ev-a", "page_number": 2, "fragments": [{"page_number": 3}]},
                # Only fragments carry the page (span page_number is None).
                {"evidence_id": "ev-b", "page_number": None, "fragments": [{"page_number": 7}]},
                {"evidence_id": "ev-c", "page_number": 7, "fragments": []},
                # Non-page noise must be filtered, not turned into a page 0.
                {"evidence_id": "ev-d", "page_number": 0, "fragments": [{"page_number": None}]},
                # A span without an evidence_id must not crash resolution.
                {"page_number": 5, "fragments": []},
            ],
        )
        pages = service._record_pages("job-x", _snapshot())
    finally:
        repo.close()
    assert pages["seq-1"] == [2, 3]
    assert pages["meas-1"] == [7]
    # meas-2 has no evidence: an explicit empty list, never an invented page.
    assert pages["meas-2"] == []
    # Every record id appears as a key.
    assert set(pages) == {"seq-1", "meas-1", "meas-2"}


def test_missing_extract_degrades_to_empty_pages(tmp_path: Path) -> None:
    service, repo, root = _service(tmp_path)
    try:
        # baseline_id is set but no extract artifact exists anywhere: the
        # reader raises RESULT_CORRUPT; record pages must degrade instead of
        # failing the whole records response.
        pages = service._record_pages("job-x", _snapshot())
    finally:
        repo.close()
    assert pages == {"seq-1": [], "meas-1": [], "meas-2": []}


def test_no_published_baseline_skips_extract_entirely(tmp_path: Path) -> None:
    service, repo, root = _service(tmp_path)
    try:
        # Without a baseline, manifest or legacy extract the gate skips the
        # read: no pages, no error.
        snapshot = _snapshot()
        snapshot["baseline_id"] = None
        pages = service._record_pages("job-x", snapshot)
    finally:
        repo.close()
    assert pages == {"seq-1": [], "meas-1": [], "meas-2": []}