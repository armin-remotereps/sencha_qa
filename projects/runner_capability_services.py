"""Check which optional capabilities the connected Test Runner advertises.

An older runner does not know newer actions such as ``double_click``; it would
reply with an unknown-message error, so the server checks the capability first
and pauses the case for help with a clear "update the Test Runner" message instead.
"""

from __future__ import annotations

from controller_client.protocol import ClientCapability
from projects.exceptions import RunnerOutdatedError
from projects.models import Project


def runner_supports(project_id: int, capability: ClientCapability) -> bool:
    """Return whether the project's runner advertised ``capability``."""
    capabilities = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_capabilities", flat=True)
        .get()
    )
    return capability.value in (capabilities or [])


def ensure_runner_supports_double_click(project_id: int) -> None:
    """Raise ``RunnerOutdatedError`` when the runner cannot double-click."""
    if runner_supports(project_id, ClientCapability.DOUBLE_CLICK_V1):
        return
    # No runner-reported version in the text: it reaches the UI and TestRail.
    raise RunnerOutdatedError(
        "This Test Runner is too old to double-click. Download a fresh Test "
        "Runner from the Environment page."
    )


def runner_os_name(project_id: int) -> str:
    """Return the runner-reported OS name (``"Windows"``, ...), or ``""``."""
    info = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_system_info", flat=True)
        .get()
    )
    return str(info.get("os", "")) if isinstance(info, dict) else ""


def ensure_runner_supports_windows_shell(project_id: int, os_name: str) -> None:
    """Raise ``RunnerOutdatedError`` when a Windows runner predates cmd.exe shells."""
    if os_name != "Windows":
        return
    if runner_supports(project_id, ClientCapability.WINDOWS_SHELL_V1):
        return
    raise RunnerOutdatedError(
        "This Test Runner cannot run shell commands on Windows. Download a fresh "
        "Test Runner from the Environment page."
    )
