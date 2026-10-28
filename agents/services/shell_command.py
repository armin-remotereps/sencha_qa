"""Build the command line the runner's shell runs for ``execute_command``.

The cd is built on the server, not passed to the runner, because older runners
ignore unknown payload fields; the server already knows the runner's OS.
"""

from __future__ import annotations

import re
import shlex
from typing import Final

from agents.exceptions import InvalidShellCommandError

# A home prefix bash must still expand: ``~``, ``~user``, ``$HOME`` or
# ``${HOME}``, alone or followed by ``/``. ``~user`` is limited to portable
# user-name characters so nothing else reaches the shell unquoted.
_HOME_PREFIX: Final = re.compile(r"(~[A-Za-z0-9._-]*|\$HOME|\$\{HOME\})(?=/|$)")


def build_shell_command(command: str, cwd: str, os_name: str) -> str:
    """Return ``command`` run from ``cwd`` in the runner's shell for ``os_name``."""
    if os_name == "Windows":
        return _build_cmd_command(command, cwd)
    if not cwd:
        return command
    return f"cd -- {_quote_posix_path(cwd)} && {command}"


def _quote_posix_path(cwd: str) -> str:
    """Quote ``cwd`` for bash, keeping a leading home prefix expandable."""
    match = _HOME_PREFIX.match(cwd)
    if match is None:
        return shlex.quote(cwd)
    prefix = match.group(1)
    if prefix.startswith("$"):
        # Double quotes keep $HOME expanding but stop word splitting and globbing.
        prefix = f'"{prefix}"'
    rest = cwd[match.end() :]
    if rest in ("", "/"):
        return prefix + rest
    # rest starts with "/"; quote what follows it.
    return f"{prefix}/{shlex.quote(rest[1:])}"


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
