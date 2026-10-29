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


def open_shell_hint(os_name: str) -> str:
    """How a person opens ``shell_label(os_name)`` on that OS."""
    if os_name == "Windows":
        # Windows 11's "Terminal" opens PowerShell, where cmd syntax
        # (``%VAR%``) prints literally.
        return (
            "press Win+R, type cmd, press Enter (use Command Prompt, not "
            "PowerShell or Windows Terminal)"
        )
    if os_name == "Darwin":
        return "press Cmd+Space, type Terminal, press Enter"
    return "press Ctrl+Alt+T or open Terminal from Activities"


def shell_syntax_hint(os_name: str) -> str:
    """The command syntax to use in ``shell_label(os_name)``."""
    if os_name == "Windows":
        return (
            "cmd.exe syntax only: variables as %VAR% (e.g. "
            "echo %JAVA_HOME%), where to find a program, dir to list files, type "
            "to show a file; no PowerShell syntax ($env:VAR, Get-*)"
        )
    return (
        "POSIX shell syntax that works in both bash and zsh: variables as $VAR "
        "(e.g. echo $JAVA_HOME), which to find a program, ls to list files, cat "
        "to show a file"
    )
