"""K08: ready/release handshake for the docling inference child.

The process-tree budget test needs the docling grandchild to be alive
WHEN the budget fires — a real Docling run on a one-page PDF finishes
in a couple of seconds, which makes "the budget expired while the
descendant was alive" nearly impossible to observe on a 30 s budget.

This module uses an env-var contract so the docling subprocess
respects the same ready/release handshake:

- ``BIO_PDF_K08_HOLD_DIR=<path>`` is set on the parent process BEFORE
  it spawns the comparison child. ``install_hold_for_subprocess``
  records the path so the test can read the ready sentinel and write
  the release sentinel.
- The docling_adapter reads the env var and, after a successful
  ``convert(...)``, writes READY into the hold dir and BLOCKS on
  RELEASE before returning — exactly the contract ``install_hold``
  exposes.
- The hold happens AFTER ``convert`` succeeds — the real model
  inference is still exercised; only the wall-clock duration is held
  under controlled release.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

_READY_FILENAME = "k08_ready.json"
_RELEASE_FILENAME = "k08_release.json"


def install_hold_for_subprocess(*, hold_dir: Path) -> dict[str, Path]:
    """Configure the environment so a docling subprocess honours the
    ready/release handshake. Returns the ready / release paths so the
    scenario can poll / write.
    """

    hold_dir.mkdir(parents=True, exist_ok=True)
    ready_path = hold_dir / _READY_FILENAME
    release_path = hold_dir / _RELEASE_FILENAME
    for p in (ready_path, release_path):
        if p.exists():
            p.unlink()
    os.environ["BIO_PDF_K08_HOLD_DIR"] = str(hold_dir.resolve())
    return {"ready": ready_path, "release": release_path}


def release_hold(hold_dir: Path) -> None:
    """Write the RELEASE sentinel so the held docling grandchild can
    return. No-op when the hold was never installed or already released.
    """

    release_path = Path(hold_dir) / _RELEASE_FILENAME
    release_path.write_text("released", encoding="utf-8")


def uninstall_hold() -> None:
    os.environ.pop("BIO_PDF_K08_HOLD_DIR", None)


def apply_hold_if_configured(converter, request) -> None:
    """Installed as the converter's ``convert`` method at adapter
    construction time (K08 contract). When ``BIO_PDF_K08_HOLD_DIR`` is
    set in the subprocess env, the held convert blocks on a release
    sentinel after the real model output is in hand.
    """

    hold_dir_env = os.environ.get("BIO_PDF_K08_HOLD_DIR")
    if not hold_dir_env:
        return  # not a test scenario — no hold
    hold_dir = Path(hold_dir_env)
    hold_dir.mkdir(parents=True, exist_ok=True)
    ready_path = hold_dir / _READY_FILENAME
    release_path = hold_dir / _RELEASE_FILENAME

    original_convert = converter.convert
    # The docling_adapter passes a dict (the request_payload) — pull
    # the job_id defensively (object OR dict).
    request_id = (
        getattr(request, "job_id", None)
        or (request.get("job_id") if isinstance(request, dict) else None)
        or "k08"
    )

    def _held_convert(*args, **kwargs):
        real = original_convert(*args, **kwargs)
        try:
            ready_path.write_text(
                json.dumps(
                    {
                        "at": time.time(),
                        "own_pid": os.getpid(),
                        "parent_pid": os.getppid(),
                        "request_id": str(request_id),
                    }
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        # Block until release — never longer than the watchdog's
        # budget so the test cannot deadlock.
        deadline = time.time() + 120.0
        while time.time() < deadline:
            if release_path.exists():
                try:
                    release_path.unlink()
                except OSError:
                    pass
                return real
            time.sleep(0.05)
        # Held too long — return what we have so the parent can still
        # get a result (the watchdog will still terminate us).
        return real

    converter.convert = _held_convert  # type: ignore[method-assign]

