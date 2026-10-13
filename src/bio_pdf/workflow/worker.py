"""Isolated worker for heavy stage execution.

The heavy parser stages (Docling, later MinerU) run in a separate process so
the main process stays responsive and pypdfium2/model state does not leak
across jobs. On Windows this uses ``spawn`` semantics implicitly (a fresh
interpreter) and never passes PDF handles or model objects over a queue.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_stage_in_subprocess(
    module: str,
    *,
    payload_path: Path,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Run ``python -m <module>`` in a fresh interpreter.

    The child reads its request from ``payload_path`` and writes artifacts to
    the job output directory. ``check=True`` propagates a non-zero exit as a
    :class:`subprocess.CalledProcessError` which the runner maps to a failed
    stage.
    """

    subprocess.run(
        [sys.executable, "-m", module],
        env=env,
        cwd=str(cwd),
        check=True,
    )


__all__ = ["run_stage_in_subprocess"]