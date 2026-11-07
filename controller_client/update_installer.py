"""Build the new install's own environment: venv, requirements, browsers.

Each step is a subprocess with a timeout. On failure the UpdateError detail is
the last lines of the step's output, so the panel shows why pip or Playwright
failed. The steps never touch the old install.
"""

import ast
import asyncio
import logging
import os
import shutil
import sys
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final

import psutil

from controller_client.exceptions import UpdateError
from controller_client.protocol import UPDATE_STATUS_DETAIL_MAX_LENGTH, UpdatePhase
from controller_client.update_layout import UpdateLayout, venv_python

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[UpdatePhase], Awaitable[None]]

VENV_TIMEOUT_SECONDS: Final[float] = 5 * 60
PIP_TIMEOUT_SECONDS: Final[float] = 20 * 60
CERTIFI_TIMEOUT_SECONDS: Final[float] = 60
PLAYWRIGHT_TIMEOUT_SECONDS: Final[float] = 25 * 60
# The whole GNOME capture extension re-install (pack, install, enable).
GNOME_HELPER_TIMEOUT_SECONDS: Final[float] = 60
# All step limits plus the handover wait (update_runner) must stay inside the
# whole update's UPDATE_MAX_SECONDS: 5 + 20 + 1 + 25 + 1 + 5 = 57 minutes.
_TAIL_LINES: Final[int] = 30
# Environment variables that would make a child Python use the old install.
_DROPPED_ENV: Final[tuple[str, ...]] = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")
# The bundled GNOME Shell capture extension (gnome_capture_helper/,
# gnome_shell_capture.py); the same steps as scripts/setup.sh.
GNOME_HELPER_UUID: Final[str] = "test-runner-capture@punk-hazard"
_GNOME_HELPER_DIR: Final[str] = "gnome_capture_helper"
_GNOME_ENABLED_KEY: Final[tuple[str, str]] = ("org.gnome.shell", "enabled-extensions")
_CERTIFI_CHECK: Final[str] = (
    "import os, certifi; "
    "assert os.path.isfile(certifi.where()), 'certifi bundle missing'"
)


def subprocess_environment() -> dict[str, str]:
    """The current environment minus what points a Python at the old install."""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


async def install(layout: UpdateLayout, python: Path, on_phase: PhaseCallback) -> Path:
    """Create the new venv and install into it; returns the new venv's python.

    ``python`` is the old runner's interpreter: ``-m venv`` from a venv builds
    on its base interpreter. Raises UpdateError when a step fails or times out.
    """
    package_dir = layout.new_package_dir
    cwd = layout.new_root

    await on_phase(UpdatePhase.CREATING_ENVIRONMENT)
    await _run_step(
        "Creating the virtual environment",
        [str(python), "-m", "venv", str(package_dir / ".venv")],
        cwd,
        VENV_TIMEOUT_SECONDS,
    )
    new_python = venv_python(package_dir)
    if not new_python.is_file():
        raise UpdateError(f"The new virtual environment has no Python at {new_python}")

    await on_phase(UpdatePhase.INSTALLING_DEPENDENCIES)
    # No "--upgrade pip": pip is never asked to replace itself.
    await _run_step(
        "Installing dependencies",
        [
            str(new_python),
            "-m",
            "pip",
            "install",
            "-r",
            str(package_dir / "requirements.txt"),
        ],
        cwd,
        PIP_TIMEOUT_SECONDS,
    )
    await _run_step(
        "Checking certifi",
        [str(new_python), "-c", _CERTIFI_CHECK],
        cwd,
        CERTIFI_TIMEOUT_SECONDS,
    )

    await on_phase(UpdatePhase.INSTALLING_BROWSERS)
    # No --with-deps: OS packages need sudo and stay a manual step. A no-op
    # when this Playwright version's browsers are already in the shared cache.
    await _run_step(
        "Installing browsers",
        [str(new_python), "-m", "playwright", "install"],
        cwd,
        PLAYWRIGHT_TIMEOUT_SECONDS,
    )
    await install_gnome_capture_helper(package_dir)
    return new_python


def _is_gnome_session() -> bool:
    return sys.platform.startswith("linux") and "GNOME" in (
        os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    )


async def install_gnome_capture_helper(package_dir: Path) -> None:
    """Re-install the GNOME capture extension from ``package_dir`` (GNOME only).

    ``gnome-extensions install --force`` replaces the files; the Shell keeps
    running the old copy until the next login, which ``gnome_shell_capture``
    tolerates (it accepts every helper from HELPER_VERSION_MIN on). Never
    fails the update: a missing tool or a refusal is logged, and window
    capture then reports the helper missing.
    """
    if not _is_gnome_session():
        return
    if shutil.which("gnome-extensions") is None:
        logger.warning(
            "gnome-extensions is not on PATH; the GNOME capture extension was "
            "not updated"
        )
        return
    source = package_dir / _GNOME_HELPER_DIR
    if not source.is_dir():
        logger.warning("The update has no %s folder", _GNOME_HELPER_DIR)
        return
    try:
        async with asyncio.timeout(GNOME_HELPER_TIMEOUT_SECONDS):
            with tempfile.TemporaryDirectory(prefix="test-runner-gnome-") as out:
                await _run_step(
                    "Packing the GNOME capture extension",
                    [
                        "gnome-extensions",
                        "pack",
                        str(source),
                        "--force",
                        "--out-dir",
                        out,
                    ],
                    package_dir,
                    GNOME_HELPER_TIMEOUT_SECONDS,
                )
                await _run_step(
                    "Installing the GNOME capture extension",
                    [
                        "gnome-extensions",
                        "install",
                        "--force",
                        str(Path(out) / f"{GNOME_HELPER_UUID}.shell-extension.zip"),
                    ],
                    package_dir,
                    GNOME_HELPER_TIMEOUT_SECONDS,
                )
            await _enable_gnome_capture_helper(package_dir)
    except (UpdateError, TimeoutError, OSError) as exc:
        logger.warning("Could not update the GNOME capture extension: %s", exc)


async def _enable_gnome_capture_helper(cwd: Path) -> None:
    try:
        await _run_step(
            "Enabling the GNOME capture extension",
            ["gnome-extensions", "enable", GNOME_HELPER_UUID],
            cwd,
            GNOME_HELPER_TIMEOUT_SECONDS,
        )
        return
    except UpdateError:
        # On Wayland the Shell knows a newly installed extension only after
        # the next login; mark it enabled for then.
        logger.info("The Shell does not know the capture extension yet")
    raw = await _command_output(["gsettings", "get", *_GNOME_ENABLED_KEY], cwd)
    enabled = _parse_string_array(raw) if raw is not None else None
    if enabled is None:
        logger.warning("Could not read GNOME's enabled extensions")
        return
    if GNOME_HELPER_UUID in enabled:
        return
    value = "[" + ", ".join(repr(item) for item in [*enabled, GNOME_HELPER_UUID])
    await _run_step(
        "Marking the GNOME capture extension enabled",
        ["gsettings", "set", *_GNOME_ENABLED_KEY, value + "]"],
        cwd,
        GNOME_HELPER_TIMEOUT_SECONDS,
    )


def _parse_string_array(raw: str) -> list[str] | None:
    """A GVariant ``as`` as ``gsettings get`` prints it, or None."""
    text = raw.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    try:
        value: object = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return [str(v) for v in value]


async def _command_output(command: Sequence[str], cwd: Path) -> str | None:
    """The stdout of a short command, or None when it fails."""
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=subprocess_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await process.communicate()
    except BaseException:
        await _kill_tree(process)
        raise
    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace")


async def _run_step(
    label: str, command: Sequence[str], cwd: Path, timeout: float
) -> None:
    logger.info("Update: %s", label)
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=subprocess_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.wait_for(_collect(process, tail), timeout=timeout)
    except TimeoutError as e:
        await _kill_tree(process)
        raise UpdateError(
            _detail(f"{label} timed out after {timeout / 60:.0f} min", tail)
        ) from e
    except BaseException:
        # Cancelled (the runner is stopping): do not leave pip running.
        await _kill_tree(process)
        raise
    if process.returncode != 0:
        raise UpdateError(
            _detail(f"{label} failed (exit code {process.returncode})", tail)
        )


async def _collect(process: asyncio.subprocess.Process, tail: deque[str]) -> None:
    stream = process.stdout
    if stream is not None:
        while raw := await stream.readline():
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                tail.append(line)
                logger.debug("update> %s", line)
    await process.wait()


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


def _detail(headline: str, tail: deque[str]) -> str:
    """Headline plus as much of the output's end as fits in a status detail."""
    detail = headline
    if tail:
        output = "\n".join(tail)
        room = UPDATE_STATUS_DETAIL_MAX_LENGTH - len(headline) - 2
        if room > 0:
            detail = f"{headline}: {output[-room:]}"
    return detail[:UPDATE_STATUS_DETAIL_MAX_LENGTH]
