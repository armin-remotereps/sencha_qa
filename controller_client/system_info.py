import platform
import socket
from dataclasses import dataclass

import pyautogui

try:
    from screeninfo import get_monitors

    _SCREENINFO_AVAILABLE = True
except ImportError:
    _SCREENINFO_AVAILABLE = False


@dataclass(frozen=True)
class SystemInfo:
    os: str
    os_version: str
    architecture: str
    hostname: str
    screen_width: int
    screen_height: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "os": self.os,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "hostname": self.hostname,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
        }


_ARCHITECTURE_MAP: dict[str, str] = {
    "x86_64": "AMD64",
    "amd64": "AMD64",
    "aarch64": "ARM64",
    "arm64": "ARM64",
}


def _normalize_architecture(arch: str) -> str:
    return _ARCHITECTURE_MAP.get(arch.lower(), arch)


def _get_screen_resolution() -> tuple[int, int]:
    if _SCREENINFO_AVAILABLE:
        try:
            monitors = get_monitors()
            if monitors:
                return monitors[0].width, monitors[0].height
        except (IndexError, RuntimeError):
            pass
    size = pyautogui.size()
    return size.width, size.height


def gather_system_info() -> SystemInfo:
    width, height = _get_screen_resolution()
    return SystemInfo(
        os=platform.system(),
        os_version=platform.version(),
        architecture=_normalize_architecture(platform.machine()),
        hostname=socket.gethostname(),
        screen_width=width,
        screen_height=height,
    )
