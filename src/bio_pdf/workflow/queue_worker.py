"""Persistent single-concurrency worker.

The worker is a separate process from the API. It claims queued jobs
through short SQLite transactions (never holding a transaction across
model execution), runs the full production pipeline via
:class:`~bio_pdf.application.job_service.JobService`, and heartbeats a
lease so a crashed worker's job becomes claimable again after the lease
expires. Old-generation results can never clobber a newer run because the
runner only switches the job status at the end and artifacts are
content-addressed.

Run it with::

    python -m bio_pdf.workflow.queue_worker --data-dir data/demo
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.errors import WorkflowError

#: Lease duration; the worker heartbeats at half this interval.
LEASE_SECONDS = 900.0


def _worker_id() -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"worker-{socket.gethostname()}-{stamp}"


def run_worker(data_dir: Path, *, poll_interval: float = 1.5, once: bool = False) -> int:
    """Claim and run jobs until interrupted. Returns a process exit code."""

    worker_id = _worker_id()
    service = JobService(JobPaths.under(data_dir))
    print(f"[queue_worker] {worker_id} polling {data_dir}", flush=True)
    try:
        while True:
            job = service.repository.claim_next_queued_job(
                worker_id=worker_id, lease_seconds=LEASE_SECONDS
            )
            if job is None:
                if once:
                    return 0
                time.sleep(poll_interval)
                continue

            print(f"[queue_worker] claimed {job.job_id}", flush=True)
            try:
                result = service.run_job(job.job_id)
                print(
                    f"[queue_worker] {job.job_id} -> {result.status}"
                    f" (review={result.review_count})",
                    flush=True,
                )
            except WorkflowError as exc:
                print(f"[queue_worker] {job.job_id} failed: {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001 - worker must survive jobs
                print(f"[queue_worker] {job.job_id} crashed: {exc!r}", flush=True)
                service.repository.update_job_status(job.job_id, "failed")
            finally:
                service.repository.release_lease(job.job_id, worker_id=worker_id)
            if once:
                return 0
    except KeyboardInterrupt:
        print("[queue_worker] interrupted", flush=True)
        return 0
    finally:
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
