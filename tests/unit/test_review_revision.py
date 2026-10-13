"""Review decision / revision tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.contracts import Job, ReviewItem
from bio_pdf.errors import ReviewRevisionConflictError
from bio_pdf.review.decisions import apply_decision
from bio_pdf.storage.repository import Repository


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "bio.sqlite")
    now = datetime.now(tz=timezone.utc)
    r.upsert_document(
        document_id="sha256:doc", sha256="a" * 64,
        source_path="/tmp/x.pdf", bytes_count=1,
    )
    r.create_job(Job(
        job_id="job-1", document_id="sha256:doc",
        config_hash="cfg", created_at=now, updated_at=now,
    ))
    r.upsert_review_item(ReviewItem(
        review_id="rev-1", job_id="job-1", record_id="seq-1",
        record_revision=1, reason_codes=["SEQ_CONFLICT"],
    ))
    yield r
    r.close()


def test_apply_decision_appends_history(repo: Repository) -> None:
    decision = apply_decision(
        repo,
        review_id="rev-1",
        actor="user@example.com",
        action="correct",
        base_revision=1,
        patch={"normalized_sequence": "MKTAG"},
        evidence_ids=["ev-1"],
    )
    history = repo.list_review_decisions("rev-1")
    assert len(history) == 1
    assert history[0].patch_json["normalized_sequence"] == "MKTAG"


def test_apply_decision_stale_revision_conflicts(repo: Repository) -> None:
    with pytest.raises(ReviewRevisionConflictError):
        apply_decision(
            repo,
            review_id="rev-1",
            actor="user@example.com",
            action="reject",
            base_revision=99,  # stale
        )


def test_reject_decision_is_recorded(repo: Repository) -> None:
    apply_decision(
        repo,
        review_id="rev-1",
        actor="user@example.com",
        action="reject",
        base_revision=1,
    )
    history = repo.list_review_decisions("rev-1")
    assert history[0].action == "reject"