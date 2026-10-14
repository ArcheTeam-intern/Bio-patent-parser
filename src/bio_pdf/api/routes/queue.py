"""Queue status: is anything actually polling this data directory?

A queued job with no worker is indistinguishable from a slow parse in the
job payload alone. The worker publishes a presence heartbeat (its own
table, written every few seconds even while idle), and this endpoint
exposes it so the UI can say "no parser process is running" instead of
leaving an uploaded job sitting at 等待解析 with no explanation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from bio_pdf.api.contracts import QueueStatusResponse, WorkerPresenceView
from bio_pdf.api.dependencies import ServiceDep, SettingsDep
from bio_pdf.workflow.queue_worker import PRESENCE_STALE_AFTER

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/status", response_model=QueueStatusResponse)
def queue_status(service: ServiceDep, settings: SettingsDep) -> QueueStatusResponse:
    now = datetime.now(tz=timezone.utc)
    workers: list[WorkerPresenceView] = []
    for row in service.repository.list_worker_presence():
        try:
            last_seen = datetime.fromisoformat(str(row["last_seen_at"]))
        except (TypeError, ValueError):
            continue
        age = max(0.0, (now - last_seen).total_seconds())
        workers.append(
            WorkerPresenceView(
                worker_id=str(row["worker_id"]),
                pid=row["pid"],
                seconds_since_heartbeat=round(age, 1),
                online=age <= PRESENCE_STALE_AFTER,
            )
        )
    counts = service.repository.count_jobs_by_status()
    return QueueStatusResponse(
        data_dir=str(settings.data_dir.resolve()),
        queued_jobs=counts.get("queued", 0),
        running_jobs=counts.get("running", 0),
        workers=sorted(workers, key=lambda w: w.seconds_since_heartbeat),
        online_worker_count=sum(1 for w in workers if w.online),
        stale_after_seconds=PRESENCE_STALE_AFTER,
    )


__all__ = ["router"]
