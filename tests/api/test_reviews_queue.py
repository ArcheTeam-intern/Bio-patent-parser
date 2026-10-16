"""Review queue listing: true totals, pagination, and per-record lookup.

The queue can hold hundreds of items (every cell of a reconstructed
susceptibility matrix is one). A truncated first page must never present
itself as the whole queue, and the workbench panel must find the review
item of ANY record — not only records that happen to be on page one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings


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
def completed_job(tmp_path: Path):
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
        yield c, job["job_id"]


def test_total_is_the_true_count_not_the_page_size(completed_job) -> None:
    c, job_id = completed_job
    full = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    assert full["total"] >= 2, full
    assert full["total"] == len(full["reviews"])

    # A page of one still reports the whole queue's size.
    page = c.get(f"/api/v1/reviews?job_id={job_id}&limit=1").json()
    assert page["total"] == full["total"]
    assert len(page["reviews"]) == 1

    # Offsets page through without repeats.
    second = c.get(f"/api/v1/reviews?job_id={job_id}&limit=1&offset=1").json()
    assert second["reviews"][0]["review_id"] != page["reviews"][0]["review_id"]


def test_record_id_filter_finds_the_review_item_directly(completed_job) -> None:
    c, job_id = completed_job
    full = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    # Pick a record that is NOT the first entry of the queue.
    target = full["reviews"][-1]["record_id"]

    res = c.get(f"/api/v1/reviews?job_id={job_id}&record_id={target}").json()
    assert res["total"] == 1
    assert len(res["reviews"]) == 1
    assert res["reviews"][0]["record_id"] == target

    # Unknown record: honest empty result, not an error.
    empty = c.get(f"/api/v1/reviews?job_id={job_id}&record_id=no-such-record").json()
    assert empty["total"] == 0
    assert empty["reviews"] == []


def test_queue_reports_groups_over_the_whole_filtered_queue(completed_job) -> None:
    """C06: counts come as records AND groups; groups cover the whole queue."""

    c, job_id = completed_job
    body = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    assert body["groups"], "filtered queue must carry group counts"
    grouped = sum(g["count"] for g in body["groups"])
    assert grouped == body["total"], "group counts must cover the whole queue"
    # Every group has a stable key and a human label.
    for g in body["groups"]:
        assert g["group_id"].startswith(job_id)
        assert g["label"]


def test_batch_decision_precheck_fails_all_on_one_stale_revision(completed_job) -> None:
    """C06: one stale revision fails the whole batch — nothing applied."""

    c, job_id = completed_job
    items = c.get(f"/api/v1/reviews?job_id={job_id}").json()["reviews"]
    assert len(items) >= 2
    first, second = items[0], items[1]

    # Live revisions of both records.
    def live_revision(review_id: str) -> int:
        return c.get(f"/api/v1/reviews/{review_id}/decisions").status_code == 200 and _record_revision(c, job_id, review_id)

    def _record_revision(c, job_id, review_id):  # pragma: no cover - helper alias
        return 1

    from bio_pdf.api.app import create_app  # noqa: F401  (context only)

    # Fetch record revisions via the workbench records endpoint.
    records = c.get(f"/api/v1/jobs/{job_id}/records").json()
    rev_by_id = {r["sequence_id"]: r.get("_revision", 1) for r in records["sequences"]}
    rev_by_id.update({r["measurement_id"]: r.get("_revision", 1) for r in records["measurements"]})

    def base_for(item):
        return int(rev_by_id.get(item["record_id"], 1))

    # One good + one stale (base 999) — precheck must reject the batch.
    res = c.post(
        "/api/v1/reviews/decisions/batch",
        json={
            "actor": "tester",
            "decisions": [
                {"review_id": first["review_id"], "action": "confirm", "base_revision": base_for(first)},
                {"review_id": second["review_id"], "action": "confirm", "base_revision": 999},
            ],
        },
    )
    assert res.status_code == 409, res.text
    detail = res.json()["error"]
    assert detail["code"] == "REVIEW_BATCH_CONFLICT"
    conflicts = detail["details"]["conflicts"]
    assert any(cf["review_id"] == second["review_id"] for cf in conflicts)

    # Nothing was applied: both still open.
    remaining = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    ids = {r["review_id"] for r in remaining["reviews"]}
    assert first["review_id"] in ids and second["review_id"] in ids


def test_batch_missing_reason_rolls_back_everything(completed_job) -> None:
    """D01 / S01 exact repro: item 1 valid, item 2 lacks a reject reason →
    NOTHING may be written (previously item 1 was already committed)."""

    c, job_id = completed_job
    items = c.get(f"/api/v1/reviews?job_id={job_id}").json()["reviews"]
    assert len(items) >= 2
    first, second = items[0], items[1]

    # Assertions run through the API-visible statuses and the decisions log
    # — no direct DB access needed.
    res = c.post(
        "/api/v1/reviews/decisions/batch",
        json={
            "actor": "tester",
            "decisions": [
                {
                    "review_id": first["review_id"],
                    "action": "reject",
                    "base_revision": first["record_revision"],
                    "reason": "not our target",
                },
                {
                    "review_id": second["review_id"],
                    "action": "reject",
                    "base_revision": second["record_revision"],
                    # no reason -> must fail the WHOLE batch
                },
            ],
        },
    )
    assert res.status_code == 422, res.text
    assert "reject requires a reason" in res.json()["error"]["message"]

    remaining = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    ids = {r["review_id"] for r in remaining["reviews"]}
    assert first["review_id"] in ids and second["review_id"] in ids, (
        "rollback must leave BOTH items open"
    )
    for rid in (first["review_id"], second["review_id"]):
        decisions = c.get(f"/api/v1/reviews/{rid}/decisions").json()["decisions"]
        assert decisions == [], f"{rid} must have no decision log entries"

    # The success path still writes everything in one go.
    ok = c.post(
        "/api/v1/reviews/decisions/batch",
        json={
            "actor": "tester",
            "decisions": [
                {
                    "review_id": first["review_id"],
                    "action": "reject",
                    "base_revision": first["record_revision"],
                    "reason": "not our target",
                },
                {
                    "review_id": second["review_id"],
                    "action": "reject",
                    "base_revision": second["record_revision"],
                    "reason": "also noise",
                },
            ],
        },
    )
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["applied"]) == 2
    for rid in (first["review_id"], second["review_id"]):
        decisions = c.get(f"/api/v1/reviews/{rid}/decisions").json()["decisions"]
        assert len(decisions) == 1


def test_batch_rejects_duplicates_before_writing(completed_job) -> None:
    """D01: duplicate review_id / same-record duplicates are refused up front."""

    c, job_id = completed_job
    items = c.get(f"/api/v1/reviews?job_id={job_id}").json()["reviews"]
    first = items[0]

    dup_ids = c.post(
        "/api/v1/reviews/decisions/batch",
        json={
            "actor": "tester",
            "decisions": [
                {"review_id": first["review_id"], "action": "confirm", "base_revision": 1},
                {"review_id": first["review_id"], "action": "confirm", "base_revision": 1},
            ],
        },
    )
    assert dup_ids.status_code == 422
    assert dup_ids.json()["error"]["details"].get("code") == "BATCH_DUPLICATE"

    # Same record via two different review items (if present) — create the
    # situation directly using two items of the same record when available.
    same_record = [r for r in items if r["record_id"] == first["record_id"]]
    if len(same_record) >= 2:
        dup_record = c.post(
            "/api/v1/reviews/decisions/batch",
            json={
                "actor": "tester",
                "decisions": [
                    {"review_id": same_record[0]["review_id"], "action": "confirm", "base_revision": 1},
                    {"review_id": same_record[1]["review_id"], "action": "confirm", "base_revision": 1},
                ],
            },
        )
        assert dup_record.status_code == 422
        assert dup_record.json()["error"]["details"].get("code") == "BATCH_SAME_RECORD"
