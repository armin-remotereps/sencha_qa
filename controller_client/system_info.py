import platform
import socket
from dataclasses import dataclass

from controller_client.desktop import get_desktop_backend


@dataclass(frozen=True)
class SystemInfo:
    os: str
    os_version: str
    architecture: str
    hostname: str
    screen_width: int
    screen_height: int
    # Extra keys from the desktop backend (Linux: display_server, desktop);
    # empty on Windows and macOS, so their handshake keeps today's keys.
    session: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, str | int]:
        return {
            "os": self.os,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "hostname": self.hostname,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            **dict(self.session),
        }


_ARCHITECTURE_MAP: dict[str, str] = {
    "x86_64": "AMD64",
    "amd64": "AMD64",
    "aarch64": "ARM64",
    "arm64": "ARM64",
}


def _normalize_architecture(arch: str) -> str:
    return _ARCHITECTURE_MAP.get(arch.lower(), arch)


def gather_system_info() -> SystemInfo:
    backend = get_desktop_backend()
    width, height = backend.screen_size()
    return SystemInfo(
        os=platform.system(),
        os_version=platform.version(),
        architecture=_normalize_architecture(platform.machine()),
        hostname=socket.gethostname(),
        screen_width=width,
        screen_height=height,
        session=tuple(backend.session_info().items()),
    )
