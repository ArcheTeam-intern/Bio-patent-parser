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


# ---- D05: stable identity + baseline supersede -----------------------------


def _seed_job(repo, job_id: str) -> None:
    from datetime import datetime, timezone

    from bio_pdf.contracts import Job

    now = datetime.now(tz=timezone.utc)
    repo.create_job(
        Job(
            job_id=job_id,
            document_id="sha256:doc",
            config_hash="h",
            status="queued",
            created_at=now,
            updated_at=now,
        )
    )


def test_new_baseline_supersedes_stale_open_items_and_keeps_decisions(
    tmp_path: Path,
) -> None:
    """D05: re-publishing marks stale OPEN items superseded; a human-decided
    item and its decision log survive untouched."""

    repo = Repository(tmp_path / "bio.sqlite")
    repo.upsert_document(
        document_id="sha256:doc",
        sha256="doc",
        source_path=str(tmp_path / "doc.pdf"),
        bytes_count=1,
        metadata={},
    )
    _seed_job(repo, "job-1")

    def item(rid: str, record: str) -> ReviewItem:
        return ReviewItem(
            review_id=rid,
            job_id="job-1",
            record_id=record,
            record_revision=1,
            reason_codes=["measurement has no unit"],
            status="open",
            evidence_ids=[],
            proposed_actions=[],
        )

    # First publication (baseline A) with two open items.
    ok = repo.publish_result_fenced(
        token=None,
        job_id="job-1",
        final_status="succeeded_with_review",
        manifest_rel="jobs/job-1/manifest-a.json",
        manifest_sha="a" * 40,
        review_items=[item("review-1", "rec-1"), item("review-2", "rec-2")],
        issues=[],
    )
    assert ok
    # A human decides review-1 (decision log + resolved).
    repo.connection.execute(
        "UPDATE review_items SET status = 'resolved' WHERE review_id = 'review-1'"
    )
    repo.connection.execute(
        "INSERT INTO review_decisions (decision_id, review_id, actor, action, patch_json,"
        " evidence_ids_json, base_revision, created_at)"
        " VALUES ('dec-1', 'review-1', 'human', 'confirm', '{}', '[]', 1, '2026-10-16T00:00:00+00:00')"
    )
    repo.connection.commit()

    # Second publication (baseline B): review-2 is re-derived, review-3 is new.
    ok = repo.publish_result_fenced(
        token=None,
        job_id="job-1",
        final_status="succeeded_with_review",
        manifest_rel="jobs/job-1/manifest-b.json",
        manifest_sha="b" * 40,
        review_items=[item("review-2", "rec-2"), item("review-3", "rec-3")],
        issues=[],
    )
    assert ok

    rows = {
        row["review_id"]: row
        for row in repo.connection.execute(
            "SELECT review_id, status, baseline_id FROM review_items"
        ).fetchall()
    }
    assert rows["review-1"]["status"] == "resolved", "human decision must survive"
    assert rows["review-2"]["status"] == "open", "re-derived item is live in the new baseline"
    assert rows["review-2"]["baseline_id"] == "b" * 16
    assert rows["review-3"]["status"] == "open"
    assert rows["review-3"]["baseline_id"] == "b" * 16

    decisions = repo.list_review_decisions("review-1")
    assert len(decisions) == 1, "decision log must not be touched"

    # A stale open item that is NOT re-derived becomes superseded.
    repo.connection.execute(
        "UPDATE review_items SET status = 'open' WHERE review_id = 'review-3'"
    )
    repo.connection.commit()
    ok = repo.publish_result_fenced(
        token=None,
        job_id="job-1",
        final_status="succeeded_with_review",
        manifest_rel="jobs/job-1/manifest-c.json",
        manifest_sha="c" * 40,
        review_items=[],
        issues=[],
    )
    assert ok
    status = repo.connection.execute(
        "SELECT status FROM review_items WHERE review_id = 'review-3'"
    ).fetchone()["status"]
    assert status == "superseded", "stale open item must be marked superseded, not silently kept"
    assert rows["review-1"]["status"] == "resolved"
    repo.close()


def test_group_id_binds_job_not_label() -> None:
    """D05: two jobs with the same page/table label must be two groups."""

    from bio_pdf.api.routes.reviews import _group_id

    record = "sha256:doc:meas:8:grid:r1c1"
    first = _group_id("job-a", record)
    second = _group_id("job-b", record)
    assert first != second
    # Same job + same page are one group.
    assert _group_id("job-a", record) == _group_id("job-a", "sha256:doc:meas:8:grid:r9c9")
