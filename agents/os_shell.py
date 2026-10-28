"""The test machine's OS and shell, from the runner's system info.

A leaf module (no imports from ``agents.services``) so ``projects`` can use it
without the import cycle through ``agents/services/__init__.py``.
"""


def get_os_name(system_info: dict[str, object] | None) -> str:
    if not system_info:
        return "Linux"
    return str(system_info.get("os", "Linux"))


def shell_label(os_name: str) -> str:
    """The shell a person runs a command step in (the same one the agent uses)."""
    if os_name == "Windows":
        return "Command Prompt (cmd.exe)"
    return "Terminal (bash)"
