"""Check which optional capabilities a Test Runner advertises.

An older runner does not know newer actions such as ``double_click``; it would
reply with an unknown-message error, so the server checks the capability first
and pauses the case for help with a clear "update the Test Runner" message instead.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum

from controller_client.protocol import (
    ClientCapability,
    RunnerTarget,
    WebBrowser,
    ordered_web_browsers,
    web_browsers_for,
)
from projects.exceptions import RunnerOutdatedError
from runners.models import TestRunner
from runners.types import RunnerId


def runner_supports(runner_id: RunnerId, capability: ClientCapability) -> bool:
    """Return whether the runner advertised ``capability`` (one query).

    A missing runner advertises nothing.
    """
    capabilities = (
        TestRunner.objects.filter(pk=runner_id)
        .values_list("capabilities", flat=True)
        .first()
    )
    return capabilities_include(capabilities, capability)


def capabilities_include(capabilities: object, capability: ClientCapability) -> bool:
    """Whether a loaded ``TestRunner.capabilities`` value (a list, or a set
    of its names) lists ``capability`` (no query)."""
    return (
        isinstance(capabilities, (list, frozenset, set))
        and capability.value in capabilities
    )


def ensure_runner_supports_double_click(runner_id: RunnerId) -> None:
    """Raise ``RunnerOutdatedError`` when the runner cannot double-click."""
    if runner_supports(runner_id, ClientCapability.DOUBLE_CLICK_V1):
        return
    # No runner-reported version in the text: it reaches the UI and TestRail.
    raise RunnerOutdatedError(
        "This Test Runner is too old to double-click. Install a fresh Test "
        "Runner from the Test Runners page."
    )


def ensure_runner_supports_pointer_modifiers(runner_id: RunnerId) -> None:
    """Raise ``RunnerOutdatedError`` when the runner cannot hold modifier keys.

    An older runner ignores the ``modifiers`` field and would do a plain
    click, so this runs before anything is parsed or pressed.
    """
    if runner_supports(runner_id, ClientCapability.POINTER_MODIFIERS_V1):
        return
    raise RunnerOutdatedError(
        "This Test Runner is too old to hold modifier keys during a click, "
        "drag, hover or scroll. Update it from its Test Runner page."
    )


def runner_os_name(runner_id: RunnerId) -> str:
    """Return the runner-reported OS name (``"Windows"``, ...), or ``""``."""
    info = (
        TestRunner.objects.filter(pk=runner_id)
        .values_list("system_info", flat=True)
        .first()
    )
    return os_name_of(info)


def os_name_of(system_info: object) -> str:
    """The OS name in a loaded ``TestRunner.system_info`` value, or ``""``
    (no query)."""
    return str(system_info.get("os", "")) if isinstance(system_info, dict) else ""


def ensure_runner_supports_windows_shell(runner_id: RunnerId, os_name: str) -> None:
    """Raise ``RunnerOutdatedError`` when a Windows runner predates cmd.exe shells."""
    if os_name != "Windows":
        return
    if runner_supports(runner_id, ClientCapability.WINDOWS_SHELL_V1):
        return
    raise _windows_shell_outdated()


def ensure_windows_shell_supported(os_name: str, capabilities: object) -> None:
    """``ensure_runner_supports_windows_shell`` on loaded values (no query)."""
    if os_name != "Windows":
        return
    if capabilities_include(capabilities, ClientCapability.WINDOWS_SHELL_V1):
        return
    raise _windows_shell_outdated()


def _windows_shell_outdated() -> RunnerOutdatedError:
    return RunnerOutdatedError(
        "This Test Runner cannot run shell commands on Windows. Install a fresh "
        "Test Runner from the Test Runners page."
    )


class WebBrowserCheck(StrEnum):
    """Whether the WEB_BROWSER_V1 layer can serve a request (no query)."""

    OK = "ok"
    # The runner does not advertise WEB_BROWSER_V1.
    OUTDATED = "outdated"
    # The named browser is not in ``web_browsers_for(target, host_os)``.
    UNSUPPORTED = "unsupported"


def web_browser_check(
    capabilities: Collection[str],
    target: RunnerTarget,
    host_os: str,
    browser: WebBrowser | None,
) -> WebBrowserCheck:
    """Check loaded runner values (capabilities, target, ``system_info.os``)
    against the web browser layer; ``browser`` None checks only the
    capability (the runner picks the open browser)."""
    if ClientCapability.WEB_BROWSER_V1.value not in capabilities:
        return WebBrowserCheck.OUTDATED
    if browser is not None and browser not in web_browsers_for(target, host_os):
        return WebBrowserCheck.UNSUPPORTED
    return WebBrowserCheck.OK


def web_browsers_supported(
    target: RunnerTarget, host_os: str
) -> tuple[WebBrowser, ...]:
    """The browsers a runner of ``target`` on ``host_os`` serves, in a stable
    order (no query)."""
    return ordered_web_browsers(web_browsers_for(target, host_os))
