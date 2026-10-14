"""B03: real fencing, manifest verification and the publication transaction.

Counterexamples from the post-M1 review:

- a generation-1 worker whose lease was reclaimed by generation-2 must not
  change status / review items / published refs on ANY path (success or
  exception);
- stealing the lease between the last guard and publishing must abort the
  publish;
- a tampered stage manifest whose output hashes were also updated must
  still not verify (the manifest itself is hashed);
- manifest semantic mismatches (job/stage/cache_key/inputs) invalidate the
  cache;
- a corrupted published manifest must NOT silently fall back to legacy
  working files.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.contracts import ArtifactRef, Job, StageOutcome
from bio_pdf.errors import WorkflowError
from bio_pdf.storage.artifacts import atomic_write_bytes, compute_sha256_bytes
from bio_pdf.storage.repository import Repository
from bio_pdf.storage.result_reader import ResultReader
from bio_pdf.workflow.runner import WorkflowRunner
from bio_pdf.workflow.stages import StageContext


class _Handler:
    """Writes one artifact; can lose the lease mid-run."""

    name = "native_parse"

    def __init__(self, *, lose_after: int | None = None) -> None:
        self.calls = 0
        self.lose_after = lose_after
        self.guard_calls = 0

    def cache_key(self, context: StageContext) -> str:
        return "k"

    def implementation_version(self) -> str:
        return "test"

    def provider_version(self, context: StageContext) -> str:
        return ""

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        if self.lose_after is not None and self.calls >= self.lose_after:
            # The lease is stolen exactly while the stage runs.
            self.lost = True
        ref = atomic_write_bytes(Path(context.stage_dir()), "out.bin", b"payload")
        return StageOutcome(
            artifact_refs=[
                ArtifactRef(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=(
                        f"jobs/{context.job_id}/stage_runs/"
                        f"{context.stage_run_id}/out.bin"
                    ),
                    media_type="application/octet-stream",
                )
            ]
        )


class _PublishStage(_Handler):
    name = "export"


@pytest.fixture()
def repo(tmp_path: Path):
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
    yield r
    r.close()


def _job() -> Job:
    now = datetime.now(tz=timezone.utc)
    return Job(
        job_id="job-1", document_id="sha256:doc",
        config_hash="cfg", created_at=now, updated_at=now,
    )


def test_fenced_worker_cannot_publish_or_write_status(repo: Repository, tmp_path: Path) -> None:
    handler = _Handler(lose_after=1)
    export = _PublishStage()
    state = {"lost": False}

    def guard() -> bool:
        state_calls = guard.calls  # type: ignore[attr-defined]
        guard.calls += 1  # type: ignore[attr-defined]
        # Lose the lease during the very first stage's execution: the guard
        # checks BEFORE each stage, so the first check passes and the stage
        # steals the lease; the next guard must fail.
        return state_calls < 1

    guard.calls = 0  # type: ignore[attr-defined]

    def lost_guard() -> bool:
        # Simulates: the stage ran, the lease was stolen mid-stage.
        return not getattr(handler, "lost", False) and guard.calls == 0

    runner = WorkflowRunner(repo, tmp_path, lease_guard=lost_guard)
    runner.register(handler)
    runner.register(export)
    result = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))

    assert result.status == "fenced"
    # No published manifest, status NOT succeeded.
    published = tmp_path / "jobs" / "job-1" / "published" / "result-manifest.json"
    assert not published.is_file()
    reloaded = repo.get_job("job-1")
    assert reloaded is not None and reloaded.status != "succeeded"


def test_stolen_lease_before_publish_aborts_publish(repo: Repository, tmp_path: Path) -> None:
    handler = _Handler()
    export = _PublishStage()
    checks = {"n": 0}

    def guard() -> bool:
        checks["n"] += 1
        # Pass for stage runs, fail right before the publish check (3rd call).
        return checks["n"] < 3

    runner = WorkflowRunner(repo, tmp_path, lease_guard=guard)
    runner.register(handler)
    runner.register(export)
    result = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))

    assert result.status == "fenced"
    published = tmp_path / "jobs" / "job-1" / "published" / "result-manifest.json"
    assert not published.is_file()


def test_tampered_manifest_with_matching_outputs_is_rejected(
    repo: Repository, tmp_path: Path
) -> None:
    """The manifest itself is hashed: rewriting it (and outputs) fails resume."""

    handler = _Handler()
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(handler)
    runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert handler.calls == 1

    # Tamper: change the manifest content and re-hash the output so the
    # (naive) output-only check would pass; the manifest hash must fail.
    manifest_path = next(
        (tmp_path / "jobs" / "job-1" / "stage_runs").glob("*native_parse*/stage.manifest.json")
    )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["warnings"] = ["tampered"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    handler2 = _Handler()
    resumed = WorkflowRunner(repo, tmp_path)
    resumed.register(handler2)
    result = resumed.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True)
    assert result.status in ("succeeded", "succeeded_with_review")
    assert handler2.calls == 1  # cache miss due to tampered manifest


def test_manifest_semantics_mismatch_invalidates_cache(
    repo: Repository, tmp_path: Path
) -> None:
    """A manifest whose cache_key does not match the current key re-runs."""

    handler = _Handler()
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(handler)
    runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))

    # Simulate a rules upgrade: implementation version changes → new cache
    # key → even an intact manifest for the OLD key must not be reused.
    class _V2(_Handler):
        def implementation_version(self) -> str:
            return "test-v2"

    handler2 = _V2()
    resumed = WorkflowRunner(repo, tmp_path)
    resumed.register(handler2)
    resumed.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True)
    assert handler2.calls == 1


def test_corrupted_published_manifest_does_not_fall_back(tmp_path: Path) -> None:
    """A present-but-corrupt published manifest is RESULT_CORRUPT, not legacy."""

    job_dir = tmp_path / "jobs" / "job-1"
    (job_dir / "validate").mkdir(parents=True)
    (job_dir / "validate" / "validation.json").write_text(
        json.dumps({"schema_version": "1.1.0", "sequences": [], "measurements": []}),
        encoding="utf-8",
    )
    published_dir = job_dir / "published"
    published_dir.mkdir()
    # Corrupt: invalid JSON.
    (published_dir / "result-manifest.json").write_text("{not json", encoding="utf-8")

    reader = ResultReader(tmp_path)
    with pytest.raises(WorkflowError, match="RESULT_CORRUPT"):
        reader.read_json("job-1", "validation")


def test_published_manifest_missing_entry_is_corrupt(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job-2"
    published_dir = job_dir / "published"
    published_dir.mkdir(parents=True)
    manifest = {
        "job_id": "job-2",
        "stages": {"validate": {"validation.json": None}},
    }
    (published_dir / "result-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    reader = ResultReader(tmp_path)
    with pytest.raises(WorkflowError, match="RESULT_CORRUPT"):
        reader.read_json("job-2", "validation")


def test_complete_stage_fenced_refuses_when_generation_reclaimed(
    repo: Repository, tmp_path: Path
) -> None:
    """The commit gate itself: gen-1 completes a stage AFTER gen-2 claimed."""

    from bio_pdf.storage.repository import LeaseToken

    claimed = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    assert claimed is not None
    token1 = LeaseToken.from_claim(claimed)

    # Reclaim (simulating a crash/timeout) by w2: generation bumps to 2.
    repo.connection.execute(
        "UPDATE worker_leases SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = 'job-1'"
    )
    claimed2 = repo.claim_next_queued_job(worker_id="w2", lease_seconds=600)
    assert claimed2 is not None and claimed2.generation == 2

    # A stale stage-run row from generation 1.
    from bio_pdf.contracts import Stage

    stage = Stage(
        stage_run_id="job-1--native_parse--a1--deadbeef",
        job_id="job-1",
        stage_name="native_parse",
        attempt=1,
        status="running",
    )
    repo.start_stage(stage)

    # gen-1 tries to complete it: refused, nothing written.
    ok = repo.complete_stage_fenced(
        stage.stage_run_id, token=token1, artifact_manifest_ref="x"
    )
    assert ok is False
    row = repo.connection.execute(
        "SELECT status FROM stage_runs WHERE stage_run_id = ?", (stage.stage_run_id,)
    ).fetchone()
    assert row["status"] == "running"

    # gen-2 may complete its own stage runs only with its own token.
    token2 = LeaseToken.from_claim(claimed2)
    stage2 = Stage(
        stage_run_id="job-1--native_parse--a1--cafe0000",
        job_id="job-1",
        stage_name="native_parse",
        attempt=1,
        status="running",
    )
    repo.start_stage(stage2)
    assert repo.complete_stage_fenced(stage2.stage_run_id, token=token2, artifact_manifest_ref="x")


def test_publish_fenced_refuses_stale_token_and_writes_nothing(
    repo: Repository, tmp_path: Path
) -> None:
    from bio_pdf.storage.repository import LeaseToken

    claimed = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    assert claimed is not None
    token1 = LeaseToken.from_claim(claimed)
    repo.connection.execute(
        "UPDATE worker_leases SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = 'job-1'"
    )
    claimed2 = repo.claim_next_queued_job(worker_id="w2", lease_seconds=600)
    assert claimed2 is not None

    ok = repo.publish_result_fenced(
        token=token1,
        job_id="job-1",
        final_status="succeeded",
        manifest_rel="jobs/job-1/published/x.json",
        manifest_sha="a" * 64,
        review_items=[],
    )
    assert ok is False
    job_row = repo.get_job("job-1")
    assert job_row is not None and job_row.status != "succeeded"
    assert repo.get_publication("job-1") is None
