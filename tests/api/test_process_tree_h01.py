"""H01/S01: the execution budget must kill the WHOLE process tree.

The chain is comparison thread → spawn child → engine inference child
(Docling runs its converter in its own subprocess). ``terminate()`` on the
middle process left the grandchild burning CPU. These tests use the REAL
Docling chain: they wait until the inference child exists, let the budget
fire, and assert every recorded descendant is gone.

Windows detection uses a CIM query on the command line; the scenario
process records its own grandchild PID before exiting so the assertion
checks actual PIDs, not a bookkeeping flag.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


def _docling_child_pids() -> list[int]:
    """PIDs of live ``docling_child`` processes (the inference grandchild)."""

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*docling_child*' }"
                " | Select-Object -ExpandProperty ProcessId",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        return []
    return [int(token) for token in result.stdout.split() if token.strip().isdigit()]


def _pid_alive(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    return str(pid) in (result.stdout or "")


def _force_kill(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, errors="replace", timeout=30
    )


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


def _wait_for_grandchild(timeout: float = 60.0) -> list[int]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = _docling_child_pids()
        if pids:
            return pids
        time.sleep(2)
    return []


def _wait_until_dead(pids: list[int], timeout: float = 30.0) -> list[int]:
    deadline = time.time() + timeout
    remaining = list(pids)
    while time.time() < deadline and remaining:
        remaining = [pid for pid in remaining if _pid_alive(pid)]
        if remaining:
            time.sleep(1)
    return remaining


def test_budget_timeout_terminates_the_real_docling_grandchild(tmp_path: Path) -> None:
    """The reviewer's repro: the inference grandchild is RUNNING when the
    budget fires (the middle process alone is not the execution unit)."""

    from bio_pdf.application.comparison_service import ComparisonService
    from bio_pdf.storage.repository import Repository

    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=30.0,
        run_mode="process",
    )
    cid = svc.create(document_id="doc-h01", source_path=pdf, pages=[1], engines=["docling"])
    recorded: list[int] = []
    try:
        svc.start(cid)
        recorded = _wait_for_grandchild(60.0)
        if not recorded:
            pytest.skip("the docling inference child never appeared in this environment")
        # Budget 30 s from side start: the grandchild is alive NOW and must
        # be gone shortly after the fence.
        deadline = time.time() + 90
        while time.time() < deadline:
            side = svc.get(cid)["sides"][0]
            if side["status"] == "failed":
                break
            time.sleep(0.5)
        side = svc.get(cid)["sides"][0]
        assert side["status"] == "failed", side
        assert "EXECUTION_BUDGET_EXCEEDED" in (side["error"] or "")
        remaining = _wait_until_dead(recorded, timeout=30.0)
        assert remaining == [], f"grandchild still running after budget: {remaining}"
    finally:
        for pid in recorded:
            if _pid_alive(pid):
                _force_kill(pid)
        svc.repository.close()


_SCENARIO = '''
import json
import subprocess
import sys
import time
from pathlib import Path

from bio_pdf.application.comparison_service import ComparisonService
from bio_pdf.storage.repository import Repository


def pids() -> list[int]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*docling_child*' }"
             " | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        return [int(t) for t in result.stdout.split() if t.strip().isdigit()]
    except Exception:
        return []


def main() -> None:
    data = Path(sys.argv[1])
    out = Path(sys.argv[2])
    pdf = Path(sys.argv[3])
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=600.0,
        run_mode="process",
    )
    cid = svc.create(document_id="doc-shutdown", source_path=pdf, pages=[1], engines=["docling"])
    svc.start(cid)
    grand = []
    deadline = time.time() + 90
    while time.time() < deadline:
        grand = pids()
        if grand:
            break
        time.sleep(2)
    import multiprocessing as mp
    middle = [p.pid for p in mp.active_children()]
    out.write_text(json.dumps({"grand": grand, "middle": middle}), encoding="utf-8")
    # NORMAL interpreter exit → atexit must sweep the registered trees.


main()
'''


def test_service_shutdown_sweeps_the_process_trees(tmp_path: Path) -> None:
    """An API-style NORMAL shutdown (atexit) must clean the same scope: the
    scenario process records its live middle + grandchild PIDs, exits, and
    the parent asserts both are gone."""

    scenario = tmp_path / "scenario.py"
    scenario.write_text(_SCENARIO, encoding="utf-8")
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    out = tmp_path / "pids.json"
    proc = subprocess.run(
        [sys.executable, str(scenario), str(data), str(out), str(pdf)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    recorded = json.loads(out.read_text(encoding="utf-8"))
    if not recorded["grand"]:
        pytest.skip("the docling inference child never appeared in this environment")
    remaining = _wait_until_dead(recorded["grand"] + recorded["middle"], timeout=30.0)
    assert remaining == [], f"processes survived the service shutdown: {remaining}"
