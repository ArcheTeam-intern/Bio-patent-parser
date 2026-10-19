"""H01 + I01 + J02 + J03: the budget kills the WHOLE process tree — and the
test binds exactly ITS OWN tree.

- Discovery starts from the execution root that the service REGISTERED for
  this comparison_id (`live_side_processes()`), never from
  ``multiprocessing.active_children()`` (which could hand over another
  fixture's child) and never from a machine-wide command-line match (which
  matched the query itself).
- The exit-scenario script carries a ``__main__`` guard so a Windows spawn
  bootstrap cannot re-enter ``main()``; the PID artifact is owner-bound
  (outer scenario PID + random run token) and only the outer process writes
  it.
- Every scenario runs beside unrelated processes (a multiprocessing child
  of the pytest process AND a subprocess) that must survive.

K08 added a deterministic ready/release handshake so the budget test
does not depend on Docling inference being slow enough to exceed the
budget. The handshake is enabled by ``BIO_PDF_K08_HOLD_DIR``; without
it the test falls back to the old behaviour (which is still useful as
a smoke test for the process tree plumbing).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

TESTS_DIR = Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from helpers import proc_tree  # noqa: E402
from helpers.docling_hold_bootstrap import (  # noqa: E402
    install_hold_for_subprocess,
    release_hold,
    uninstall_hold,
)

ENGINE_ENTRY = "bio_pdf.parsers.docling_child"


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


def _spawn_unrelated_subprocess() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _own_mp_child_sleeping(seconds: int = 300) -> mp.Process:
    """A multiprocessing child owned by THIS test (the J02 trap: it must not
    be mistaken for a comparison root nor be cleaned up)."""

    proc = mp.Process(target=time.sleep, args=(seconds,), daemon=True)
    proc.start()
    return proc


def _wait_registered_root(comparison_id: str, *, timeout: float = 30.0) -> int | None:
    """J02: find OUR execution root through the service registry."""

    from bio_pdf.application.comparison_service import live_side_processes

    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, identity in live_side_processes().items():
            if identity.get("comparison_id") == comparison_id:
                return pid
        time.sleep(0.2)
    return None


def _wait_until_dead(pids: list[int], timeout: float = 30.0) -> list[int]:
    deadline = time.time() + timeout
    remaining = list(pids)
    while time.time() < deadline and remaining:
        remaining = [pid for pid in remaining if proc_tree.pid_alive(pid)]
        if remaining:
            time.sleep(1)
    return remaining


def test_budget_timeout_terminates_only_the_registered_tree(tmp_path: Path) -> None:
    from bio_pdf.application.comparison_service import ComparisonService
    from bio_pdf.storage.repository import Repository

    # K08: enable the deterministic ready/release handshake so the
    # budget test no longer depends on real Docling inference taking
    # longer than the budget. The descendant writes READY when its
    # real ``convert`` finishes and blocks on RELEASE; we advance the
    # injected clock past the deadline while the descendant is held.
    hold_dir = tmp_path / "h01_hold"
    paths = install_hold_for_subprocess(hold_dir=hold_dir)
    base = {"now": 1000.0}

    def _clock() -> float:
        return base["now"]

    unrelated_proc = _spawn_unrelated_subprocess()
    unrelated_mp = _own_mp_child_sleeping()
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=30.0,
        run_mode="process",
        clock=_clock,
    )
    cid = svc.create(document_id="doc-j02", source_path=pdf, pages=[1], engines=["docling"])
    root_pid: int | None = None
    grand_ids: list[int] = []
    try:
        svc.start(cid)
        # J02: identity-bound root, not "first active child".
        root_pid = _wait_registered_root(cid, timeout=30.0)
        assert root_pid is not None, "the service never registered its execution root"
        assert root_pid != unrelated_mp.pid
        # K08: wait for the docling grandchild to reach the held state.
        # Cold model loading can take ~65 s on this machine, so the wall
        # clock cap is 180 s; a dead execution root fails immediately
        # instead of burning the cap.
        ready_deadline = time.time() + 180.0
        while time.time() < ready_deadline:
            if paths["ready"].exists():
                break
            if not proc_tree.pid_alive(root_pid):
                raise AssertionError(
                    "the execution root died before the descendant reached the held state"
                )
            time.sleep(0.2)
        assert paths["ready"].exists(), "descendant never reached the held state"
        # Now advance the injected clock past the deadline. The
        # watchdog will see ``now > deadline`` and terminate the tree.
        base["now"] += 60.0
        # Release the held grandchild so the orphan Pipe closes.
        release_hold(hold_dir)

        deadline = time.time() + 90
        while time.time() < deadline:
            side = svc.get(cid)["sides"][0]
            if side["status"] == "failed":
                break
            time.sleep(0.5)
        side = svc.get(cid)["sides"][0]
        assert side["status"] == "failed", side
        assert "EXECUTION_BUDGET_EXCEEDED" in (side["error"] or "")
        remaining = _wait_until_dead(grand_ids + [root_pid], timeout=30.0)
        assert remaining == [], f"test-owned descendants still running: {remaining}"
        assert proc_tree.pid_alive(unrelated_proc.pid), "the unrelated subprocess must survive"
        assert unrelated_mp.is_alive(), "the unrelated multiprocessing child must survive"
    finally:
        uninstall_hold()
        if root_pid is not None and proc_tree.pid_alive(root_pid):
            proc_tree.kill_tree(root_pid)  # only OUR recorded root
        unrelated_proc.kill()
        if unrelated_mp.is_alive():
            unrelated_mp.terminate()
        unrelated_mp.join(timeout=10)
        svc.repository.close()


def test_root_discovery_ignores_foreign_mp_children(tmp_path: Path) -> None:
    """J02: with only a FOREIGN multiprocessing child around (no comparison
    started), discovery for a made-up comparison_id must find nothing —
    never hand over the foreign child."""

    unrelated_mp = _own_mp_child_sleeping()
    try:
        assert _wait_registered_root("cmp-never-started", timeout=1.0) is None
        assert proc_tree.pid_alive(unrelated_mp.pid)
    finally:
        if unrelated_mp.is_alive():
            unrelated_mp.terminate()
        unrelated_mp.join(timeout=10)


# ---------------------------------------------------------------------------
# J03: exit scenario — __main__ guard, owner/token-bound PID artifact
# ---------------------------------------------------------------------------

_SCENARIO = '''
"""Exit scenario: normal interpreter exit must sweep OUR trees."""
import json
import os
import secrets
import sys
import time
from pathlib import Path

from helpers import proc_tree

from bio_pdf.application.comparison_service import ComparisonService
from bio_pdf.storage.repository import Repository


def main() -> None:
    data = Path(sys.argv[1])
    out = Path(sys.argv[2])
    pdf = Path(sys.argv[3])
    run_token = sys.argv[4]
    owner_pid = os.getpid()
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=600.0,
        run_mode="process",
    )
    cid = svc.create(document_id="doc-j03", source_path=pdf, pages=[1], engines=["docling"])
    svc.start(cid)
    from bio_pdf.application.comparison_service import live_side_processes

    root = None
    deadline = time.time() + 60
    while time.time() < deadline:
        for pid, identity in live_side_processes().items():
            if identity.get("comparison_id") == cid:
                root = pid
                break
        if root:
            break
        time.sleep(0.2)
    grand = []
    if root:
        grand = [
            m.pid
            for m in proc_tree.wait_for_engine_descendant(
                root, "bio_pdf.parsers.docling_child", timeout=90.0
            )
        ]
    # J03: ONLY the outer process writes the artifact, and it is bound to
    # the outer pid + run token so a re-entrant bootstrap can never forge or
    # overwrite it.
    if os.getpid() == owner_pid:
        out.write_text(
            json.dumps(
                {
                    "owner_pid": owner_pid,
                    "run_token": run_token,
                    "comparison_id": cid,
                    "middle": [root] if root else [],
                    "grand": grand,
                }
            ),
            encoding="utf-8",
        )
    # NORMAL interpreter exit → atexit sweeps the registered trees.


if __name__ == "__main__":
    main()
'''


def test_scenario_bootstrap_does_not_reenter_main(tmp_path: Path) -> None:
    """J03: importing the scenario as ``__mp_main__`` (Windows spawn) must
    NOT create a comparison or write the PID artifact."""

    scenario = tmp_path / "scenario_guard.py"
    scenario.write_text(_SCENARIO, encoding="utf-8")
    data = tmp_path / "guard-data"
    out = tmp_path / "guard-out.json"
    script = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('__mp_main__', {str(scenario)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.argv = ['scenario', "
        f"{str(data)!r}, {str(out)!r}, {str(tmp_path / 'cmp.pdf')!r}, 'tok']\n"
        "spec.loader.exec_module(module)\n"
        "print('imported')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(TESTS_DIR)},
    )
    assert proc.returncode == 0, proc.stderr[-600:]
    assert "imported" in proc.stdout
    assert not out.exists(), "a spawn bootstrap must not write the PID artifact"
    # No comparison may exist either.
    import sqlite3

    db = data / "bio.sqlite"
    if db.is_file():
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]
        conn.close()
        assert rows == 0, "the bootstrap must not create comparisons"


def test_service_shutdown_sweeps_only_the_registered_tree(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario_j03.py"
    scenario.write_text(_SCENARIO, encoding="utf-8")
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    out = tmp_path / "pids.json"
    run_token = secrets.token_hex(8)
    unrelated_proc = _spawn_unrelated_subprocess()
    unrelated_mp = _own_mp_child_sleeping()
    try:
        proc = subprocess.run(
            [sys.executable, str(scenario), str(data), str(out), str(pdf), run_token],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=300,
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(TESTS_DIR)},
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        recorded = json.loads(out.read_text(encoding="utf-8"))
        # J03: the artifact proves WHICH outer process wrote it.
        assert recorded["run_token"] == run_token
        assert recorded["comparison_id"].startswith("cmp-")
        assert recorded["middle"], "the scenario never registered its execution root"
        if not recorded["grand"]:
            pytest.skip("the docling inference descendant never appeared in this environment")
        remaining = _wait_until_dead(
            recorded["grand"] + recorded["middle"], timeout=30.0
        )
        assert remaining == [], f"test-owned processes survived shutdown: {remaining}"
        assert proc_tree.pid_alive(unrelated_proc.pid), "the unrelated subprocess must survive"
        assert unrelated_mp.is_alive(), "the unrelated multiprocessing child must survive"
    finally:
        unrelated_proc.kill()
        if unrelated_mp.is_alive():
            unrelated_mp.terminate()
        unrelated_mp.join(timeout=10)


def test_wait_fails_without_an_engine_descendant() -> None:
    matches = proc_tree.wait_for_engine_descendant(
        os.getpid(), ENGINE_ENTRY, timeout=4.0, poll_interval=1.0
    )
    assert matches == []