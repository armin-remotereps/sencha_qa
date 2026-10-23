from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict


class VisualEngineState(StrEnum):
    # At least one worker is ready.
    READY = "ready"
    # None is ready, but a connected worker is loading or has not reported yet.
    STARTING = "starting"
    # Workers are connected and every one of them failed to load.
    FAILED = "failed"
    # No live worker is connected.
    OFFLINE = "offline"


class VisionWorkerViewPayload(TypedDict):
    name: str
    # A WorkerState value, or "" before the first status.
    state: str
    device: str
    gpu: str
    message: str
    client_version: str
    # ISO 8601, or "" when unknown.
    connected_since: str


class VisualEngineStatusPayload(TypedDict):
    """JSON shape of the visual engine status for templates and WebSockets."""

    state: str
    ready_count: int
    connected_count: int
    workers: list[VisionWorkerViewPayload]


@dataclass(frozen=True)
class VisionWorkerView:
    name: str
    state: str
    device: str
    gpu: str
    status_message: str
    client_version: str
    connected_since: str

    def to_payload(self) -> VisionWorkerViewPayload:
        return VisionWorkerViewPayload(
            name=self.name,
            state=self.state,
            device=self.device,
            gpu=self.gpu,
            message=self.status_message,
            client_version=self.client_version,
            connected_since=self.connected_since,
        )


@dataclass(frozen=True)
class VisualEngineStatus:
    state: VisualEngineState
    ready_count: int
    connected_count: int
    workers: tuple[VisionWorkerView, ...]

    def to_payload(self) -> VisualEngineStatusPayload:
        return VisualEngineStatusPayload(
            state=self.state.value,
            ready_count=self.ready_count,
            connected_count=self.connected_count,
            workers=[worker.to_payload() for worker in self.workers],
        )
