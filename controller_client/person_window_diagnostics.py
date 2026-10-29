"""Checks for ``python -m controller_client.diagnose --window``.

The Test Runner window (where a person answers Human in the loop steps on the
test machine) is a Tk program in its own process. The automatic checks say
whether this Python has tkinter, which Tk it has, which display Tk would use
and whether a Tk window opens in a separate process the way the runner starts
it. The interactive check then shows a sample step (with "How to do it"
items and a sample Ask the AI exchange) in the real window, asks
whether it is visible, hides it the way the runner does before every agent
action, takes one screenshot with the desktop backend and asks the person to
confirm the window is not in it.

Nothing here talks to the server; the window's buttons are not sent anywhere.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from controller_client.desktop import get_desktop_backend
from controller_client.diagnostics import DiagnosticStep
from controller_client.person_window import PersonWindow, probe_person_window
from controller_client.protocol import (
    PersonHowToPayload,
    PersonStepActionResultPayload,
    PersonStepQuestionPayload,
    PersonStepQuestionResultPayload,
    PersonStepQuestionWireStatus,
    PersonStepStatePayload,
    PersonStepWireKind,
    PersonStepWireStatus,
)

_PROBE_TIMEOUT_S: Final[float] = 10.0
_SCREENSHOT_NAME: Final[str] = "punk-hazard-window-check.png"

# Needs no display: whether tkinter imports, and its Tk and Tcl versions.
_IMPORT_SCRIPT: Final[str] = (
    "import tkinter\n" "print(f'Tk {tkinter.TkVersion}, Tcl {tkinter.TclVersion}')\n"
)


def tkinter_hint() -> str:
    """How to get tkinter for this Python on this OS."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.platform == "linux":
        return (
            "install it with `sudo apt-get install -y python3-tk` (or your "
            f"distribution's Tk package for Python {version})"
        )
    if sys.platform == "win32":
        return (
            "the python.org installer includes Tk: run it again, choose Modify "
            "and tick 'tcl/tk and IDLE'"
        )
    if "Cellar" in sys.base_prefix or sys.base_prefix.startswith("/opt/homebrew"):
        return f"Homebrew Python: `brew install python-tk@{version}`"
    return (
        "the python.org installer for macOS includes Tk: install Python "
        f"{version} from python.org and re-create the Test Runner's venv with it"
    )


def _run_python(script: str) -> subprocess.CompletedProcess[str]:
    """Run script with this interpreter, as the runner starts its window.

    tkinter is never imported in this process: it may be missing, and on
    macOS Tk must own the main thread of the process that uses it.
    """
    try:
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"no answer within {_PROBE_TIMEOUT_S:.0f} s; the display did not respond"
        ) from e


def _last_error_line(completed: subprocess.CompletedProcess[str]) -> str:
    lines = completed.stderr.strip().splitlines()
    return lines[-1] if lines else f"exit code {completed.returncode}"


def _check_tkinter() -> str:
    completed = _run_python(_IMPORT_SCRIPT)
    if completed.returncode != 0:
        raise RuntimeError(
            f"tkinter is missing ({_last_error_line(completed)}); "
            f"{tkinter_hint()}. Without it the Test Runner has no window and "
            "steps are answered in Punk Hazard only."
        )
    return f"{completed.stdout.strip()} ({sys.executable})"


def _check_display() -> str:
    if sys.platform == "win32":
        return "Windows desktop"
    if sys.platform == "darwin":
        return "macOS (Aqua)"
    display = os.environ.get("DISPLAY", "")
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    if wayland and display:
        # Tk has no Wayland backend; on GNOME it runs through XWayland.
        return f"Wayland {wayland}, Tk uses XWayland on DISPLAY={display}"
    if wayland:
        raise RuntimeError(
            f"Wayland {wayland} but no DISPLAY: Tk needs XWayland, which GNOME "
            "provides inside the desktop session. Start the Test Runner from a "
            "terminal in that session, not over SSH."
        )
    if display:
        return f"X11 DISPLAY={display}"
    raise RuntimeError(
        "no DISPLAY or WAYLAND_DISPLAY: start the Test Runner from a terminal "
        "inside the desktop session, not over SSH."
    )


def _check_window_opens() -> str:
    # The same probe the runner runs at start to decide whether it offers
    # the window to the server.
    result = asyncio.run(probe_person_window())
    if not result.ok:
        raise RuntimeError(result.detail)
    return f"opens in a separate process ({result.detail})"


def build_window_steps() -> tuple[DiagnosticStep, ...]:
    """The automatic checks, in order; each later one needs the earlier ones."""
    return (
        DiagnosticStep("tkinter and Tk version", _check_tkinter),
        DiagnosticStep("display", _check_display),
        DiagnosticStep("Tk window", _check_window_opens),
    )


if sys.platform == "win32":
    _SAMPLE_SHELL: Final[str] = "Command Prompt (cmd.exe)"
    _SAMPLE_OPEN_SHELL: Final[str] = "press Win+R, type cmd, press Enter"
    _SAMPLE_COMMAND: Final[str] = "echo %USERPROFILE%"
else:
    _SAMPLE_SHELL = "Terminal (bash)"
    _SAMPLE_OPEN_SHELL = "open the Terminal app"
    _SAMPLE_COMMAND = "echo $HOME"


def _sample_step() -> PersonStepStatePayload:
    return PersonStepStatePayload(
        person_step_id=1,
        round=1,
        status=PersonStepWireStatus.WAITING,
        run_name="diagnose --window",
        case_title="Sample step (nothing is sent to Punk Hazard)",
        step_label="Step 1 of 1",
        kind=PersonStepWireKind.INTERACTIVE,
        instruction=(
            "This is a sample step shown by the Test Runner diagnose. Look at "
            "it, then answer the question in the terminal. The buttons do "
            "nothing here."
        ),
        expected_result="You can read this window.",
        command="",
        shell_label=_SAMPLE_SHELL,
        objection="",
        blockers=(),
        case_url="",
        how_to=(
            PersonHowToPayload(text=f"Open a terminal: {_SAMPLE_OPEN_SHELL}."),
            PersonHowToPayload(
                text="Run this command and look for a folder path in its output.",
                command=_SAMPLE_COMMAND,
            ),
        ),
        questions=(
            PersonStepQuestionPayload(
                question_id=1,
                round=1,
                status=PersonStepQuestionWireStatus.ANSWERED,
                question="What should the output look like?",
                answer="A folder path. Nothing is sent to Punk Hazard here.",
                asked_by="diagnose --window",
            ),
        ),
    )


def _is_yes(answer: str) -> bool:
    return answer.strip().lower() in {"y", "yes"}


async def _answer_nothing(window: PersonWindow) -> None:
    # A press on the sample window gets a reply, so its buttons come back.
    async for _ in window.actions():
        window.show_result(
            PersonStepActionResultPayload(
                ok=False,
                message="Diagnose only: nothing was sent.",
                queued=False,
            )
        )


async def _answer_no_questions(window: PersonWindow) -> None:
    # The sample Ask button gets a reply too, so it comes back.
    async for _ in window.questions():
        window.show_question_result(
            PersonStepQuestionResultPayload(
                ok=False, message="Diagnose only: nothing was asked."
            )
        )


async def _run_window_check(
    ask: Callable[[str], str], report: Callable[[str], None]
) -> bool:
    window = PersonWindow()
    drain = asyncio.create_task(_answer_nothing(window))
    drain_questions = asyncio.create_task(_answer_no_questions(window))
    try:
        window.set_connected(True)
        window.apply_state((_sample_step(),), questions_enabled=True)
        report("A sample step should now be shown in the Test Runner window.")
        seen = await asyncio.to_thread(
            ask, "Do you see the Test Runner window with the sample step? [y/n] "
        )
        if not _is_yes(seen):
            report(
                "[FAIL] window shown: not seen. Check the log lines above; "
                "the window may be behind other windows or on another screen."
            )
            return False
        report("[ok] window shown")

        # The runner hides the window before every agent action, the same way.
        started = time.monotonic()
        async with window.hidden_for_action():
            hide_ms = round((time.monotonic() - started) * 1000)
            image = await asyncio.to_thread(get_desktop_backend().screenshot)
            # No step left: the window stays hidden after this block.
            window.apply_state(())
        report(f"[ok] window hidden in {hide_ms} ms")
        path = Path(tempfile.gettempdir()) / _SCREENSHOT_NAME
        await asyncio.to_thread(image.save, path)
        gone = await asyncio.to_thread(
            ask,
            f"Open {path}: is the Test Runner window absent from that "
            "screenshot? [y/n] ",
        )
        if not _is_yes(gone):
            report(
                "[FAIL] hidden before screenshots: the window was still "
                "captured. The agent would see it; report this with the "
                "screenshot."
            )
            return False
        report("[ok] hidden before screenshots")
        return True
    finally:
        drain.cancel()
        drain_questions.cancel()
        await window.stop()


def run_window_check(ask: Callable[[str], str], report: Callable[[str], None]) -> bool:
    """Show a sample step, hide it, screenshot; True when the person confirms."""
    return asyncio.run(_run_window_check(ask, report))
