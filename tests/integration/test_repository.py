"""SQLite repository integration tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.contracts import (
    ArtifactRef,
    Job,
    ReviewDecision,
    ReviewItem,
    Stage,
)
from bio_pdf.errors import (
    DatabaseError,
    ReviewRevisionConflictError,
)
from bio_pdf.storage.repository import (
    SCHEMA_SQL,
    Repository,
    init_repository,
)


def _new_job(document_id: str = "sha256:doc") -> Job:
    now = datetime.now(tz=timezone.utc)
    return Job(
        job_id="job-1",
        document_id=document_id,
        config_hash="cfg-1",
        status="queued",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    database = tmp_path / "bio.sqlite"
    r = Repository(database)
    yield r
    r.close()


def test_init_repository_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "bio.sqlite"
    conn1 = init_repository(database)
    names1 = {row["name"] for row in conn1.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn1.close()
    conn2 = init_repository(database)
    try:
        names2 = {row["name"] for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert names1 == names2
    finally:
        conn2.close()


def test_migration_creates_all_expected_tables(tmp_path: Path) -> None:
    database = tmp_path / "bio.sqlite"
    conn = init_repository(database)
    try:
        names = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = {
            "documents",
            "jobs",
            "stage_runs",
            "artifacts",
            "review_items",
            "review_decisions",
        }
        assert expected.issubset(names)
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = tmp_path / "bio.sqlite"
    conn = init_repository(database)
    try:
        # Inserting a job for a non-existent document must fail because of the
        # foreign key constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO jobs (job_id, document_id, config_hash, status, current_stage,
                                  result_revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-x",
                    "sha256:missing",
                    "cfg-1",
                    "queued",
                    None,
                    0,
                    datetime.now(tz=timezone.utc).isoformat(),
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
    finally:
        conn.close()


def test_create_and_get_job_round_trip(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc",
        sha256="a" * 64,
        source_path="/tmp/doc.pdf",
        bytes_count=123,
    )
    job = _new_job()
    repo.create_job(job)
    loaded = repo.get_job("job-1")
    assert loaded == job


def test_start_and_complete_stage(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc", sha256="a" * 64, source_path="/tmp/x.pdf", bytes_count=1
    )
    repo.create_job(_new_job())
    stage = Stage(
        stage_run_id="sr-1",
        job_id="job-1",
        stage_name="native_parse",
        attempt=1,
        status="running",
        cache_key="ck-1",
        started_at=datetime.now(tz=timezone.utc),
    )
    repo.start_stage(stage)
    repo.complete_stage("sr-1", artifact_manifest_ref="artifacts/sr-1.json")
    loaded = next(iter(repo.list_stages("job-1")))
    assert loaded.status == "succeeded"
    assert loaded.ended_at is not None


def test_fail_stage_records_error_json(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc", sha256="a" * 64, source_path="/tmp/x.pdf", bytes_count=1
    )
    repo.create_job(_new_job())
    stage = Stage(
        stage_run_id="sr-2",
        job_id="job-1",
        stage_name="layout_parse",
        attempt=1,
        status="running",
        started_at=datetime.now(tz=timezone.utc),
    )
    repo.start_stage(stage)
    repo.fail_stage("sr-2", error_json={"code": "ENGINE_TIMEOUT", "message": "oops"})
    loaded = next(iter(repo.list_stages("job-1")))
    assert loaded.status == "failed"
    assert loaded.error_json["code"] == "ENGINE_TIMEOUT"


def test_find_successful_stage_returns_match(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc", sha256="a" * 64, source_path="/tmp/x.pdf", bytes_count=1
    )
    repo.create_job(_new_job())
    for attempt in (1, 2):
        stage = Stage(
            stage_run_id=f"sr-{attempt}",
            job_id="job-1",
            stage_name="native_parse",
            attempt=attempt,
            status="succeeded",
            cache_key="ck-stable",
            started_at=datetime.now(tz=timezone.utc),
            ended_at=datetime.now(tz=timezone.utc),
        )
        repo.start_stage(stage)
        repo.complete_stage(f"sr-{attempt}", artifact_manifest_ref=f"art-{attempt}.json")
    found = repo.find_successful_stage("job-1", "native_parse", "ck-stable")
    assert found is not None
    assert found.attempt == 2  # most recent


def test_find_successful_stage_ignores_failed_runs(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc", sha256="a" * 64, source_path="/tmp/x.pdf", bytes_count=1
    )
    repo.create_job(_new_job())
    failed = Stage(
        stage_run_id="sr-f",
        job_id="job-1",
        stage_name="layout_parse",
        attempt=1,
        status="failed",
        cache_key="ck",
        started_at=datetime.now(tz=timezone.utc),
    )
    repo.start_stage(failed)
    repo.fail_stage("sr-f", error_json={"code": "ENGINE_TIMEOUT"})
    assert repo.find_successful_stage("job-1", "layout_parse", "ck") is None


def test_register_and_get_artifact(repo: Repository) -> None:
    ref = ArtifactRef(
        artifact_id="sha256:abc",
        sha256="abc",
        relative_path="data/x.json",
        media_type="application/json",
        producer_stage_run_id="sr-1",
    )
    repo.register_artifact(ref)
    loaded = repo.get_artifact("sha256:abc")
    assert loaded == ref


def test_append_review_decision_requires_matching_revision(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc", sha256="a" * 64, source_path="/tmp/x.pdf", bytes_count=1
    )
    repo.create_job(_new_job())
    review = ReviewItem(
        review_id="rev-1",
        job_id="job-1",
        record_id="seq-1",
        record_revision=3,
        reason_codes=["SEQ_CONFLICT"],
    )
    repo.upsert_review_item(review)
    decision = ReviewDecision(
        decision_id="dec-1",
        review_id="rev-1",
        actor="user@example.com",
        action="confirm",
        base_revision=2,  # stale
    )
    with pytest.raises(ReviewRevisionConflictError):
        repo.append_review_decision(decision)


def test_append_review_decision_appends_history(repo: Repository) -> None:
    repo.upsert_document(
        document_id="sha256:doc", sha256="a" * 64, source_path="/tmp/x.pdf", bytes_count=1
    )
    repo.create_job(_new_job())
    review = ReviewItem(
        review_id="rev-1",
        job_id="job-1",
        record_id="seq-1",
        record_revision=1,
        reason_codes=["SEQ_CONFLICT"],
    )
    repo.upsert_review_item(review)
    decision = ReviewDecision(
        decision_id="dec-1",
        review_id="rev-1",
        actor="user@example.com",
        action="correct",
        patch_json={"code": "MKTXG"},
        evidence_ids=["ev-1"],
        base_revision=1,
    )
    repo.append_review_decision(decision)
    history = repo.list_review_decisions("rev-1")
    assert len(history) == 1
    assert history[0].action == "correct"
    assert history[0].patch_json["code"] == "MKTXG"


def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    database = tmp_path / "bio.sqlite"
    conn = init_repository(database)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_migration_script_is_safe_to_replay(tmp_path: Path) -> None:
    """Running the migration twice must not change the resulting schema."""

    database = tmp_path / "bio.sqlite"
    conn1 = init_repository(database)
    names1 = {row["name"] for row in conn1.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn1.close()
    conn2 = init_repository(database)
    try:
        names2 = {row["name"] for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert names1 == names2
    finally:
        conn2.close()


def test_migration_sql_runs_cleanly() -> None:
    """The schema SQL should be parseable on an in-memory database."""

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()