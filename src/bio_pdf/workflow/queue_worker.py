"""Persistent single-concurrency worker with fenced leases (plan T03).

- Claims return a :class:`ClaimedJob` carrying ``worker_id``/``generation``;
  every heartbeat, stage gate and release is fenced by that identity.
- A background heartbeat thread (its OWN SQLite connection — never shared
  across threads) extends the lease every 30 s. Losing the lease sets a
  stop flag checked before every stage; the runner aborts without
  publishing.
- Resume intent is persisted in the job config (``execution_mode``), so a
  crash + reclaim resumes from verified cache while a first run does not.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.errors import WorkflowError
from bio_pdf.storage.repository import ClaimedJob, LeaseToken, Repository

#: Lease duration; the worker heartbeats at one fifth of this interval.
LEASE_SECONDS = 900.0
HEARTBEAT_INTERVAL = LEASE_SECONDS / 5

#: Presence heartbeat cadence and the age after which the API reports a
#: worker as offline (live-ops: an idle worker holds no job lease, so
#: without this the queue cannot tell "no parser is running" apart from
#: "the parser is just slow").
PRESENCE_INTERVAL = 5.0
PRESENCE_STALE_AFTER = 15.0


def _worker_id() -> str:
    """Globally unique worker identity (B03: not hostname+second)."""

    import os
    import uuid

    return f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class _LeaseKeeper:
    """Owns its own SQLite connection; stops on lost lease."""

    def __init__(self, data_dir: Path, claimed: ClaimedJob) -> None:
        self._repo = JobService(JobPaths.under(data_dir)).repository
        self.claimed = claimed
        self.lost = threading.Event()
        self._stop = threading.Event()

    def _loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            ok = self._repo.heartbeat(
                self.claimed.job.job_id,
                worker_id=self.claimed.worker_id,
                generation=self.claimed.generation,
                lease_seconds=LEASE_SECONDS,
            )
            if not ok:
                self.lost.set()
                return

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._repo.close()


class _PresenceKeeper:
    """Publishes "this worker is polling" with its own SQLite connection.

    Runs independently of the job loop, so the row stays fresh both while
    idle-polling and during a multi-minute job run. On clean shutdown the
    row is deleted; a crashed worker's row simply goes stale and the API
    reports it offline.
    """

    def __init__(self, data_dir: Path, worker_id: str) -> None:
        self._repo = Repository(JobPaths.under(data_dir).database)
        self._worker_id = worker_id
        self._data_dir = str(data_dir.resolve())
        self._stop = threading.Event()

    def _beat(self) -> None:
        import os

        self._repo.record_worker_presence(
            self._worker_id, pid=os.getpid(), data_dir=self._data_dir
        )

    def _loop(self) -> None:
        while not self._stop.wait(PRESENCE_INTERVAL):
            self._beat()

    def start(self) -> None:
        self._beat()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._repo.clear_worker_presence(self._worker_id)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

    def close(self) -> None:
        self._repo.close()


def run_worker(data_dir: Path, *, poll_interval: float = 1.5, once: bool = False) -> int:
    """Claim and run jobs until interrupted. Returns a process exit code."""

    worker_id = _worker_id()
    service = JobService(JobPaths.under(data_dir))
    print(f"[queue_worker] {worker_id} polling {data_dir}", flush=True)
    presence = _PresenceKeeper(data_dir, worker_id)
    presence.start()
    try:
        while True:
            claimed = service.repository.claim_next_queued_job(
                worker_id=worker_id, lease_seconds=LEASE_SECONDS
            )
            if claimed is None:
                if once:
                    return 0
                time.sleep(poll_interval)
                continue

            job = claimed.job
            print(
                f"[queue_worker] claimed {job.job_id} (gen={claimed.generation})",
                flush=True,
            )
            keeper = _LeaseKeeper(data_dir, claimed)
            keeper.start()
            try:
                resume_mode = service.job_config(job.job_id).get("execution_mode") == "resume"
                result = service.run_job(
                    job.job_id,
                    resume=resume_mode,
                    # Fencing: the runner aborts before any status write or
                    # publish once this worker no longer holds the lease.
                    lease_guard=lambda: not keeper.lost.is_set(),
                    # B03: conditional commits re-verify this token inside
                    # their own transactions (stage completion, publication).
                    lease_token=LeaseToken.from_claim(claimed),
                )
                print(
                    f"[queue_worker] {job.job_id} -> {result.status}"
                    f" (review={result.review_count})",
                    flush=True,
                )
            except WorkflowError as exc:
                print(f"[queue_worker] {job.job_id} failed: {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001 - worker must survive jobs
                print(f"[queue_worker] {job.job_id} crashed: {exc!r}", flush=True)
                if keeper.lost.is_set():
                    print(
                        f"[queue_worker] {job.job_id} lease lost — writes stopped",
                        flush=True,
                    )
                else:
                    service.repository.update_job_status(job.job_id, "failed")
            finally:
                keeper.stop()
                service.repository.release_lease(
                    job.job_id,
                    worker_id=claimed.worker_id,
                    generation=claimed.generation,
                )
                keeper.close()
            if once:
                return 0
    except KeyboardInterrupt:
        print("[queue_worker] interrupted", flush=True)
        return 0
    finally:
        presence.stop()
        presence.close()
        service.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bio PDF persistent queue worker")
    parser.add_argument("--data-dir", type=Path, default=Path("data/demo"))
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--once", action="store_true", help="Exit when no queued job remains")
    args = parser.parse_args(argv)
    sys.exit(run_worker(args.data_dir, poll_interval=args.poll_interval, once=args.once))


if __name__ == "__main__":
    main()
