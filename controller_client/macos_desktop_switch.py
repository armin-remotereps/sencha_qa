"""Switch to a window's Space for one capture, and back, on macOS.

Exports ``create_switcher`` (``desktop_switch.SwitcherFactory``).

macOS has no public Space API, so the switch is the documented app
activation: raise the target window through Accessibility (``kAXRaiseAction``)
and activate its app (``AXFrontmost`` plus
``NSRunningApplication.activateWithOptions``). With the Dock setting
``workspaces-auto-swoosh`` on (the default) macOS then shows the Space of
that window. Verified by the window being on screen.

The way back is the same move for an "anchor": a window on the current Space,
the focused one when its app has one here, else the topmost. Its app is
activated and its window raised, verified by the anchor being on screen
again. A Space with no window cannot be returned to this way, so the switch is
refused up front ("the current Space has no window to return to") and the
desktop is never left changed. The same holds for the other preconditions:
Accessibility granted (``AXIsProcessTrusted``) and auto-switch not turned off.

The AX references of the target and the anchor are taken before switching:
from the other Space ``AXWindows`` usually no longer lists them, but a held
reference stays valid. Ctrl+Arrow / Ctrl+Number shortcuts are not used (the
Space index is unknown and Ctrl+Number is off by default).
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    import logging
    import subprocess
    from collections.abc import Sequence
    from typing import Final

    import AppKit

    from controller_client.desktop_switch import (
        DesktopSwitcher,
        DesktopSwitchError,
        poll_until,
    )
    from controller_client.window_inventory_macos import (
        AxWindow,
        ax_app_windows,
        ax_is_trusted,
        ax_raise,
        ax_set_frontmost,
        cg_window_on_screen,
        match_ax_window,
        running_app,
    )
    from controller_client.window_model import DesktopInfo, DesktopWindow

    logger = logging.getLogger(__name__)

    _DEFAULTS: Final[str] = "/usr/bin/defaults"
    _DEFAULTS_TIMEOUT_SECONDS: Final[float] = 5.0
    _DOCK_DOMAIN: Final[str] = "com.apple.dock"
    _AUTO_SWOOSH_KEY: Final[str] = "workspaces-auto-swoosh"

    _NO_ACCESSIBILITY: Final[str] = (
        "minimised or on another Space; grant Accessibility to the Test Runner "
        "(System Settings > Privacy & Security > Accessibility) to switch"
    )
    _AUTO_SWOOSH_OFF: Final[str] = (
        "macOS is set not to switch Spaces for an app; turn on System Settings > "
        'Desktop & Dock > "When switching to an application, switch to a Space '
        'with open windows for the application"'
    )
    _NO_ANCHOR: Final[str] = "the current Space has no window to return to"
    _APP_GONE: Final[str] = "the window's app is no longer running"
    _APP_ON_THIS_SPACE: Final[str] = (
        "the window's app also has a window on this Space, and macOS would stay "
        "here; minimise or close that window, or capture it instead"
    )
    _NOT_SWITCHED: Final[str] = "macOS did not switch to the window's Space in time"
    _BAD_WINDOW: Final[str] = "the window cannot be addressed"

    def _auto_swoosh_off() -> bool:
        """True only when the Dock setting is explicitly off ("0"); an unset
        key means the default (on). An unreadable setting is not refused:
        the verification catches a switch that does not happen."""
        try:
            completed = subprocess.run(
                [_DEFAULTS, "read", _DOCK_DOMAIN, _AUTO_SWOOSH_KEY],
                capture_output=True,
                text=True,
                timeout=_DEFAULTS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Could not read the Dock's Space switching setting")
            return False
        return completed.returncode == 0 and completed.stdout.strip() in (
            "0",
            "false",
            "NO",
        )

    def _window_number(window: DesktopWindow) -> int:
        try:
            return int(window.window_id)
        except ValueError as exc:
            raise DesktopSwitchError(_BAD_WINDOW) from exc

    def _ax_window_for(window: DesktopWindow) -> AxWindow | None:
        if window.pid is None:
            return None
        return match_ax_window(window.rect, window.title, ax_app_windows(window.pid))

    def _activate(pid: int, app: AppKit.NSRunningApplication) -> None:
        ax_set_frontmost(pid)
        # IgnoringOtherApps has no effect on macOS 14+ (cooperative
        # activation); AXFrontmost above covers that case.
        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

    class _MacSwitcher:
        def __init__(self, windows: Sequence[DesktopWindow]) -> None:
            self._windows = list(windows)
            self._changed = False
            self._anchor: DesktopWindow | None = None
            self._anchor_ax: AxWindow | None = None
            self._target_ax: AxWindow | None = None

        @property
        def changed(self) -> bool:
            return self._changed

        def _pick_anchor(self) -> DesktopWindow | None:
            """A window shown on the current Space whose app can be
            activated: the focused one first, then by stacking order."""
            shown = [
                (w, w.pid)
                for w in self._windows
                if w.desktop.current and not w.minimized and w.pid is not None
            ]
            shown.sort(key=lambda pair: (not pair[0].focused, pair[0].z))
            for window, pid in shown:
                app = running_app(pid)
                if (
                    app is not None
                    and app.activationPolicy()
                    == AppKit.NSApplicationActivationPolicyRegular
                ):
                    return window
            return None

        def switch_to(self, window: DesktopWindow) -> None:
            # Record and check everything before changing anything.
            if not ax_is_trusted():
                raise DesktopSwitchError(_NO_ACCESSIBILITY)
            if _auto_swoosh_off():
                raise DesktopSwitchError(_AUTO_SWOOSH_OFF)
            target_number = _window_number(window)
            if window.pid is None:
                raise DesktopSwitchError(_APP_GONE)
            target_pid = window.pid
            app = running_app(target_pid)
            if app is None:
                raise DesktopSwitchError(_APP_GONE)
            anchor = self._pick_anchor()
            if anchor is None or anchor.pid is None:
                raise DesktopSwitchError(_NO_ANCHOR)
            _window_number(anchor)  # refuse an unaddressable anchor up front
            self._target_ax = _ax_window_for(window)
            if self._target_ax is None and any(
                w.pid == target_pid and w.desktop.current and not w.minimized
                for w in self._windows
            ):
                # Activation alone would just focus the app's window here.
                raise DesktopSwitchError(_APP_ON_THIS_SPACE)
            self._anchor = anchor
            self._anchor_ax = _ax_window_for(anchor)
            logger.info(
                "Switching Space for a capture (process %s, back to %s)",
                window.process,
                anchor.process,
            )

            self._changed = True
            if self._target_ax is not None:
                ax_raise(self._target_ax.element)
            _activate(target_pid, app)
            if not poll_until(lambda: cg_window_on_screen(target_number)):
                raise DesktopSwitchError(_NOT_SWITCHED)

        def restore(self) -> bool:
            if not self._changed:
                return True
            anchor = self._anchor
            try:
                if anchor is None or anchor.pid is None:
                    return False
                anchor_number = int(anchor.window_id)
                app = running_app(anchor.pid)
                if app is None:
                    logger.warning(
                        "Could not switch back: the app to return to (process "
                        "%s) has quit",
                        anchor.process,
                    )
                    return False
                if self._anchor_ax is not None:
                    ax_raise(self._anchor_ax.element)
                _activate(anchor.pid, app)
                restored = poll_until(lambda: cg_window_on_screen(anchor_number))
                if not restored:
                    logger.warning(
                        "The original Space was not shown again in time "
                        "(process %s)",
                        anchor.process,
                    )
                return restored
            except Exception:  # noqa: BLE001 - restore must never raise
                logger.warning(
                    "Switching back to the original Space failed", exc_info=True
                )
                return False
            finally:
                # Release the held AX references.
                self._anchor_ax = None
                self._target_ax = None

    def create_switcher(
        info: DesktopInfo, windows: Sequence[DesktopWindow]
    ) -> DesktopSwitcher:
        """One capture's switcher (``desktop_switch.SwitcherFactory``);
        ``info`` carries nothing on macOS (no Space index)."""
        del info
        return _MacSwitcher(windows)
