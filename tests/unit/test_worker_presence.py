"""Worker presence (live-ops).

An idle worker holds no job lease, so without presence rows the queue
cannot tell "no parser process is running" from "the parser is just
slow" — an uploaded job would sit at 等待解析 with no explanation.
"""

from __future__ import annotations

from pathlib import Path

from bio_pdf.storage.repository import Repository


def test_presence_roundtrip_and_refresh(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "bio.sqlite")
    try:
        repo.record_worker_presence("worker-a", pid=123, data_dir=str(tmp_path))
        rows = repo.list_worker_presence()
        assert len(rows) == 1
        assert rows[0]["worker_id"] == "worker-a"
        assert rows[0]["pid"] == 123
        first_started = rows[0]["started_at"]

        # A later beat refreshes the same row — it must not duplicate and
        # must not reset started_at.
        repo.record_worker_presence("worker-a", pid=123, data_dir=str(tmp_path))
        rows = repo.list_worker_presence()
        assert len(rows) == 1
        assert rows[0]["started_at"] == first_started

        # Two workers on one data dir are separate rows (they compete for
        # claims but both prove polling).
        repo.record_worker_presence("worker-b", pid=124, data_dir=str(tmp_path))
        assert len(repo.list_worker_presence()) == 2

        repo.clear_worker_presence("worker-a")
        remaining = repo.list_worker_presence()
        assert [r["worker_id"] for r in remaining] == ["worker-b"]
    finally:
        repo.close()


def test_count_jobs_by_status_empty(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "bio.sqlite")
    try:
        assert repo.count_jobs_by_status() == {}
    finally:
        repo.close()
