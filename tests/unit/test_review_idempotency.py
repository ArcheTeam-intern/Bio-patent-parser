"""C06: re-deriving the same review plan must not duplicate or reopen items."""

from __future__ import annotations

from pathlib import Path

from bio_pdf.contracts import ReviewItem
from bio_pdf.review.plan import build_review_plan
from bio_pdf.storage.repository import Repository


def test_review_ids_are_stable_across_plan_runs() -> None:
    records = [
        {
            "job_id": "job-1",
            "measurement_id": "sha256:d:meas:1:prose:1",
            "record_revision": 1,
            "reason_codes": ["measurement has no unit"],
            "evidence_ids": ["ev-1"],
        }
    ]
    first = build_review_plan(records, budget=50)
    second = build_review_plan(records, budget=50)
    assert [i.review_id for i in first] == [i.review_id for i in second]


def test_repeated_upsert_does_not_duplicate_or_reopen(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "bio.sqlite")
    from datetime import datetime, timezone

    from bio_pdf.contracts import Job

    repo.upsert_document(
        document_id="sha256:doc",
        sha256="doc",
        source_path=str(tmp_path / "doc.pdf"),
        bytes_count=1,
        metadata={},
    )
    now = datetime.now(tz=timezone.utc)
    repo.create_job(
        Job(
            job_id="job-1",
            document_id="sha256:doc",
            config_hash="h",
            status="queued",
            created_at=now,
            updated_at=now,
        )
    )
    item = ReviewItem(
        review_id="review-abc",
        job_id="job-1",
        record_id="rec-1",
        record_revision=1,
        reason_codes=["measurement has no unit"],
        status="open",
        evidence_ids=["ev-1"],
        proposed_actions=[],
    )
    repo.upsert_review_item(item)
    repo.upsert_review_item(item)
    count = repo.connection.execute(
        "SELECT COUNT(*) AS n FROM review_items WHERE review_id = 'review-abc'"
    ).fetchone()["n"]
    assert count == 1

    repo.connection.execute(
        "UPDATE review_items SET status = 'resolved' WHERE review_id = 'review-abc'"
    )
    repo.connection.commit()
    repo.upsert_review_item(item)
    status = repo.connection.execute(
        "SELECT status FROM review_items WHERE review_id = 'review-abc'"
    ).fetchone()["status"]
    assert status == "resolved", "a decided item must never be reopened by a re-run"
    repo.close()
