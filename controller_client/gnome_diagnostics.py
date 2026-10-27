"""Diagnose steps for the GNOME Wayland backend.

Every probe is read-only: environment variables, the Mutter bus names, one
remote control session, one frame and ``gsettings get``. ``run_diagnostics``
stops at the first failure, so the hard requirements come first and the
unattended-settings check, which only ever warns, comes last.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from typing import Final

from PIL import Image

from controller_client.diagnostics import DiagnosticStep
from controller_client.gnome_remote_session import GnomeRemoteSession
from controller_client.pipewire_frame import missing_capture_tools

_GSETTINGS_TIMEOUT_S: Final[float] = 5.0

# (schema, key, value an unattended machine needs, as gsettings prints it,
# value to pass to `gsettings set`)
_UNATTENDED_SETTINGS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("org.gnome.desktop.screensaver", "lock-enabled", "false", "false"),
    ("org.gnome.desktop.session", "idle-delay", "uint32 0", "0"),
    (
        "org.gnome.settings-daemon.plugins.power",
        "sleep-inactive-ac-type",
        "'nothing'",
        "\"'nothing'\"",
    ),
)

_AUTOLOGIN_HINT: Final[str] = (
    "For runs after a reboot, also enable GDM autologin in /etc/gdm3/custom.conf "
    "([daemon] AutomaticLoginEnable=true and AutomaticLogin=<user>)."
)


def build_gnome_steps(
    session: GnomeRemoteSession, screenshot: Callable[[], Image.Image]
) -> tuple[DiagnosticStep, ...]:
    """The Linux diagnose steps; screenshot is the backend's capture call."""

    def check_capture() -> str:
        started = time.monotonic()
        image = screenshot()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return f"{image.width}x{image.height} in {elapsed_ms} ms"

    return (
        DiagnosticStep("Wayland GNOME session", _check_wayland_gnome),
        DiagnosticStep("inside the desktop session", _check_inside_session),
        DiagnosticStep(
            "GNOME remote desktop service", lambda: _check_mutter_services(session)
        ),
        DiagnosticStep("GStreamer PipeWire tools", _check_capture_tools),
        DiagnosticStep("remote control session", session.probe),
        DiagnosticStep("screen capture", check_capture),
        DiagnosticStep("unattended settings", _check_unattended_settings),
    )


def _check_wayland_gnome() -> str:
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if session_type == "wayland" and "gnome" in desktop.lower():
        return f"{session_type}, {desktop}"
    raise RuntimeError(
        "The Test Runner on Linux needs Ubuntu's default GNOME desktop on Wayland "
        f"(found XDG_SESSION_TYPE={session_type or '<unset>'}, "
        f"XDG_CURRENT_DESKTOP={desktop or '<unset>'}). Log in to the "
        "'Ubuntu' (GNOME) session on the machine's own console, not Xorg, "
        "another desktop or a remote login, and start the runner there."
    )


def _check_inside_session() -> str:
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    if wayland_display:
        return f"WAYLAND_DISPLAY={wayland_display}"
    raise RuntimeError(
        "WAYLAND_DISPLAY is not set, so the runner was started over SSH, from a "
        "service or from another session. Start it from a terminal in the "
        "logged-in desktop so the apps it launches can open windows."
    )


def _check_mutter_services(session: GnomeRemoteSession) -> str:
    if session.mutter_services_present():
        return "org.gnome.Mutter.RemoteDesktop and org.gnome.Mutter.ScreenCast found"
    raise RuntimeError(
        "GNOME's remote desktop and screen cast services "
        "(org.gnome.Mutter.RemoteDesktop, org.gnome.Mutter.ScreenCast) are not on "
        "the session bus. This is not a GNOME Wayland session; log in to the "
        "default Ubuntu (GNOME) session and start the runner from its terminal."
    )


def _check_capture_tools() -> str:
    missing = missing_capture_tools()
    if missing is None:
        return "gst-launch-1.0 with pipewiresrc found"
    raise RuntimeError(missing)


def _check_unattended_settings() -> str:
    """Warn about lock, blanking and suspend settings; never fails."""
    to_change: list[str] = []
    unreadable: list[str] = []
    for schema, key, wanted, set_value in _UNATTENDED_SETTINGS:
        current = _gsettings_get(schema, key)
        if current is None:
            unreadable.append(f"{schema} {key}")
        elif current != wanted:
            to_change.append(f"gsettings set {schema} {key} {set_value}")
    if not to_change and not unreadable:
        return "ok"
    parts: list[str] = ["warning:"]
    if to_change:
        parts.append(
            "a lock screen, screen blanking or suspend stops unattended runs. "
            "Run: " + "; ".join(to_change) + "."
        )
    if unreadable:
        parts.append(
            "Could not read " + ", ".join(unreadable) + " with gsettings; check "
            "that the screen lock, blanking and automatic suspend are off."
        )
    parts.append(_AUTOLOGIN_HINT)
    return " ".join(parts)


def _gsettings_get(schema: str, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["gsettings", "get", schema, key],
            capture_output=True,
            text=True,
            timeout=_GSETTINGS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
