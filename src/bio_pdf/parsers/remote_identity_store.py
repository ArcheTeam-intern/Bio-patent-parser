"""Fenced remote-identity write store (K04).

The MinerU adapter persists its ``remote_identity.json`` file at a
fixed path per side. Two failure modes drove the rework:

1. A late response from a previous generation's polling loop was
   written back AFTER an explicit retry had already allocated a NEW
   generation. The new generation's state was reverted to the old one.
2. Multiple writers (the allocator, the uploader, the poller, the
   completion write) raced on the same ``identity.json`` (and on the
   shared ``.tmp`` file), letting one overwrite the other silently.

The store keeps the JSON contract small but adds two fences:

- a GENERATION counter the caller passes in on every transition. The
  transition applies only if the persisted ``generation`` still equals
  the caller's expected value. A stale generation is rejected.
- an OWNER token (the comparison-side lease token / attempt_token).
  The transition applies only if the persisted ``owner_token`` still
  matches. A takeover or a writeback from a non-owner is rejected.

The transition is wrapped in a SHORT critical section: read, validate,
patch, write under a unique temp file, atomic-replace. Network IO is
deliberately performed OUTSIDE the lock so the critical section stays
microseconds and concurrent waiters don't starve.

History is appended by every transition so a forensic read can replay
``terminal_failures`` and ``status_changes`` without diffing artefacts.

Public surface:

- :func:`transition` — single fenced apply.
- :func:`read` — forensic read of the persisted state.
- :class:`TransitionRejected` — raised when the fence fails. Carries
  the persisted state so the caller can re-decide.

The module is parser-agnostic; ``mineru_adapter`` is the only current
caller but the contract is designed to fit other cloud parsers with the
same allocated→uploaded→polling→terminal_failed/completed lifecycle.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class TransitionRejected(RuntimeError):
    """The remote-identity write was rejected by the generation/owner
    fence. The caller MUST inspect the carried ``current`` state and
    decide whether to retry, give up, or escalate.

    The exception is raised OUTSIDE the lock so callers can use the
    bundled state to choose a next step.
    """

    def __init__(
        self,
        message: str,
        *,
        current: dict[str, Any],
        reason: str,
        expected_version: "IdentityVersion | None",
    ) -> None:
        super().__init__(message)
        self.current = current
        self.reason = reason
        self.expected_version = expected_version


@dataclass(frozen=True)
class IdentityVersion:
    """The persistence-time identity of the remote-identity file.

    A writer passes the version it READ into ``transition``; the
    transition is applied only if the file still matches. A retry /
    recovery pass allocates a NEW version (generation +1 or owner
    change) and passes that — old in-flight writes are then rejected.
    """

    fingerprint: str
    generation: int
    owner_token: str
    #: monotonic revision inside one generation, incremented on every
    #: successful transition. Stale-revision writes inside the same
    #: generation are also rejected.
    revision: int = 0


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


@contextlib.contextmanager
def _exclusive(target: Path, *, timeout: float = 30.0):
    """Acquire an O_EXCL ``.lock`` file with stale-lock cleanup.

    Short critical section — the caller MUST NOT perform network IO
    inside the ``with`` block.
    """

    lock = _lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0.0
            if age > 120.0:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.time() > deadline:
                raise TransitionRejected(
                    "timed out waiting for the remote-identity lock",
                    current={},
                    reason="lock_timeout",
                    expected_version=None,
                )
            time.sleep(0.05)
    try:
        os.write(fd, f"{os.getpid()}:{uuid.uuid4().hex}".encode("ascii"))
        yield
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            lock.unlink()


def read(target: Path) -> dict[str, Any]:
    """Read the persisted state. Returns ``{}`` when the file is absent
    or unreadable — never raises."""

    if not target.is_file():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _version_of(state: dict[str, Any]) -> IdentityVersion:
    return IdentityVersion(
        fingerprint=str(state.get("fingerprint", "")),
        generation=int(state.get("generation", 0)),
        owner_token=str(state.get("owner_token", "")),
        revision=int(state.get("revision", 0)),
    )


def transition(
    target: Path,
    *,
    expected_version: IdentityVersion,
    patch: dict[str, Any],
    history_label: str,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Fenced read-modify-write of the persisted remote identity.

    Steps (under a short lock):
    1. Re-read the file. If absent, treat as an empty state with
       ``revision=-1`` so the FIRST write still goes through (its
       expected_version.revision is 0).
    2. Compare ``current.{fingerprint, generation, owner_token,
       revision}`` to ``expected_version``. Any mismatch → reject with
       :class:`TransitionRejected` carrying the current state.
    3. Merge ``patch`` into a shallow copy. Bump ``revision`` by one
       and stamp ``updated_at`` + ``last_writer_pid``.
    4. Append a ``history`` entry tagged ``history_label`` so the
       forensic trail shows which transition changed which fields.
    5. Write the new state to a UNIQUE temp file (mkstemp) and
       atomically replace the target.

    Network IO must be performed by the caller OUTSIDE the call.
    """

    if now is None:
        from datetime import datetime, timezone

        now_fn = lambda: datetime.now(tz=timezone.utc).isoformat()  # noqa: E731
    else:
        now_fn = now

    with _exclusive(target):
        current = read(target)
        current_version = _version_of(current)
        if (
            current_version.fingerprint != expected_version.fingerprint
            or current_version.generation != expected_version.generation
            or current_version.owner_token != expected_version.owner_token
            or current_version.revision != expected_version.revision
        ):
            raise TransitionRejected(
                "remote identity version fence failed",
                current=current,
                reason=(
                    f"expected_version={expected_version!r} "
                    f"current_version={current_version!r}"
                ),
                expected_version=expected_version,
            )
        next_state: dict[str, Any] = dict(current)
        next_state.update(patch)
        next_state["revision"] = current_version.revision + 1
        next_state["updated_at"] = now_fn()
        next_state["last_writer_pid"] = os.getpid()
        history = list(next_state.get("history") or [])
        history.append(
            {
                "label": history_label,
                "at": next_state["updated_at"],
                "expected_version": {
                    "fingerprint": expected_version.fingerprint,
                    "generation": expected_version.generation,
                    "owner_token": expected_version.owner_token,
                    "revision": expected_version.revision,
                },
                "patch_keys": sorted(patch.keys()),
            }
        )
        next_state["history"] = history[-64:]  # cap so the file stays small
        # Atomic write with a UNIQUE temp file (mkstemp) so two writers
        # never collide on the same temporary path.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(next_state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, target)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        return next_state


__all__ = [
    "IdentityVersion",
    "TransitionRejected",
    "read",
    "transition",
]
