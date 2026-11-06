"""The test machine's OS and shell, from the runner's system info.

A leaf module (no imports from ``agents.services``) so ``projects`` can use it
without the import cycle through ``agents/services/__init__.py``.
"""

from controller_client.protocol import is_mobile_target


def get_os_name(system_info: dict[str, object] | None) -> str:
    if not system_info:
        return "Linux"
    return str(system_info.get("os", "Linux"))


def shell_label(os_name: str, *, on_runner_computer: bool = False) -> str:
    """The shell a person runs a command step in (the same one the agent uses).

    ``on_runner_computer``: the runner drives a phone or tablet, so the shell
    is the host computer's, not the device's.
    """
    label = "Command Prompt (cmd.exe)" if os_name == "Windows" else "Terminal (bash)"
    if on_runner_computer:
        return f"{label} on the Test Runner computer"
    return label


def runner_shell_label(system_info: dict[str, object] | None) -> str:
    """``shell_label`` of the runner that sent ``system_info``."""
    return shell_label(
        get_os_name(system_info),
        on_runner_computer=is_mobile_target(system_info or {}),
    )
