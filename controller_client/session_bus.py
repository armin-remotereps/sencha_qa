"""The D-Bus session bus address of the desktop session (Linux).

dbus-fast reads ``DBUS_SESSION_BUS_ADDRESS`` and otherwise falls back to X11
autolaunch files, which do not exist on Wayland; systemd user sessions always
put the bus at ``$XDG_RUNTIME_DIR/bus``. Shared by every module that talks to
the session bus (``gnome_remote_session``, ``manual_hotkeys_portal``,
``capture_session_linux``, ``gnome_shell_capture``). Stdlib only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def session_bus_address(environ: Mapping[str, str] | None = None) -> str | None:
    """The session bus address from ``environ`` (default ``os.environ``):
    ``DBUS_SESSION_BUS_ADDRESS``, else ``unix:path=$XDG_RUNTIME_DIR/bus`` when
    that socket exists, else None."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address:
        return address
    runtime_dir = env.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        socket_path = os.path.join(runtime_dir, "bus")
        if os.path.exists(socket_path):
            return f"unix:path={socket_path}"
    return None
