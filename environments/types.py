from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class DockerHostPortDict(TypedDict):
    HostIp: str
    HostPort: str


DockerPortMapping = dict[str, list[DockerHostPortDict] | None]


@dataclass(frozen=True)
class PortConfiguration:
    ssh: str = "22/tcp"
    vnc: str = "5900/tcp"
    playwright_cdp: str = "9223/tcp"

    def to_docker_port_mapping(self) -> dict[str, None]:
        return {self.ssh: None, self.vnc: None, self.playwright_cdp: None}


PORT_CONFIG = PortConfiguration()


@dataclass(frozen=True)
class ContainerPorts:
    ssh: int
    vnc: int
    playwright_cdp: int


@dataclass(frozen=True)
class ContainerInfo:
    container_id: str
    name: str
    ports: ContainerPorts
    status: str


@dataclass(frozen=True)
class HealthCheckResult:
    ssh: bool
    vnc: bool
    playwright: bool = True

    @property
    def all_ok(self) -> bool:
        return self.ssh and self.vnc


@dataclass(frozen=True)
class SSHResult:
    exit_code: int
    stdout: str
    stderr: str
