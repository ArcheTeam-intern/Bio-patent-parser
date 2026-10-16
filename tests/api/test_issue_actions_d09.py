"""D09: real issue locations, corrupt results surface, actions with audit.

Acceptance repros covered:
- a table issue with id ``7:t0`` locates page 7 (the id prefix is OUR
  documented convention, and new issues carry the page directly);
- a corrupted published artifact returns 409 RESULT_CORRUPT instead of an
  empty "no result yet" list;
- dismiss requires a reason; every action is logged with actor + note;
- bounded reparse stores a candidate artifact and marks the issue, or is
  refused with a reason when the scope has no such path.
"""

from __future__ import annotations

import json
import sqlite3
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
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


@pytest.fixture()
def client(tmp_path: Path):
    settings = ApiSettings(
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        max_upload_bytes=10 * 1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, tmp_path


def _seed_issue(c: TestClient, tmp_path: Path, *, reason_code: str, source_ids: list[str], detail: str) -> tuple[str, str]:
    pdf = _sample_pdf(tmp_path / "i.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("i.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "native"},
    ).json()
    job_id = job["job_id"]
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.execute(
        "INSERT INTO parse_issues (issue_id, job_id, scope, reason_code, source_json, detail, state, created_at)"
        " VALUES ('iss-d09', ?, 'table', ?, ?, ?, 'open', '2026-10-16T00:00:00+00:00')",
        (job_id, reason_code, json.dumps(source_ids), detail),
    )
    conn.commit()
    conn.close()
    return job_id, "iss-d09"


def test_table_issue_with_docling_id_prefix_locates_page(client) -> None:
    """Acceptance repro: id '7:t0' must locate page 7 (documented shape)."""

    c, tmp_path = client
    job_id, issue_id = _seed_issue(
        c,
        tmp_path,
        reason_code="TABLE_STRUCTURE",
        source_ids=["7:t0"],
        detail="TABLE_GRID_OVERLAP at r0c0",
    )
    body = c.get(f"/api/v1/jobs/{job_id}/issues?page_number=7").json()
    assert body["total"] == 1
    assert body["issues"][0]["page_number"] == 7

    single = c.get(f"/api/v1/jobs/{job_id}/issues/{issue_id}").json()
    assert single["issue"]["page_number"] == 7
    assert single["events"] == []


def test_corrupt_published_result_is_409_not_empty(client, tmp_path: Path) -> None:
    """Acceptance repro: tampering with a published artifact must surface as
    RESULT_CORRUPT, never as an empty figures list with total=0."""

    c, _ = client
    pdf = _sample_pdf(tmp_path / "corrupt.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("corrupt.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "native"},
    ).json()
    job_id = job["job_id"]

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)

    # Figures work while the artifact is intact.
    ok = c.get(f"/api/v1/jobs/{job_id}/figures")
    assert ok.status_code == 200

    # Locate the published normalized artifact and corrupt its bytes.
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    row = conn.execute(
        "SELECT manifest_rel, manifest_sha256 FROM publications WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    manifest = json.loads((tmp_path / "data" / row[0]).read_text(encoding="utf-8"))
    conv = manifest["stages"]["normalize"]["normalized.json"]
    artifact_path = tmp_path / "data" / conv["relative_path"]
    conn.close()
    original = artifact_path.read_text(encoding="utf-8")
    artifact_path.write_text(original + " ", encoding="utf-8")  # sha mismatch

    res = c.get(f"/api/v1/jobs/{job_id}/figures")
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "RESULT_CORRUPT"


def test_actions_require_reason_and_are_logged(client) -> None:
    c, tmp_path = client
    job_id, issue_id = _seed_issue(
        c,
        tmp_path,
        reason_code="SEQ_INVENTORY_MISSING",
        source_ids=["st25:7"],
        detail="SEQ ID 7 (page 32): region reached, no residues",
    )

    # dismiss without a reason is refused.
    res = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/actions",
        json={"action": "dismiss"},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ISSUE_NOTE_REQUIRED"
    still_open = c.get(f"/api/v1/jobs/{job_id}/issues/{issue_id}").json()
    assert still_open["issue"]["state"] == "open"
    assert still_open["events"] == []

    # dismiss with a reason logs the event and changes state.
    res = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/actions",
        json={"action": "dismiss", "note": "背景药敏表，非目标", "actor": "reviewer-1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["issue"]["state"] == "dismissed"
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["action"] == "dismiss"
    assert ev["actor"] == "reviewer-1"
    assert ev["note"] == "背景药敏表，非目标"

    # note reopens the dismissed issue with context.
    res = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/actions",
        json={"action": "note", "note": "等 OCR 升档后再看"},
    )
    assert res.status_code == 200
    assert res.json()["issue"]["state"] == "open"
    assert len(res.json()["events"]) == 2


def test_bounded_reparse_stores_a_candidate(client, monkeypatch) -> None:
    c, tmp_path = client
    job_id, issue_id = _seed_issue(
        c,
        tmp_path,
        reason_code="SEQ_INVENTORY_MISSING",
        source_ids=["st25:7"],
        detail="SEQ ID 7 (page 1): region reached, no residues",
    )

    def fake_ocr(pdf_path, pages):  # noqa: ANN001
        # region_ocr shape (E5): (x0, y0, x1, y1, text)
        return {int(pages[0]): [(40.0, 100.0, 120.0, 110.0, "MKTG AcGT")]}

    monkeypatch.setattr(
        "bio_pdf.extract.sequences.region_ocr.ocr_residue_pages", fake_ocr
    )

    res = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["candidate"]["line_count"] == 1
    assert "MKTG" in body["candidate"]["preview"]
    assert body["issue"]["state"] == "reparse_requested"
    assert any(e["action"] == "reparse_candidate" for e in body["events"])
    assert body["candidate"]["artifact_rel"].startswith(f"jobs/{job_id}/issues/")
    assert (tmp_path / "data" / body["candidate"]["artifact_rel"]).is_file()


def test_reparse_refuses_unsupported_scope_with_reason(client) -> None:
    c, tmp_path = client
    job_id, issue_id = _seed_issue(
        c,
        tmp_path,
        reason_code="TABLE_SCORING",
        source_ids=["8:t0"],
        detail="page 8: classified scoring; structure preserved",
    )
    res = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ISSUE_REPARSE_UNSUPPORTED"

def test_issue_endpoints_enforce_job_ownership(client) -> None:
    """E4/S04: an issue from job A cannot be read, mutated or reparsed
    through job B's URLs — 404 with zero writes."""

    c, tmp_path = client
    import sqlite3

    def _new_job(tag: str) -> str:
        pdf = _sample_pdf(tmp_path / f"{tag}.pdf")
        doc = c.post(
            "/api/v1/documents",
            files={"file": (f"{tag}.pdf", pdf.read_bytes(), "application/pdf")},
        ).json()
        return c.post(
            "/api/v1/jobs",
            json={"document_id": doc["document_id"], "parser": "native"},
        ).json()["job_id"]

    job_a = _new_job("own-a")
    job_b = _new_job("own-b")

    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.execute(
        "INSERT INTO parse_issues (issue_id, job_id, scope, reason_code, source_json, detail, state, created_at)"
        " VALUES ('iss-owned', ?, 'table', 'TABLE_SCORING', '[]', 'page 8: scoring', 'open', '2026-10-16T00:00:00+00:00')",
        (job_a,),
    )
    conn.commit()
    conn.close()

    # reads
    assert c.get(f"/api/v1/jobs/{job_b}/issues/iss-owned").status_code == 404
    assert c.get(f"/api/v1/jobs/{job_a}/issues/iss-owned").status_code == 200

    # actions must not cross jobs
    res = c.post(
        f"/api/v1/jobs/{job_b}/issues/iss-owned/actions",
        json={"action": "dismiss", "note": "wrong job"},
    )
    assert res.status_code == 404
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    state = conn.execute(
        "SELECT state FROM parse_issues WHERE issue_id = 'iss-owned'"
    ).fetchone()[0]
    events = conn.execute(
        "SELECT COUNT(*) FROM issue_events WHERE issue_id = 'iss-owned'"
    ).fetchone()[0]
    conn.close()
    assert state == "open", "cross-job action must leave zero writes"
    assert events == 0

    # reparse must not cross jobs either (it would mix source + page)
    rep = c.post(f"/api/v1/jobs/{job_b}/issues/iss-owned/reparse")
    assert rep.status_code == 404


def test_second_reparse_never_overwrites_the_first_candidate(client, monkeypatch) -> None:
    """E3/S03: each candidate is its own immutable attempt."""

    c, tmp_path = client
    job_id, issue_id = _seed_issue(
        c, tmp_path,
        reason_code="SEQ_INVENTORY_MISSING",
        source_ids=["st25:7"],
        detail="SEQ ID 7 (page 1): region reached, no residues",
    )

    from bio_pdf.extract.sequences import region_ocr

    responses = []
    texts = iter([
        [(40.0, 100.0, 120.0, 110.0, "FIRST-CANDIDATE")],
        [(40.0, 100.0, 120.0, 110.0, "SECOND-CANDIDATE")],
    ])

    def fake_ocr(pdf_path, pages):  # noqa: ANN001
        return {int(pages[0]): next(texts)}

    monkeypatch.setattr(region_ocr, "ocr_residue_pages", fake_ocr)

    first = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["candidate"]["attempt"] == 1
    assert "FIRST" in first_body["candidate"]["preview"]
    first_rel = first_body["candidate"]["artifact_rel"]
    first_sha = first_body["candidate"]["content_sha256"]

    second = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    second_body = second.json()
    assert second_body["candidate"]["attempt"] == 2
    assert second_body["candidate"]["artifact_rel"] != first_rel

    # The FIRST event's artifact is byte-identical to what it recorded.
    path1 = tmp_path / "data" / first_rel
    assert "FIRST-CANDIDATE" in path1.read_text(encoding="utf-8")
    import hashlib as _h

    assert _h.sha256(path1.read_bytes()).hexdigest() == first_sha
    events = c.get(f"/api/v1/jobs/{job_id}/issues/{issue_id}").json()["events"]
    rels = [e["artifact_rel"] for e in events if e["action"] == "reparse_candidate"]
    assert rels == [first_rel, second_body["candidate"]["artifact_rel"]]
    # Each event's note carries its own sha so a replay is checkable.
    assert all("sha256=" in (e["note"] or "") for e in events if e["action"] == "reparse_candidate")


def test_adopt_creates_a_new_formal_job_not_an_inplace_edit(client, monkeypatch) -> None:
    """E3/S03: adoption = a NEW job/baseline; history kept, nothing mutated."""

    c, tmp_path = client
    job_id, issue_id = _seed_issue(
        c, tmp_path,
        reason_code="SEQ_INVENTORY_MISSING",
        source_ids=["st25:7"],
        detail="SEQ ID 7 (page 1): region reached, no residues",
    )
    from bio_pdf.extract.sequences import region_ocr

    monkeypatch.setattr(
        region_ocr, "ocr_residue_pages",
        lambda pdf_path, pages: {int(pages[0]): [(40.0, 100.0, 120.0, 110.0, "ADOPT ME")]},
    )
    rep = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert rep.status_code == 200

    # Without a candidate on a DIFFERENT issue, adoption refuses.
    other = c.post(f"/api/v1/jobs/{job_id}/issues/iss-does-not-exist/adopt", json={})
    assert other.status_code == 404

    adopted = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt", json={"actor": "reviewer"})
    assert adopted.status_code == 201, adopted.text
    body = adopted.json()
    new_job_id = body["adopted"]["new_job_id"]
    assert new_job_id and new_job_id != job_id
    assert body["adopted"]["pages"] == [1]
    assert any(e["action"] == "adopted" for e in body["events"])

    # The new job is independent and queued — the ORIGINAL job's state and
    # baseline are untouched.
    original = c.get(f"/api/v1/jobs/{job_id}").json()
    assert original["job_id"] == job_id
    new = c.get(f"/api/v1/jobs/{new_job_id}").json()
    assert new["status"] in ("queued", "running")

    events = c.get(f"/api/v1/jobs/{job_id}/issues/{issue_id}").json()["events"]
    adopted_events = [e for e in events if e["action"] == "adopted"]
    assert adopted_events and new_job_id in (adopted_events[0]["note"] or "")
