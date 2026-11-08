"""Is a desktop test browser open and controllable? (WEB_BROWSER_V1)

A browser the Test Runner's launcher started writes its remote control port
into its dedicated profile: Chrome and Edge ``DevToolsActivePort`` (first
line), Firefox ``MarionetteActivePort``. A port only counts when it answers
right now (Chromium: ``/json/version`` over http on 127.0.0.1; Firefox: a
TCP connect), since a crashed browser leaves a stale file behind.
"""

import json
import logging
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

DEVTOOLS_PORT_FILE: Final[str] = "DevToolsActivePort"
MARIONETTE_PORT_FILE: Final[str] = "MarionetteActivePort"
LOCALHOST: Final[str] = "127.0.0.1"
_PROBE_TIMEOUT_SECONDS: Final[float] = 0.8
_PORT_FILE_MAX_BYTES: Final[int] = 4096
_PROBE_MAX_BYTES: Final[int] = 64 * 1024
# Never through a proxy: the port is on this machine.
_NO_PROXY_OPENER: Final[urllib.request.OpenerDirector] = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)


def _read_port(path: Path) -> int | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(_PORT_FILE_MAX_BYTES)
    except OSError:
        return None
    first = data.decode("ascii", errors="ignore").strip().splitlines()
    if not first or not first[0].strip().isdigit():
        return None
    port = int(first[0].strip())
    return port if 0 < port < 65536 else None


def read_devtools_port(profile: Path) -> int | None:
    """The port in a Chrome / Edge profile's DevToolsActivePort, if any."""
    return _read_port(profile / DEVTOOLS_PORT_FILE)


def read_marionette_port(profile: Path) -> int | None:
    """The port in a Firefox profile's MarionetteActivePort, if any."""
    return _read_port(profile / MARIONETTE_PORT_FILE)


def probe_chromium(port: int) -> str | None:
    """The ``Browser`` value of ``/json/version`` (e.g. "Chrome/131.0..."),
    "" when it answers without one, None when nothing answers."""
    url = f"http://{LOCALHOST}:{port}/json/version"
    try:
        with _NO_PROXY_OPENER.open(url, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            body = response.read(_PROBE_MAX_BYTES)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    browser = decoded.get("Browser") if isinstance(decoded, dict) else None
    return browser if isinstance(browser, str) else ""


def probe_marionette(port: int) -> bool:
    """True when something accepts a TCP connection on the port."""
    try:
        with socket.create_connection(
            (LOCALHOST, port), timeout=_PROBE_TIMEOUT_SECONDS
        ):
            return True
    except OSError:
        return False


def chromium_open_port(profile: Path) -> int | None:
    """The live DevTools port of the profile's browser, else None."""
    port = read_devtools_port(profile)
    if port is None or probe_chromium(port) is None:
        return None
    return port


def firefox_open_port(profile: Path) -> int | None:
    """The live Marionette port of the profile's browser, else None."""
    port = read_marionette_port(profile)
    if port is None or not probe_marionette(port):
        return None
    return port
