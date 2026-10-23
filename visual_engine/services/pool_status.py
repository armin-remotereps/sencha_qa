from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone

from vision_worker.protocol import WorkerState
from visual_engine.models import VisionWorker
from visual_engine.types import VisionWorkerView, VisualEngineState, VisualEngineStatus

VISUAL_ENGINE_GROUP: Final[str] = "visual_engine_status"


@dataclass(frozen=True)
class ReadyWorker:
    id: int
    name: str
    channel_name: str


def get_visual_engine_status() -> VisualEngineStatus:
    """Summarise every live (connected, recently heard from) worker in one query."""
    workers = tuple(
        _build_worker_view(worker)
        for worker in _live_workers().only(
            "name",
            "state",
            "device",
            "status_message",
            "client_version",
            "system_info",
            "connected_at",
        )
    )
    ready_count = sum(1 for worker in workers if worker.state == WorkerState.READY)
    return VisualEngineStatus(
        state=_derive_state(workers, ready_count),
        ready_count=ready_count,
        connected_count=len(workers),
        workers=workers,
    )


def broadcast_visual_engine_status() -> None:
    """Push the current engine status to every open project / machine-setup page."""
    layer: Any = get_channel_layer()
    if layer is None:
        return
    payload = get_visual_engine_status().to_payload()
    async_to_sync(layer.group_send)(
        VISUAL_ENGINE_GROUP,
        {"type": "visual_engine.status", "visual_engine": payload},
    )


def list_ready_workers() -> list[ReadyWorker]:
    """The live, ready workers in a stable order (by id), for dispatching parses."""
    return [
        ReadyWorker(id=worker_id, name=name, channel_name=channel_name)
        for worker_id, name, channel_name in _live_workers()
        .filter(state=WorkerState.READY)
        .exclude(channel_name="")
        .order_by("id")
        .values_list("id", "name", "channel_name")
    ]


def _live_workers() -> QuerySet[VisionWorker]:
    # A consumer that died without running disconnect (Daphne crash) leaves its
    # row connected; the heartbeat cutoff drops it.
    cutoff = timezone.now() - timedelta(seconds=settings.VISION_WORKER_STALE_SECONDS)
    return VisionWorker.objects.filter(connected=True, last_seen_at__gte=cutoff)


def _derive_state(
    workers: tuple[VisionWorkerView, ...], ready_count: int
) -> VisualEngineState:
    if ready_count:
        return VisualEngineState.READY
    if not workers:
        return VisualEngineState.OFFLINE
    if any(worker.state != WorkerState.FAILED for worker in workers):
        # Loading, or connected but not reported yet.
        return VisualEngineState.STARTING
    return VisualEngineState.FAILED


def _build_worker_view(worker: VisionWorker) -> VisionWorkerView:
    system_info: object = worker.system_info
    gpu = system_info.get("gpu", "") if isinstance(system_info, dict) else ""
    return VisionWorkerView(
        name=worker.name,
        state=worker.state,
        device=worker.device,
        gpu=str(gpu),
        status_message=worker.status_message,
        client_version=worker.client_version,
        connected_since=_isoformat(worker.connected_at),
    )


def _isoformat(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else ""
