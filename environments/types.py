from __future__ import annotations

from dataclasses import dataclass

DockerPortMapping = dict[str, list[dict[str, str]] | None]


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
    ssh_ok: bool
    vnc_ok: bool
    playwright_ok: bool

    @property
    def all_ok(self) -> bool:
        return self.ssh_ok and self.vnc_ok and self.playwright_ok


@dataclass(frozen=True)
class SSHResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class VerificationResult:
    ssh: bool
    vnc: bool
    playwright: bool

    @property
    def all_passed(self) -> bool:
        return self.ssh and self.vnc and self.playwright
