"""Build the command line the runner's shell runs for ``execute_command``.

The cd is built on the server, not passed to the runner, because older runners
ignore unknown payload fields; the server already knows the runner's OS.
"""

from __future__ import annotations

from agents.exceptions import InvalidShellCommandError


def build_shell_command(command: str, cwd: str, os_name: str) -> str:
    """Return ``command`` run from ``cwd`` in the runner's shell for ``os_name``."""
    if os_name == "Windows":
        return _build_cmd_command(command, cwd)
    if not cwd:
        return command
    return f"cd {cwd} && {command}"


def _build_cmd_command(command: str, cwd: str) -> str:
    # cmd /c stops at the first line break and silently drops the rest.
    if "\n" in command or "\r" in command:
        raise InvalidShellCommandError(
            "cmd.exe runs only the first line of a command. Put it on one line "
            "and chain the parts with &&."
        )
    if not cwd:
        return command
    # cmd.exe has no escape for '"' inside a quoted path, and no Windows path can
    # contain one. %VAR% still expands inside the quotes on purpose.
    if '"' in cwd or "\n" in cwd or "\r" in cwd:
        raise InvalidShellCommandError(
            'The working directory must not contain a double quote (") or a line break.'
        )
    return f'cd /d "{cwd}" && {command}'
