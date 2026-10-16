"""E1/S01: review identity is bound to the baseline (real publish path).

The acceptance repro: an old browser request must NOT operate on a new
baseline's record. Regression goes through the REAL build_review_plan and
publish_result_fenced functions — no hand-made review ids.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings
from bio_pdf.contracts import ReviewItem
from bio_pdf.review.plan import build_review_plan
from bio_pdf.storage.repository import Repository


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in [
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
        "",
        "Example 1: The IC50 was 1-10 nM.",
    ]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


@pytest.fixture()
def published_job(tmp_path: Path):
    settings = ApiSettings(
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        max_upload_bytes=10 * 1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        pdf = _sample_pdf(tmp_path / "paper.pdf")
        doc = c.post(
            "/api/v1/documents",
            files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
        ).json()
        job = c.post(
            "/api/v1/jobs",
            json={"document_id": doc["document_id"], "parser": "native"},
        ).json()
        from bio_pdf.workflow.queue_worker import run_worker

        run_worker(tmp_path / "data", once=True)
        yield c, tmp_path, job["job_id"], doc["document_id"]


def _items_of(c: TestClient, job_id: str) -> list[dict]:
    body = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    return body["reviews"]


def _republish_with_new_baseline(
    tmp_path: Path, job_id: str, record_id: str, baseline_b: str
) -> str:
    """Publish a NEW baseline for the same record through the real path."""

    repo = Repository(tmp_path / "data" / "bio.sqlite")
    items = build_review_plan(
        [
            {
                "record_id": record_id,
                "job_id": job_id,
                "validation_state": "conflict",
                "reason_codes": ["VALIDATION_CONFLICT"],
                "evidence_ids": [],
                "record_revision": 1,
            }
        ],
        budget=0,
        baseline_id=baseline_b,
    )
    assert items and items[0].baseline_id == baseline_b
    ok = repo.publish_result_fenced(
        token=None,
        job_id=job_id,
        final_status="succeeded_with_review",
        manifest_rel=f"jobs/{job_id}/published/result-manifest-B.json",
        manifest_sha="b" * 40,
        review_items=items,
        issues=[],
        baseline_id=baseline_b,
    )
    repo.close()
    assert ok
    return items[0].review_id


def test_old_baseline_request_cannot_touch_new_baseline(published_job) -> None:
    c, tmp_path, job_id, document_id = published_job

    items_a = _items_of(c, job_id)
    assert items_a, "baseline A must have review items"
    old = items_a[0]
    review_id_a = old["review_id"]
    record_id = old["record_id"]
    baseline_a = old["baseline_id"]
    assert baseline_a, "items from the real publish carry a baseline"

    baseline_b = "b" * 16
    review_id_b = _republish_with_new_baseline(tmp_path, job_id, record_id, baseline_b)
    assert review_id_b != review_id_a, (
        "the same record under a new baseline must get a NEW review id — "
        "ON CONFLICT must never rebind an old id onto new content"
    )

    # The old item is superseded, and a decision carrying the OLD baseline
    # is refused with 409 + zero writes.
    stale = c.post(
        f"/api/v1/reviews/{review_id_a}/decisions",
        json={
            "action": "reject",
            "base_revision": 1,
            "actor": "stale-page",
            "reason": "old window",
            "baseline_id": baseline_a,
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] in (
        "REVIEW_BASELINE_CONFLICT",
        "REVIEW_REVISION_CONFLICT",
    )

    # Even WITHOUT the expected baseline, the stale item cannot be decided:
    # its own stored baseline no longer matches the live one.
    stale2 = c.post(
        f"/api/v1/reviews/{review_id_a}/decisions",
        json={"action": "reject", "base_revision": 1, "actor": "stale-page", "reason": "x"},
    )
    assert stale2.status_code == 409, stale2.text
    assert stale2.json()["error"]["code"] == "REVIEW_BASELINE_CONFLICT"

    # The stale item remains superseded with no decision log; the new
    # baseline's item stays open (history preserved, nothing rebound).
    queue = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    ids = {r["review_id"]: r for r in queue["reviews"]}
    assert review_id_b in ids
    assert review_id_a not in ids  # superseded is not open

    decided = c.get(f"/api/v1/reviews/{review_id_a}/decisions").json()["decisions"]
    assert decided == [], "zero decision history may be written for the stale item"

    # Current-baseline decisions still work.
    fresh = c.post(
        f"/api/v1/reviews/{review_id_b}/decisions",
        json={
            "action": "confirm",
            "base_revision": 1,
            "actor": "reviewer",
            "baseline_id": baseline_b,
        },
    )
    # blockers may refuse acceptance — that's the acceptance contract, not a
    # baseline failure; either way the status must not be a baseline 409.
    if fresh.status_code == 422:
        assert "blockers" in fresh.text
    else:
        assert fresh.status_code == 200, fresh.text


def test_build_review_plan_ids_are_baseline_scoped() -> None:
    records = [
        {
            "record_id": "r1",
            "job_id": "job-1",
            "validation_state": "conflict",
            "reason_codes": ["VALIDATION_CONFLICT"],
            "evidence_ids": [],
        }
    ]
    a = build_review_plan(records, budget=0, baseline_id="a" * 16)
    b = build_review_plan(records, budget=0, baseline_id="b" * 16)
    assert a[0].review_id != b[0].review_id
    assert a[0].baseline_id == "a" * 16
    # Same baseline twice stays stable (idempotent re-plan).
    a2 = build_review_plan(records, budget=0, baseline_id="a" * 16)
    assert a2[0].review_id == a[0].review_id