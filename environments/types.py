from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class DockerHostPortDict(TypedDict):
    HostIp: str
    HostPort: str


DockerPortMapping = dict[str, list[DockerHostPortDict] | None]


@dataclass(frozen=True)
class PortConfiguration:
    vnc: str = "5900/tcp"

    def to_docker_port_mapping(self) -> dict[str, None]:
        return {self.vnc: None}


PORT_CONFIG = PortConfiguration()


@dataclass(frozen=True)
class ContainerPorts:
    vnc: int


@dataclass(frozen=True)
class ContainerInfo:
    container_id: str
    name: str
    ports: ContainerPorts
    status: str


@dataclass(frozen=True)
class HealthCheckResult:
    vnc: bool

    @property
    def is_healthy(self) -> bool:
        return self.vnc
