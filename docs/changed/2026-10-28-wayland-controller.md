# Test Runner on GNOME Wayland (Ubuntu 26.04)

Date: 2026-10-28
Plan: docs/plan/feat-wayland-controller.md

## What changed
- `controller_client/desktop_backend.py`, `desktop.py`: a `DesktopBackend` Protocol for every screen and input call, picked by `sys.platform`: GNOME on Linux, pyautogui elsewhere.
- `controller_client/desktop_pyautogui.py`, `screen_capture_macos.py`: today's Windows/macOS code moved unchanged (pyautogui calls, Quartz double-click, key aliases, `screencapture`, screeninfo size).
- `controller_client/desktop_gnome.py`: the Linux backend. Input through Mutter's RemoteDesktop session, screenshots from the linked ScreenCast stream of the primary monitor, bounds checked against the last frame.
- `controller_client/gnome_remote_session.py`: dbus-fast client for `org.gnome.Mutter.RemoteDesktop` / `ScreenCast` on a private asyncio loop thread; starts, rebuilds after `Closed`, and stops sessions it abandons.
- `controller_client/pipewire_frame.py`: one frame per screenshot via `gst-launch-1.0 pipewiresrc path=<node> num-buffers=1 ! ... pngenc`.
- `controller_client/keysyms.py`: key names and characters to X keysyms.
- `controller_client/gnome_diagnostics.py`: Linux `diagnose` steps (Wayland GNOME session, inside the desktop session, Mutter services, GStreamer tools, session start, timed capture, unattended settings warning).
- `executor.py`, `screen_capture.py`, `system_info.py`, `diagnose.py`, `main.py`: call the backend; `main.py` logs its name.
- `controller_client/scripts/setup.sh`: Linux installs the GStreamer PipeWire packages instead of `gnome-screenshot`, warns on non-GNOME-Wayland sessions, and prints (never applies) the lock screen, blanking, suspend and GDM autologin settings.
- `agents/services/prompt_parts.py`: the Linux persona and environment text describe the runner's reported desktop instead of "Ubuntu 24.04 with XFCE4 on display :0".

## Why
New Ubuntu releases (25.10, 26.04) only offer GNOME on Wayland. Importing pyautogui crashed there (no X display), and pyautogui cannot capture or drive native Wayland windows anyway.

## Behaviour and contract changes
- Linux runners are GNOME-Wayland-only; Xorg and other desktops get a "needs a GNOME Wayland session" error. Windows and macOS are unchanged, except that a failing screen-size lookup now reports (0, 0) instead of failing the handshake.
- `system_info` gains `display_server` and `desktop` on Linux only. No wire message or capability change; `CLIENT_VERSION` is 0.6.1.
- New runner dependency `dbus-fast~=5.2.0` (not on Windows).
- On GNOME, clicks outside the last screenshot raise instead of being clamped. An action whose session closes midway (lock screen, monitor change) fails with a lock-screen hint instead of finishing on a rebuilt session.

## Notes for future changes
- The Mutter API is private and has no consent dialog. The lock screen and login screen end every session and refuse new ones, so unattended machines need autologin, no lock screen, and no blanking or suspend. The documented portal is the fallback if GNOME adds a caller check.
- Not yet run on a real machine. Still to confirm on the 26.04 VM (plan Verification): the D-Bus signatures (`gdbus introspect`), that `pipewiresrc path=<node id>` works, the frame latency, HiDPI clicks, and the lock-mid-run recovery.
- Typed characters missing from the active keyboard layout are dropped by mutter without an error (pyautogui did the same).
- Snap/Flatpak apps are still not found by `launch_app`, and the runner has no autostart; both are separate follow-ups.
