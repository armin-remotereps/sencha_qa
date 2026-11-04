"""Checks for ``python -m controller_client.diagnose --window``.

The Test Runner window (where a person answers Human in the loop steps on the
test machine) is a Tk program in its own process. The automatic checks say
whether this Python has tkinter, which Tk it has, which display Tk would use,
whether a Tk window opens in a separate process the way the runner starts
it, and whether the window renders a sample step, test case, log and
discussion, and a sample Manual run (case list, a claimed case with marked
steps, thumbnails, the mark sheet and upload progress, then the same case
paused with its discussion), without errors (in a window that is never
shown), and whether the Manual run Pass / Fail shortcuts register system-wide
(with questions: also that a press arrives). The interactive check then shows
that sample (a step, its test case and log, and a paused case's discussion)
in the real window, asks whether it is visible, hides it the way the runner
does before every agent action, takes one screenshot with the desktop
backend and asks the person to
confirm the window is not in it.

Nothing here talks to the server; the window's buttons are not sent anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from PIL import Image

from controller_client.desktop import get_desktop_backend
from controller_client.diagnostics import DiagnosticStep
from controller_client.person_window import PersonWindow, probe_person_window
from controller_client.person_window_ipc import (
    CaseRequestKind,
    ManualDraftTexts,
    ManualDraftView,
    ManualMarkView,
    ManualStateCommand,
    ManualThumbnailCommand,
    ManualUploadView,
    ManualWindowState,
    StateCommand,
    WindowCommand,
    encode_command,
)
from controller_client.protocol import (
    CasePauseActionPayload,
    ContinueModeWire,
    DiscussionMessageKind,
    ManualActiveCasePayload,
    ManualCaseListItemPayload,
    ManualCaseVerdictWire,
    ManualDiscussionPayload,
    ManualRunInfoPayload,
    ManualStepOutcome,
    ManualStepPayload,
    PersonCasePausePayload,
    PersonCaseRole,
    PersonCaseStatePayload,
    PersonCaseStepPayload,
    PersonChatSendPayload,
    PersonDiscussionMessagePayload,
    PersonHelpPayload,
    PersonPlanDraftPayload,
    PersonPlanDraftRowPayload,
    PersonStepActionResultPayload,
    PersonStepStatePayload,
    PersonStepWireKind,
    PersonStepWireStatus,
)

_PROBE_TIMEOUT_S: Final[float] = 10.0
_RENDER_TIMEOUT_S: Final[float] = 30.0
# A pending registration (a desktop dialog, 60 s) plus the key press (20 s).
_SHORTCUT_PROBE_TIMEOUT_S: Final[float] = 100.0
_APP_MODULE: Final[str] = "controller_client.person_window_app"
# ``-m controller_client...`` resolves from the directory holding the package.
_PACKAGE_PARENT: Final[Path] = Path(__file__).resolve().parent.parent
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


def _check_window_renders() -> str:
    # The real window code in a separate process, never shown: renders the
    # sample twice (the second adds a chat row, as a snapshot would) and
    # visits every tab and both text sizes.
    commands: list[WindowCommand] = [
        StateCommand(steps=steps, cases=cases)
        for steps, cases in (_sample_state(), _sample_state(extra_message=True))
    ]
    # A manual run: the case list, then a claimed case with a thumbnail,
    # then the same case paused with its discussion (twice: a new row).
    commands.append(ManualStateCommand(state=_sample_manual_state(claimed=False)))
    commands.append(ManualStateCommand(state=_sample_manual_state(claimed=True)))
    commands.extend(
        ManualThumbnailCommand(key=key, thumbnail_b64=_sample_thumbnail())
        for key in (_SAMPLE_SHOT_KEY, _SAMPLE_SHOT_KEY_2)
    )
    for extra in (False, True):
        commands.append(
            ManualStateCommand(
                state=_sample_manual_state(
                    claimed=True, paused=True, extra_message=extra
                )
            )
        )
    lines = b"".join(encode_command(command) for command in commands)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", _APP_MODULE, "--self-check"],
            input=lines,
            capture_output=True,
            timeout=_RENDER_TIMEOUT_S,
            check=False,
            cwd=str(_PACKAGE_PARENT),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"rendering did not finish within {_RENDER_TIMEOUT_S:.0f} s"
        ) from e
    out = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        err = completed.stderr.decode("utf-8", errors="replace").strip()
        detail = out or (err.splitlines()[-1] if err else "")
        raise RuntimeError(
            f"the window failed to render the sample (exit code "
            f"{completed.returncode}): {detail}"
        )
    return out


def _check_shortcuts(interactive: bool) -> str:
    # The window process registers the Manual run Pass / Fail shortcuts and
    # releases them. Interactive: it waits for the tester to press Pass (its
    # prompt comes on stderr, which is the terminal's) and a Linux desktop
    # may show its permission dialog first. Shortcuts that work only while
    # the window is focused are a warning: Pass / Fail still work. A probe
    # that fails or hangs is a warning too, so the later window checks still
    # run (the window itself works without shortcuts).
    command = [sys.executable, "-m", _APP_MODULE, "--probe-shortcuts"]
    if interactive:
        command.append("--interactive")
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=None if interactive else subprocess.PIPE,
            text=True,
            timeout=_SHORTCUT_PROBE_TIMEOUT_S,
            check=False,
            cwd=str(_PACKAGE_PARENT),
        )
    except subprocess.TimeoutExpired:
        return (
            f"WARNING: the shortcut check did not finish within "
            f"{_SHORTCUT_PROBE_TIMEOUT_S:.0f} s. The Pass / Fail buttons work "
            "anyway."
        )
    lines = completed.stdout.strip().splitlines()
    last = lines[-1] if lines else ""
    if completed.returncode != 0 or "\t" not in last:
        return (
            f"WARNING: the shortcut check failed (exit code "
            f"{completed.returncode}): {last or 'no output'}. The Pass / Fail "
            "buttons work anyway."
        )
    state, detail = last.split("\t", 1)
    if state == "active":
        return f"system-wide ({detail})"
    if state == "focus_only":
        return (
            f"WARNING: only while the Test Runner window is focused: {detail}. "
            "The Pass / Fail buttons work anyway."
        )
    return f"WARNING: not available ({state}): {detail}"


def build_window_steps(*, interactive: bool = False) -> tuple[DiagnosticStep, ...]:
    """The automatic checks, in order; each later one needs the earlier ones.

    ``interactive``: the shortcut check also waits for a key press.
    """
    return (
        DiagnosticStep("tkinter and Tk version", _check_tkinter),
        DiagnosticStep("display", _check_display),
        DiagnosticStep("Tk window", _check_window_opens),
        DiagnosticStep("window renders a sample case", _check_window_renders),
        DiagnosticStep("Manual shortcuts", lambda: _check_shortcuts(interactive)),
    )


if sys.platform == "win32":
    _SAMPLE_SHELL: Final[str] = "Command Prompt (cmd.exe)"
else:
    _SAMPLE_SHELL = "Terminal (bash)"


_SAMPLE_STEP_PIVOT: Final[int] = 1
_SAMPLE_HELP_PIVOT: Final[int] = 2
_SAMPLE_LOG_LINES: Final[int] = 120


def _sample_step() -> PersonStepStatePayload:
    return PersonStepStatePayload(
        person_step_id=1,
        pivot_id=_SAMPLE_STEP_PIVOT,
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
        case_steps_label="Case steps 2–3",
        case_steps=_SAMPLE_CASE_STEPS[1:3],
    )


_SAMPLE_CASE_STEPS: Final[tuple[PersonCaseStepPayload, ...]] = (
    PersonCaseStepPayload(num=1, text="Log in to the sample app."),
    PersonCaseStepPayload(
        num=2, text="Open a terminal.", expected="A terminal window opens."
    ),
    PersonCaseStepPayload(
        num=3,
        text="Print the home folder.",
        expected="The output is a folder path.",
    ),
    PersonCaseStepPayload(num=4, text="Close the terminal."),
)


def _sample_log() -> str:
    return "\n".join(
        f"[diagnose] sample log line {number}: nothing is sent to Punk Hazard"
        for number in range(1, _SAMPLE_LOG_LINES + 1)
    )


def _sample_messages(extra: bool) -> tuple[PersonDiscussionMessagePayload, ...]:
    messages = [
        PersonDiscussionMessagePayload(
            message_id=1,
            kind=DiscussionMessageKind.USER,
            author="diagnose --window",
            text="The dialog looks wrong; let's talk about it.",
            created_at="2026-01-01T10:00:00+00:00",
        ),
        PersonDiscussionMessagePayload(
            message_id=2,
            kind=DiscussionMessageKind.NOTICE,
            author="",
            text="The agent ran: Take a screenshot",
            created_at="2026-01-01T10:00:20+00:00",
        ),
        PersonDiscussionMessagePayload(
            message_id=3,
            kind=DiscussionMessageKind.ASSISTANT,
            author="",
            text="The dialog is a sample. I suggest continuing on the current "
            "screen with the redesigned plan below.",
            created_at="2026-01-01T10:01:00+00:00",
        ),
    ]
    if extra:
        messages.append(
            PersonDiscussionMessagePayload(
                message_id=4,
                kind=DiscussionMessageKind.USER,
                author="diagnose --window",
                text="Thanks, that is all.",
                created_at="2026-01-01T10:02:00+00:00",
            )
        )
    return tuple(messages)


def _sample_cases(extra_message: bool) -> tuple[PersonCaseStatePayload, ...]:
    pause = PersonCasePausePayload(
        can_pause=True,
        pause_blocker="",
        pause_pending=False,
        pause_requested_by="",
        paused=False,
        paused_by="",
        pause_message_state="",
        pause_message_note="",
    )
    step_case = PersonCaseStatePayload(
        pivot_id=_SAMPLE_STEP_PIVOT,
        role=PersonCaseRole.STEP,
        run_name="diagnose --window",
        case_title="Sample step (nothing is sent to Punk Hazard)",
        status_label="Your turn",
        case_url="",
        preconditions="A terminal is available on this machine.",
        steps_text="",
        expected_text="The home folder is printed.",
        numbered_steps=_SAMPLE_CASE_STEPS,
        log_tail=_sample_log(),
        pause=pause,
    )
    help_case = PersonCaseStatePayload(
        pivot_id=_SAMPLE_HELP_PIVOT,
        role=PersonCaseRole.HELP,
        run_name="diagnose --window",
        case_title="Sample paused case (nothing is sent to Punk Hazard)",
        status_label="Need help",
        case_url="",
        preconditions="",
        steps_text="1. Open the sample dialog.\n2. Check its title.",
        expected_text="",
        numbered_steps=(),
        log_tail="[diagnose] the case was paused",
        pause=PersonCasePausePayload(
            can_pause=False,
            pause_blocker="The case is already paused.",
            pause_pending=False,
            pause_requested_by="",
            paused=True,
            paused_by="diagnose --window",
            pause_message_state="sent",
            pause_message_note="",
        ),
        help=PersonHelpPayload(
            help_request_id=1,
            step_num=2,
            reason="A person paused the case.",
            ask="",
            fault_label="",
            paused_by="diagnose --window",
            continue_mode=ContinueModeWire.CURRENT_SCREEN,
            continue_mode_choices=(
                ContinueModeWire.CURRENT_SCREEN,
                ContinueModeWire.RESTORE,
                ContinueModeWire.FROM_SCRATCH,
            ),
            agent_continue_mode=ContinueModeWire.CURRENT_SCREEN,
            can_continue=True,
            blockers=(),
            proposal_label="Continue",
            proposal_note="Continue on the current screen.",
            plan_draft=PersonPlanDraftPayload(
                revision_id=1,
                reason="Check the title before closing the dialog.",
                resume_step=2,
                carried_count=1,
                rows=(
                    PersonPlanDraftRowPayload(
                        num=1,
                        description="Open the sample dialog.",
                        expected_result="The dialog shows.",
                        carried_from=1,
                        state_label="Carried from step 1",
                    ),
                    PersonPlanDraftRowPayload(
                        num=2,
                        description="Check its title.",
                        expected_result="The title is 'Sample'.",
                        carried_from=0,
                        state_label="Runs",
                    ),
                ),
            ),
            thread_url="",
            turn_live=False,
            can_send=True,
            send_blocker="",
            messages=_sample_messages(extra_message),
        ),
    )
    return (step_case, help_case)


def _sample_state(
    extra_message: bool = False,
) -> tuple[tuple[PersonStepStatePayload, ...], tuple[PersonCaseStatePayload, ...]]:
    return (_sample_step(),), _sample_cases(extra_message)


_SAMPLE_MANUAL_PIVOT: Final[int] = 11
_SAMPLE_CLAIM: Final[str] = "diagnose-claim"
_SAMPLE_SHOT_KEY: Final[str] = "diagnose-shot-1"
_SAMPLE_SHOT_KEY_2: Final[str] = "diagnose-shot-2"


def _sample_thumbnail() -> str:
    image = Image.new("RGB", (160, 100), "#64748b")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _sample_manual_discussion(extra: bool) -> ManualDiscussionPayload:
    """The claimed manual case's discussion (MANUAL_PAUSE_DISCUSS_V1)."""
    messages = [
        PersonDiscussionMessagePayload(
            message_id=11,
            kind=DiscussionMessageKind.USER,
            author="diagnose --window via Test Runner",
            text="Step 2 shows 'Example' instead of 'Sample'. Is that expected?",
            created_at="2026-01-01T10:05:00+00:00",
        ),
        PersonDiscussionMessagePayload(
            message_id=12,
            kind=DiscussionMessageKind.NOTICE,
            author="",
            text="The agent searched the knowledge base",
            created_at="2026-01-01T10:05:10+00:00",
        ),
        PersonDiscussionMessagePayload(
            message_id=13,
            kind=DiscussionMessageKind.ASSISTANT,
            author="",
            text="This is a sample answer; nothing is sent to Punk Hazard. The "
            "case expects 'Sample', so step 2 failing is right.",
            created_at="2026-01-01T10:05:40+00:00",
        ),
    ]
    if extra:
        messages.append(
            PersonDiscussionMessagePayload(
                message_id=14,
                kind=DiscussionMessageKind.USER,
                author="diagnose --window via Test Runner",
                text="Thanks.",
                created_at="2026-01-01T10:06:00+00:00",
            )
        )
    return ManualDiscussionPayload(
        thread_url="",
        turn_live=not extra,
        can_send=extra,
        send_blocker="" if extra else "Wait for the agent to answer.",
        messages=tuple(messages),
    )


def _sample_manual_state(
    claimed: bool, paused: bool = False, extra_message: bool = False
) -> ManualWindowState:
    """A Manual run as the window shows it (nothing is sent anywhere).

    ``paused``: the claimed case paused, with its discussion.
    """
    cases = (
        ManualCaseListItemPayload(
            pivot_id=_SAMPLE_MANUAL_PIVOT,
            title="Sample manual case (nothing is sent to Punk Hazard)",
            testrail_id="101",
            status="in_progress" if claimed else "created",
            claimed_label="diagnose --window" if claimed else "",
        ),
        ManualCaseListItemPayload(
            pivot_id=12,
            title="Second sample case",
            testrail_id="",
            status="created",
            claimed_label="",
        ),
        ManualCaseListItemPayload(
            pivot_id=13,
            title="A case already done",
            testrail_id="103",
            status="success",
            claimed_label="",
        ),
    )
    active = ManualActiveCasePayload(
        pivot_id=_SAMPLE_MANUAL_PIVOT,
        claim_token=_SAMPLE_CLAIM,
        title="Sample manual case (nothing is sent to Punk Hazard)",
        preconditions="The sample app is installed.",
        steps=(
            ManualStepPayload(
                num=1, text="Open the sample app.", expected="The app opens."
            ),
            ManualStepPayload(
                num=2,
                text="Open the About dialog.",
                expected="The title is 'Sample'.",
            ),
            # The step shown first (the first one not marked).
            ManualStepPayload(num=3, text="Close the app."),
        ),
        claimed_at="2026-01-01T10:00:00+00:00",
        paused=paused,
        paused_by="diagnose --window via Test Runner" if paused else "",
        paused_at="2026-01-01T10:04:00+00:00" if paused else "",
        can_pause=not paused,
        can_continue=paused,
        discussion=_sample_manual_discussion(extra_message) if paused else None,
        # The mark sheet's "Add screenshot (n of max)".
        step_max_shots=5,
    )
    draft = ManualDraftView(
        pivot_id=_SAMPLE_MANUAL_PIVOT,
        claim_token=_SAMPLE_CLAIM,
        marks=(
            # Two screenshots: the step shows half-size thumbnails.
            ManualMarkView(
                step_num=1,
                outcome=ManualStepOutcome.PASS,
                keys=(_SAMPLE_SHOT_KEY, _SAMPLE_SHOT_KEY_2),
            ),
            ManualMarkView(
                step_num=2,
                outcome=ManualStepOutcome.FAIL,
                keys=("diagnose-shot-3",),
            ),
        ),
        case_shot_key="",
        texts=ManualDraftTexts(
            notes={2: "The title was 'Example'."},
            seconds={1: 12, 2: 40},
            verdict=ManualCaseVerdictWire.FAILED,
            verdict_note="",
        ),
    )
    return ManualWindowState(
        run=ManualRunInfoPayload(run_id=1, name="diagnose --window (manual)"),
        cases=cases,
        active=active if claimed else None,
        draft=draft if claimed else None,
        upload=ManualUploadView(
            waiting_cases=1, waiting_shots=3, unsent_cases=0, sending=True
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


async def _answer_no_case_requests(window: PersonWindow) -> None:
    # Pause, Send and the help buttons get a reply too, so they come back.
    async for request in window.case_requests():
        if isinstance(request, CasePauseActionPayload):
            kind = CaseRequestKind.PAUSE
        elif isinstance(request, PersonChatSendPayload):
            kind = CaseRequestKind.CHAT
        else:
            kind = CaseRequestKind.HELP
        window.show_case_result(kind, False, "Diagnose only: nothing was sent.")


async def _run_window_check(
    ask: Callable[[str], str], report: Callable[[str], None]
) -> bool:
    window = PersonWindow()
    drain = asyncio.create_task(_answer_nothing(window))
    drain_cases = asyncio.create_task(_answer_no_case_requests(window))
    try:
        window.set_connected(True)
        steps, cases = _sample_state()
        window.apply_state(steps, cases=cases)
        report(
            "A sample step should now be shown in the Test Runner window, with "
            "the Test case, Log and Discussion tabs."
        )
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
        drain_cases.cancel()
        await window.stop()


def run_window_check(ask: Callable[[str], str], report: Callable[[str], None]) -> bool:
    """Show a sample step, hide it, screenshot; True when the person confirms."""
    return asyncio.run(_run_window_check(ask, report))
