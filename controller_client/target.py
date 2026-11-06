"""The target this runner drives: the host desktop or one phone or tablet.

``desktop.py`` keeps the host's desktop backend (the person window, hotkeys
and the app closer always need it). ``main`` selects a phone target once,
after loading the config; every caller that acts on the target asks here.
"""

from __future__ import annotations

from controller_client.desktop import get_desktop_backend
from controller_client.desktop_backend import DesktopBackend
from controller_client.mobile_device import MobileDevice, MobileTargetBackend
from controller_client.protocol import RunnerTarget

_mobile: MobileTargetBackend | None = None


def select_mobile_target(backend: MobileTargetBackend) -> None:
    """Make ``backend`` the target; called once by ``main`` at startup."""
    global _mobile
    _mobile = backend


def get_target_backend() -> DesktopBackend:
    """The backend of the screen the agent sees: the phone, else the host."""
    if _mobile is not None:
        return _mobile
    return get_desktop_backend()


def get_mobile_device() -> MobileDevice | None:
    """The phone or tablet, or None on a desktop target."""
    return _mobile


def current_target() -> RunnerTarget:
    if _mobile is None:
        return RunnerTarget.DESKTOP
    return _mobile.target
