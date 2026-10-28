"""Folder layout of a side-by-side self-update.

A manual install is ``<base>/controller_client``; every update installs into a
new sibling ``<base>/test-runner-<version>[-<n>]/controller_client``. The base
folder is the person's own folder and never changes or gets deleted, so update
folders never nest. Only the old install (the old ``controller_client`` of a
manual install, or the whole old ``test-runner-*`` folder) is ever removed, and
only after ``validate_old_target`` accepted it. A ``test-runner-*`` folder
counts as an update folder only when it holds the marker file the update wrote
(``MANAGED_MARKER_NAME``); a person's own folder that happens to have that name
is treated like any manual install.
"""

import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from controller_client.config import API_KEY_ENV_NAME
from controller_client.exceptions import UpdateError
from controller_client.protocol import version_tuple

PACKAGE_DIR_NAME: Final[str] = "controller_client"
# The package this process runs from; the only source of the "old install"
# path (never the server).
RUNNING_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
MANAGED_ROOT_PREFIX: Final[str] = "test-runner-"
_MANAGED_ROOT_RE: Final[re.Pattern[str]] = re.compile(
    r"test-runner-[0-9]+(?:\.[0-9]+)*(?:-[0-9]+)?"
)
# Entries that mark a source checkout (the repo root holds controller_client/
# next to the server). Updating there would delete the repo's runner.
_SOURCE_CHECKOUT_MARKERS: Final[tuple[str, ...]] = (".git", "manage.py", "auto_tester")
_MAX_NAME_SUFFIX: Final[int] = 100
# A venv is about 300 MB and a new Playwright version downloads its browsers.
MIN_FREE_BYTES: Final[int] = 1536 * 1024 * 1024
START_SCRIPT_POSIX: Final[str] = "start-test-runner.sh"
START_SCRIPT_WINDOWS: Final[str] = "start-test-runner.cmd"
ENV_FILE_NAME: Final[str] = ".env"
# Written into every folder an update creates; holds the version.
MANAGED_MARKER_NAME: Final[str] = ".punk-hazard-runner"


@dataclass(frozen=True)
class UpdateLayout:
    # The person's folder; holds the start script, never deleted.
    base: Path
    # <base>/test-runner-<version>[-<n>]; does not exist yet.
    new_root: Path
    # What the new runner deletes once the old process exited.
    old_target: Path

    @property
    def new_package_dir(self) -> Path:
        return self.new_root / PACKAGE_DIR_NAME


def is_managed_root(path: Path) -> bool:
    """A ``test-runner-*`` folder an update created (it holds the marker)."""
    if _MANAGED_ROOT_RE.fullmatch(path.name) is None:
        return False
    marker = path / MANAGED_MARKER_NAME
    return marker.is_file() and not marker.is_symlink()


def write_managed_marker(root: Path, version: str) -> None:
    """Mark ``root`` as a folder this update created."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if sys.platform == "win32":
        flags |= os.O_BINARY
    fd = os.open(root / MANAGED_MARKER_NAME, flags, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(f"{version}\n".encode("ascii"))


def base_for_install_root(install_root: Path) -> Path:
    """The folder update folders are created in, for an install root."""
    return install_root.parent if is_managed_root(install_root) else install_root


def venv_python(package_dir: Path) -> Path:
    if sys.platform == "win32":
        return package_dir / ".venv" / "Scripts" / "python.exe"
    return package_dir / ".venv" / "bin" / "python"


def source_checkout_marker(folder: Path) -> str | None:
    """Name of the entry that marks ``folder`` as a source checkout, if any."""
    for marker in _SOURCE_CHECKOUT_MARKERS:
        if os.path.lexists(folder / marker):
            return marker
    return None


def refuse_source_checkout(install_root: Path, base: Path) -> None:
    for folder in (install_root, base):
        marker = source_checkout_marker(folder)
        if marker is not None:
            raise UpdateError(
                "This Test Runner runs from a source checkout "
                f"({folder / marker} exists); update it with git instead"
            )


def plan_layout(current_package_dir: Path, target_version: str) -> UpdateLayout:
    """Where the new version goes and what is deleted afterwards.

    Raises UpdateError for an unexpected install shape or a source checkout.
    """
    if version_tuple(target_version) is None:
        raise UpdateError(f"Invalid target version {target_version[:40]!r}")
    package_dir = current_package_dir.resolve()
    if package_dir.name != PACKAGE_DIR_NAME:
        raise UpdateError(
            f"Unexpected install folder {package_dir}: it is not named "
            f"{PACKAGE_DIR_NAME}"
        )
    install_root = package_dir.parent
    if install_root.parent == install_root:
        raise UpdateError(
            f"Refusing to update an install at the drive root {install_root}"
        )
    managed = is_managed_root(install_root)
    base = install_root.parent if managed else install_root
    refuse_source_checkout(install_root, base)
    old_target = install_root if managed else package_dir
    return UpdateLayout(
        base=base,
        new_root=_free_root_name(base, target_version),
        old_target=old_target,
    )


def _free_root_name(base: Path, target_version: str) -> Path:
    name = f"{MANAGED_ROOT_PREFIX}{target_version}"
    candidate = base / name
    suffix = 2
    while os.path.lexists(candidate):
        if suffix > _MAX_NAME_SUFFIX:
            raise UpdateError(
                f"Too many earlier update folders named {name}-* in {base}; "
                "delete the ones you do not need"
            )
        candidate = base / f"{name}-{suffix}"
        suffix += 1
    return candidate


def check_free_space(base: Path) -> None:
    free = shutil.disk_usage(base).free
    if free < MIN_FREE_BYTES:
        raise UpdateError(
            f"Not enough free disk space in {base}: {free // (1024 * 1024)} MiB "
            f"free, the update needs {MIN_FREE_BYTES // (1024 * 1024)} MiB"
        )


def install_env_file(
    old_package_dir: Path, new_package_dir: Path, cli_api_key: str | None
) -> bool:
    """Write the new install's ``.env`` (owner-only on POSIX); False if none.

    The old ``.env`` is copied byte for byte. ``cli_api_key`` is the Connection
    key the old runner got on its command line (it is never passed on the new
    runner's command line): when the copy does not already hold exactly that
    key, its ``CONTROLLER_API_KEY`` line is set to it.
    """
    source = old_package_dir / ENV_FILE_NAME
    content = source.read_bytes() if source.is_file() else b""
    if cli_api_key:
        content = _with_api_key(content, cli_api_key)
    if not content:
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if sys.platform == "win32":
        flags |= os.O_BINARY
    fd = os.open(new_package_dir / ENV_FILE_NAME, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    return True


def _with_api_key(content: bytes, api_key: str) -> bytes:
    """``content`` with its API key assignment set to ``api_key``.

    Parsed the way python-decouple reads ``.env`` (``KEY = value``, optional
    quotes, ``#`` comment lines); unchanged when it already holds that key.
    """
    if any(c in api_key for c in "\r\n\x00"):
        raise UpdateError("The Connection key given on the command line is invalid")
    # surrogateescape: bytes that are not UTF-8 survive the round trip as-is.
    lines = content.decode("utf-8", "surrogateescape").splitlines(keepends=True)
    kept: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            kept.append(line)
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != API_KEY_ENV_NAME:
            kept.append(line)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        current.append(value)
    if current and all(value == api_key for value in current):
        return content
    if kept and not kept[-1].endswith(("\n", "\r")):
        kept.append("\n")
    kept.append(f"{API_KEY_ENV_NAME}={api_key}\n")
    return "".join(kept).encode("utf-8", "surrogateescape")


def validate_old_target(target: Path, new_root: Path) -> Path:
    """Return the resolved ``target`` if it is an old runner install that may go.

    Raises UpdateError otherwise. The rules are deliberately redundant: this is
    the only check between a path and ``shutil.rmtree``.
    """
    if target.is_symlink():
        raise UpdateError(f"{target} is a symbolic link")
    resolved = target.resolve()
    new = new_root.resolve()
    if not resolved.is_dir():
        raise UpdateError(f"{resolved} is not an existing folder")
    if resolved == new or resolved in new.parents or new in resolved.parents:
        raise UpdateError(f"{resolved} overlaps the new install {new}")
    if resolved.parent == resolved or resolved == Path.home().resolve():
        raise UpdateError(f"{resolved} is a drive root or the home folder")
    base = new.parent
    if resolved == base:
        raise UpdateError(f"{resolved} is the base folder")
    # Both a manual controller_client and an old test-runner-* folder sit
    # directly in the base folder, next to the new install.
    if resolved.parent != base:
        raise UpdateError(f"{resolved} is not next to the new install in {base}")
    refuse_source_checkout(resolved.parent, base)
    if resolved.name == PACKAGE_DIR_NAME:
        if (
            not (resolved / "protocol.py").is_file()
            or not (resolved / ".venv").is_dir()
        ):
            raise UpdateError(f"{resolved} does not look like a Test Runner install")
        if source_checkout_marker(resolved) is not None:
            raise UpdateError(f"{resolved} looks like a source checkout")
    elif is_managed_root(resolved):
        # Only a folder an update created (it holds the marker).
        if not (resolved / PACKAGE_DIR_NAME / "protocol.py").is_file():
            raise UpdateError(f"{resolved} does not look like a Test Runner install")
        if source_checkout_marker(resolved) is not None:
            raise UpdateError(f"{resolved} looks like a source checkout")
    else:
        raise UpdateError(f"{resolved} is not a Test Runner folder")
    return resolved


def start_script_path(base: Path) -> Path:
    name = START_SCRIPT_WINDOWS if sys.platform == "win32" else START_SCRIPT_POSIX
    return base / name


def start_script_text(new_root: Path) -> str:
    python = venv_python(new_root / PACKAGE_DIR_NAME)
    if sys.platform == "win32":
        # cmd expands %VAR% even inside quotes; paths cannot contain quotes.
        root = str(new_root).replace("%", "%%")
        exe = str(python).replace("%", "%%")
        return (
            "@echo off\r\n"
            "rem Written by the Test Runner update: starts the current Test Runner.\r\n"
            "chcp 65001 >nul\r\n"
            "setlocal\r\n"
            f'cd /d "{root}" || exit /b 1\r\n'
            f'"{exe}" -m controller_client.main %*\r\n'
        )
    return (
        "#!/bin/sh\n"
        "# Written by the Test Runner update: starts the current Test Runner.\n"
        f"cd {shlex.quote(str(new_root))} || exit 1\n"
        f'exec {shlex.quote(str(python))} -m controller_client.main "$@"\n'
    )


def write_start_script(base: Path, new_root: Path) -> Path:
    """(Re)write ``<base>/start-test-runner.{sh,cmd}`` atomically; returns its path."""
    target = start_script_path(base)
    text = start_script_text(new_root)
    fd, temp_name = tempfile.mkstemp(
        dir=base, prefix=".start-test-runner.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if sys.platform != "win32":
            os.chmod(temp, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target
