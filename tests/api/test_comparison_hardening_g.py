"""G02–G05: comparison reliability hardening (fourth-round recheck).

- G02/S02: every DB operation rides its OWN connection — a heartbeat can
  never commit (or roll back) another thread's claim transaction.
- G03/S03: the execution budget is real — the supervised unit is
  terminated at expiry (child process), renewals stop first, and a late
  unit can never publish.
- G04/S04: the overall status is derived inside one UPDATE, so a stale
  reader can never write 'running' back after both sides finished.
- G05/S05: the cloud identity lives at the SIDE level (shared across
  attempts); a retry after a download failure polls the SAME batch and
  never re-uploads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from bio_pdf.application.comparison_service import ComparisonService
from bio_pdf.storage.repository import Repository


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


def _normalized_doc(document_id: str, *, sequence: str) -> dict:
    return {
        "schema_version": "1.0",
        "document_id": document_id,
        "parser_run_id": "stub:engine",
        "pages": [
            {
                "page_number": 1,
                "width_pt": 595.0,
                "height_pt": 842.0,
                "route": "native",
                "blocks": [
                    {"kind": "text", "semantic_role": "unknown",
                     "text_raw": "<210> 1\n<211> 4\n<212> PRT\n<400> 1\n" + sequence,
                     "bbox": [50.0, 100.0, 400.0, 112.0], "block_id": "b1"},
                ],
                "tables": [],
                "figures": [],
            }
        ],
        "errors": [],
    }


class _StubAdapter:
    def __init__(self, sequence: str, document_id: str, *, fail: bool = False) -> None:
        self.sequence = sequence
        self.document_id = document_id
        self.fail = fail

    def parse(self, request, **kwargs):  # noqa: ANN001, ANN003
        from bio_pdf.errors import WorkflowError
        from bio_pdf.parsers.base import ParseArtifacts

        if self.fail:
            raise WorkflowError("stub side failure", details={"code": "STUB_FAIL"})
        out = Path(request.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        norm = out / "normalized.json"
        norm.write_text(
            json.dumps(_normalized_doc(self.document_id, sequence=self.sequence)),
            encoding="utf-8",
        )
        (out / "raw_response.json").write_text("{}", encoding="utf-8")
        manifest = out / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return ParseArtifacts(
            normalized_path=norm, raw_response_path=out / "raw_response.json", manifest_path=manifest
        )


# ---- G02/S02: per-operation connections -----------------------------------


def test_db_operations_work_from_foreign_threads(tmp_path) -> None:
    """G02/S02: claim/renew/publish from a different thread than the one
    that constructed the service. With a shared connection this raised
    ``sqlite3.ProgrammingError`` (or let another thread's commit swallow
    the transaction); with per-operation connections every call opens its
    own handle in the calling thread."""

    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    svc = ComparisonService(
        # NOTE: default check_same_thread=True — deliberately hostile to
        # cross-thread use of THIS connection, proving the service does not
        # use it for its operations anymore.
        repository=Repository(tmp_path / "data" / "bio.sqlite"),
        artifact_root=tmp_path / "data",
        lease_seconds=3600.0,
        run_mode="thread",
    )
    cid = svc.create(
        document_id="doc-x", source_path=pdf, pages=[1], engines=["docling"]
    )

    errors: list[BaseException] = []
    results: dict[str, object] = {}

    def _work() -> None:
        try:
            results["claimed"] = svc._claim_side(cid, "docling")
            token = results["claimed"][0]  # type: ignore[index]
            results["renewed"] = svc._renew_lease(cid, "docling", token)
            results["published"] = svc._publish_side_result(
                cid, "docling", token, status="done", summary_json="{}"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_work)
    t.start()
    t.join(timeout=15)
    assert not errors, errors
    assert results["claimed"] is not None
    assert results["renewed"] is True
    assert results["published"] is True
    svc.repository.close()


def test_writer_lock_serializes_foreign_transactions(tmp_path) -> None:
    """G02/S02: a foreign writer transaction blocks another connection's
    write until it commits — transactions are serialized by SQLite, never
    interleaved through a shared handle (the old failure: renew's commit
    ended the claim transaction mid-flight)."""

    db = tmp_path / "data" / "bio.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    svc = ComparisonService(
        repository=Repository(db, check_same_thread=False),
        artifact_root=tmp_path / "data",
        lease_seconds=3600.0,
        run_mode="thread",
    )
    cid = svc.create(
        document_id="doc-y", source_path=pdf, pages=[1], engines=["docling"]
    )
    token, _attempt = svc._claim_side(cid, "docling")

    # Hold a writer transaction on a foreign connection…
    foreign = sqlite3.connect(db, timeout=30.0)
    foreign.execute("BEGIN IMMEDIATE")
    foreign.execute(
        "UPDATE comparison_sides SET lease_until = '2099-01-01T00:00:00+00:00'"
        " WHERE comparison_id = ? AND side = 'docling'",
        (cid,),
    )

    renew_result: dict[str, object] = {}

    def _renew() -> None:
        # …and renew from another thread: it must WAIT for the lock, not
        # mix into the foreign transaction.
        renew_result["ok"] = svc._renew_lease(cid, "docling", token)

    t = threading.Thread(target=_renew)
    t.start()
    time.sleep(0.3)
    assert "ok" not in renew_result, "renew must be blocked by the foreign writer transaction"
    foreign.commit()
    foreign.close()
    t.join(timeout=10)
    assert renew_result.get("ok") is True
    svc.repository.close()


def test_concurrent_recovery_of_multiple_comparisons_is_clean(tmp_path) -> None:
    """G02/S02 acceptance: one service (the sweeper pattern) runs SEVERAL
    comparisons concurrently with heartbeats — no transaction crosstalk, no
    running-without-execution, every side exactly one attempt."""

    from bio_pdf.application import comparison_service as cs

    calls: list[str] = []
    lock = threading.Lock()

    def factory(engine, source_path, request):  # noqa: ANN001
        with lock:
            calls.append(engine)
        time.sleep(0.05)  # widen the window for interleaving
        return _StubAdapter("Ala Gly Leu Lys", "doc-z")

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(cs, "_side_engine", factory)
    try:
        pdf = _sample_pdf(tmp_path / "cmp.pdf")
        svc = ComparisonService(
            repository=Repository(tmp_path / "data" / "bio.sqlite", check_same_thread=False),
            artifact_root=tmp_path / "data",
            lease_seconds=0.4,
            heartbeat_interval=0.05,
            run_mode="thread",
        )
        ids = [
            svc.create(
                document_id=f"doc-{i}",
                source_path=pdf,
                pages=[1],
                engines=["docling", "mineru_http"],
            )
            for i in range(3)
        ]
        for cid in ids:
            svc.start(cid)
        deadline = time.time() + 30
        while time.time() < deadline:
            infos = [svc.get(cid) for cid in ids]
            if all(all(s["status"] == "done" for s in info["sides"]) for info in infos):
                break
            time.sleep(0.05)
        for info in [svc.get(cid) for cid in ids]:
            assert all(s["status"] == "done" for s in info["sides"]), info
            assert all(s["attempt"] == 1 for s in info["sides"]), info
            assert info["status"] == "done"
        assert len(calls) == 6, calls
        svc.repository.close()
    finally:
        mp.undo()


# ---- G04/S04: overall status is derived inside one statement --------------


def test_overall_status_refresh_never_regresses(tmp_path) -> None:
    """G04/S04: after both sides finish, a refresh derives from CURRENT
    rows inside a single UPDATE — the old read-then-write gap (which could
    write 'running' back over a fresh 'done') no longer exists."""

    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    svc = ComparisonService(
        repository=Repository(tmp_path / "data" / "bio.sqlite", check_same_thread=False),
        artifact_root=tmp_path / "data",
        run_mode="thread",
    )
    cid = svc.create(
        document_id="doc-s", source_path=pdf, pages=[1],
        engines=["docling", "mineru_http"],
    )
    for side in ("docling", "mineru_http"):
        token, _ = svc._claim_side(cid, side)
        svc._publish_side_result(cid, side, token, status="done", summary_json="{}")

    # A stale value written externally (the recheck's regression) is
    # immediately corrected by the derivation; concurrent refreshes and a
    # re-refresh can never bring 'running' back.
    conn = svc._connect()
    conn.execute("UPDATE comparisons SET status = 'running' WHERE comparison_id = ?", (cid,))
    conn.commit()
    conn.close()

    threads = [
        threading.Thread(target=svc._refresh_status, args=(cid,)) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    svc._refresh_status(cid)
    assert svc.get(cid)["status"] == "done"
    svc.repository.close()


# ---- G03/S03: real execution budget ---------------------------------------


def test_thread_mode_budget_fences_and_stops_renewals(tmp_path) -> None:
    """G03/S03 (fast, stub): a hanging execution hits the budget, the side
    is fenced failed, renewals stop, and the late completion can never
    publish."""

    from bio_pdf.application import comparison_service as cs

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def factory(engine, source_path, request):  # noqa: ANN001
        calls.append(engine)
        entered.set()
        release.wait(timeout=10)
        return _StubAdapter("Ala Gly Leu Lys", "doc-b")

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(cs, "_side_engine", factory)
    try:
        pdf = _sample_pdf(tmp_path / "cmp.pdf")
        svc = ComparisonService(
                repository=Repository(tmp_path / "data" / "bio.sqlite", check_same_thread=False),
                artifact_root=tmp_path / "data",
                lease_seconds=0.3,
                heartbeat_interval=0.05,
                execution_budget_seconds=0.3,
                run_mode="thread",
            )
        cid = svc.create(
            document_id="doc-b", source_path=pdf, pages=[1], engines=["docling"]
        )
        started = time.monotonic()
        svc.start(cid)
        assert entered.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            side = svc.get(cid)["sides"][0]
            if side["status"] == "failed":
                break
            time.sleep(0.02)
        elapsed = time.monotonic() - started
        side = svc.get(cid)["sides"][0]
        assert side["status"] == "failed", side
        assert "EXECUTION_BUDGET_EXCEEDED" in (side["error"] or "")
        assert elapsed < 5.0, f"budget must fence promptly, took {elapsed:.1f}s"
        assert side["lease_until"] is None, "renewals must have stopped at the fence"

        # The execution is still hanging; letting it finish must change
        # NOTHING (the parent owns the completion write).
        release.set()
        time.sleep(0.5)
        side = svc.get(cid)["sides"][0]
        assert side["status"] == "failed"
        assert side["summary"] == {}
        svc.repository.close()
    finally:
        mp.undo()


@pytest.mark.slow
def test_process_mode_budget_terminates_a_real_child(tmp_path) -> None:
    """G03/S03: REAL independent-process scenario — a Docling side in a
    spawned child cannot possibly finish within a tiny budget; the watchdog
    terminates the child and the side reaches its terminal state on time
    with no background residue."""

    import multiprocessing as mp

    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    svc = ComparisonService(
        repository=Repository(tmp_path / "data" / "bio.sqlite", check_same_thread=False),
        artifact_root=tmp_path / "data",
        lease_seconds=120.0,
        execution_budget_seconds=1.5,
        run_mode="process",
    )
    cid = svc.create(
        document_id="doc-real", source_path=pdf, pages=[1], engines=["docling"]
    )
    started = time.monotonic()
    svc.start(cid)
    deadline = time.time() + 30
    while time.time() < deadline:
        side = svc.get(cid)["sides"][0]
        if side["status"] == "failed":
            break
        time.sleep(0.1)
    elapsed = time.monotonic() - started
    side = svc.get(cid)["sides"][0]
    assert side["status"] == "failed", side
    assert "EXECUTION_BUDGET_EXCEEDED" in (side["error"] or "")
    assert elapsed < 15.0, f"child termination must be prompt, took {elapsed:.1f}s"
    assert side["lease_until"] is None
    time.sleep(0.5)
    assert not [p for p in mp.active_children() if p.is_alive()], "no background residue"
    svc.repository.close()


@pytest.mark.slow
def test_process_mode_happy_path_publishes_the_real_child_result(tmp_path) -> None:
    """G03/S03: the supervised child is not just a kill path — a real
    Docling side completes in the child and its summary is published."""

    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    svc = ComparisonService(
        repository=Repository(tmp_path / "data" / "bio.sqlite", check_same_thread=False),
        artifact_root=tmp_path / "data",
        lease_seconds=600.0,
        execution_budget_seconds=300.0,
        run_mode="process",
    )
    cid = svc.create(
        document_id="doc-real2", source_path=pdf, pages=[1], engines=["docling"]
    )
    svc.start(cid)
    deadline = time.time() + 300
    while time.time() < deadline:
        side = svc.get(cid)["sides"][0]
        if side["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    side = svc.get(cid)["sides"][0]
    assert side["status"] == "done", side
    assert side["summary"].get("sequences") is not None
    assert (tmp_path / "data" / str(side["artifact_dir"])).is_dir()
    svc.repository.close()


# ---- G05/S05: side-level remote identity across attempts -------------------


def _zip_content(items) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x_content_list.json", json.dumps(items))
        zf.writestr("full.md", "# x")
    return buf.getvalue()


def test_retry_after_download_failure_reuses_the_remote_batch(tmp_path) -> None:
    """G05/S05 (the recheck's scenario): the cloud task finished but the
    download failed. A real retry must POLL the SAME batch — the upload
    count stays 1; only poll/download increase. The identity lives at the
    side level, not in the attempt dir."""

    from bio_pdf.application import comparison_service as cs
    from bio_pdf.parsers.mineru_adapter import MinerUAdapter

    posts = {"file_urls": 0, "zip_downloads": 0}
    zip_bytes = _zip_content(
        [
            {"type": "text", "text": "<210> 1", "page_idx": 0, "bbox": [10, 20, 100, 30]},
            {"type": "text", "text": "SEQ 1 TEXT", "page_idx": 0, "bbox": [10, 40, 200, 50]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            posts["file_urls"] += 1
            return httpx.Response(
                200,
                json={"data": {"batch_id": "bg-1", "file_urls": ["https://oss.example/put/g"]}},
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if "/extract-results/batch/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {"file_name": "cmp.pdf", "state": "done", "full_zip_url": "https://oss.example/zip/g"}
                        ]
                    }
                },
            )
        if url == "https://oss.example/zip/g":
            posts["zip_downloads"] += 1
            if posts["zip_downloads"] == 1:
                raise httpx.ConnectError("synthetic CDN failure")
            return httpx.Response(200, content=zip_bytes)
        raise AssertionError(url)

    def factory(engine, source_path, request):  # noqa: ANN001
        if engine == "mineru_http":
            return MinerUAdapter(token="t", transport=httpx.MockTransport(handler), poll_interval=0.0)
        return _StubAdapter("Ala Gly Leu Lys", "doc-cloud")

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(cs, "_side_engine", factory)
    try:
        pdf = _sample_pdf(tmp_path / "cmp.pdf")
        svc = ComparisonService(
                repository=Repository(tmp_path / "data" / "bio.sqlite", check_same_thread=False),
                artifact_root=tmp_path / "data",
                lease_seconds=120.0,
                run_mode="thread",
            )
        import hashlib as _h

        pdf = _sample_pdf(tmp_path / "cmp.pdf")
        doc_id = "sha256:" + _h.sha256(pdf.read_bytes()).hexdigest()
        cid = svc.create(
            document_id=doc_id,
            source_path=pdf,
            pages=[1],
            engines=["docling", "mineru_http"],
        )
        svc.start(cid)
        deadline = time.time() + 30
        while time.time() < deadline:
            info = svc.get(cid)
            if all(s["status"] in ("done", "failed") for s in info["sides"]):
                break
            time.sleep(0.05)
        sides = {s["side"]: s for s in svc.get(cid)["sides"]}
        assert sides["mineru_http"]["status"] == "failed"
        assert posts["file_urls"] == 1 and posts["zip_downloads"] == 1

        # The identity is at the SIDE level (not inside attempt-1).
        identity = (
            tmp_path / "data" / "comparisons" / cid / "mineru_http" / "remote"
            / "mineru_remote_identity.json"
        )
        assert identity.is_file(), list((tmp_path / "data").rglob("mineru_remote_identity.json"))
        assert not (
            tmp_path / "data" / "comparisons" / cid / "mineru_http" / "attempt-1"
            / "mineru_remote_identity.json"
        ).is_file()

        # REAL retry: only mineru re-runs; the batch is REUSED.
        svc.retry(cid)
        deadline = time.time() + 30
        while time.time() < deadline:
            info = svc.get(cid)
            if all(s["status"] in ("done", "failed") for s in info["sides"]):
                break
            time.sleep(0.05)
        sides = {s["side"]: s for s in svc.get(cid)["sides"]}
        assert sides["mineru_http"]["status"] == "done", sides["mineru_http"]
        assert sides["mineru_http"]["attempt"] == 2
        assert posts["file_urls"] == 1, "a retry must NOT re-upload the same request"
        assert posts["zip_downloads"] == 2, "the retry continues at the download step"
        assert sides["docling"]["attempt"] == 1, "the done side is untouched"
        # Both attempt dirs remain as immutable evidence.
        base = tmp_path / "data" / "comparisons" / cid / "mineru_http"
        assert (base / "attempt-1").is_dir() and (base / "attempt-2").is_dir()
        svc.repository.close()
    finally:
        mp.undo()
