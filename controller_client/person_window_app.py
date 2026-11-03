"""The Test Runner's person window (Human in the loop steps, help and pause).

Run by the runner as ``python -m controller_client.person_window_app``; Tk
owns this process's main thread (an AppKit requirement on macOS). Commands
arrive as JSON lines on stdin (``person_window_ipc``), read by a thread into a
queue that the Tk loop polls; events go back on stdout. The window never
talks to the server.

Every text from the server is shown as plain Tk text: nothing is interpreted
as markup. The window is never topmost, never grabs focus, starts withdrawn
and only shows when the runner says so; it raises itself once per new step
round or help request and hides itself when the person answers a step or
resolves a help request. Closing it only hides it until something new
arrives. Mark failed / Mark blocked confirm inline (the buttons turn into
Confirm / Cancel), and Pause… opens an inline panel: the window opens no
other window or dialog, so hiding it hides everything it shows.

Layout: a header (case title, run and status, Pause…, A−/A+) above a
notebook:

- Step: the selected open step (What to do, the test case steps it covers,
  How to do it, Expected result, command, the one-tap Same as expected /
  Different… report, Overrule, Mark failed / blocked, Ask the AI).
- Test case: the header case's preconditions, steps (the steps the selected
  step covers tinted) and expected results.
- Log: the last lines of the case log.
- Discussion: only for a help case. The help request, the chat with the
  agent and a message box, the agent's proposal and plan redesign, and
  Continue (mode, the redesigned plan, note) / Mark failed / Mark blocked.
- Manual run: only while the project has a Manual run
  (``person_window_manual``): the run's cases, the claimed case's verbatim
  steps with Pass / Fail screenshots and notes, the case result and Hand in.
  While it shows, closing the window minimizes it instead of hiding it (the
  tester works only here, so it must stay reachable).

Manual run shortcuts (Test Runner 0.17.0): ``ManualHotkeys`` registers the
Pass / Fail combinations system-wide while a case is claimed and not paused
(``ManualView.shortcuts_wanted``, checked from ``poll``) unless the runner
passes ``--no-manual-shortcuts``; ``poll`` drains its events into the tab.
Opening the mark sheet is the one time the window takes the focus, always
after the tester's own key press or click; after Save or Cancel the app that
was in front when a system-wide shortcut fired gets it back, unless it was
this window (Windows ``SetForegroundWindow``, macOS
``FrontAppKeeper.restore_now``; Linux leaves it to the tester).

Without ``cases`` (an older server) only the Step tab shows, as before. The
header case is the Discussion's case while that tab is selected, else the
selected step's case, else the help case, else the running case (which only
enables Pause). A step maps to its case by ``pivot_id``; a server older than
0.12.0 sends 0, and then the step's case is the "step" case with the same
case title and run name.

Every block of text is a read-only ``tk.Text`` (selectable, copyable, key
presses other than copy, select-all and moving the cursor are dropped) with
its own Copy button (``_ReadOnlyBlock``). The named fonts TkDefaultFont,
TkTextFont and TkFixedFont (and two derived ones) are scaled together by
A−/A+ from the base size the runner passes (``--text-size``), for this
session only. Long texts are rendered once per change: the log is replaced
only when its tail changes, the chat appends new rows by message id and is
re-rendered only when an earlier row changed or rows were dropped.

Showing never activates the window where the platform allows it:

- Windows: ``wm state normal`` (Tk shows with SW_SHOWNOACTIVATE and, unlike
  ``deiconify``, neither raises nor focuses) plus an explicit
  ``ShowWindow(SW_SHOWNOACTIVATE)`` on the frame; the raise per new round is
  ``SetWindowPos(HWND_TOP, SWP_NOACTIVATE)``. The frame is created hidden at
  start, so the first show goes the same way.
- macOS: Tk activates the Python app whenever it maps a window (measured;
  there is no non-activating map for a window that takes typing), so the
  runner shows it only for something new or after the agent has been idle
  for a while (``person_window``); never ``focus_force``. The app that was
  front before a show is recorded and re-activated on every hide (agent
  action, answer, close) before the hide is acknowledged
  (``person_window_front_app``), so the agent's keys reach that app.
- Linux/X11: ``deiconify`` and ``raise`` without any focus request; the window
  manager's focus-stealing prevention decides.

``--probe`` checks that tkinter imports and a Tk display opens, prints one
line and exits (0 when usable). ``--self-check`` renders the commands read
from stdin in a window that is never shown, visits every tab and both text
sizes, opens the mark sheet on the claimed sample case, and exits 0 when
nothing raised (``diagnose --window``). ``--probe-shortcuts`` registers the
Manual run shortcuts, prints ``<state><TAB><detail>`` and releases them;
with ``--interactive`` it also waits for the Pass combination (and a Linux
desktop may ask for permission).
"""

import argparse
import contextlib
import ctypes
import io
import logging
import os
import queue
import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import BinaryIO, Final

from controller_client.exceptions import ProtocolError
from controller_client.manual_hotkeys import ManualHotkeys, probe_shortcuts
from controller_client.manual_hotkeys_base import HotkeyState, HotkeyStatus
from controller_client.manual_hotkeys_x11 import prepare_x11_threads
from controller_client.person_window_chat import ChatBlock, ChatPanel
from controller_client.person_window_front_app import FrontAppKeeper
from controller_client.person_window_how_to import render_how_to
from controller_client.person_window_ipc import (
    IPC_TO_WINDOW_MAX_BYTES,
    TEXT_SIZE_DEFAULT,
    TEXT_SIZE_MAX,
    TEXT_SIZE_MIN,
    ActionEvent,
    CaseRequestKind,
    CaseResultCommand,
    ChatSendEvent,
    ClosedByUserEvent,
    ConnectionCommand,
    HelpActionEvent,
    HiddenEvent,
    HideCommand,
    ManualCaptureResultCommand,
    ManualResultCommand,
    ManualStateCommand,
    ManualThumbnailCommand,
    ManualWindowState,
    PauseEvent,
    QuestionEvent,
    QuestionResultCommand,
    QuitCommand,
    ReadyEvent,
    ResultCommand,
    ShowCommand,
    StateCommand,
    WindowCommand,
    WindowEvent,
    clamp_text_size,
    decode_command,
    encode_event,
)
from controller_client.person_window_manual import ManualBlock, ManualView
from controller_client.protocol import (
    CASE_PAUSE_MESSAGE_MAX_CHARS,
    PERSON_CHAT_TEXT_MAX_CHARS,
    PERSON_HELP_NOTE_MAX_CHARS,
    PERSON_STEP_NOTE_MAX_CHARS,
    PERSON_STEP_OUTPUT_MAX_CHARS,
    PERSON_STEP_QUESTION_MAX_CHARS,
    CasePauseActionPayload,
    ContinueModeWire,
    PersonCaseRole,
    PersonCaseStatePayload,
    PersonCaseStepPayload,
    PersonChatSendPayload,
    PersonHelpActionKind,
    PersonHelpActionPayload,
    PersonHelpPayload,
    PersonHowToPayload,
    PersonPlanDraftPayload,
    PersonStepActionKind,
    PersonStepActionPayload,
    PersonStepQuestionAskPayload,
    PersonStepQuestionPayload,
    PersonStepQuestionWireStatus,
    PersonStepStatePayload,
    PersonStepWireKind,
    PersonStepWireOutcome,
    PersonStepWireStatus,
)

_TK_IMPORT_ERROR: ImportError | None = None
try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk
except ImportError as _e:  # tkinter is an optional part of some Pythons
    _TK_IMPORT_ERROR = _e

if sys.platform == "darwin":
    try:
        import AppKit
    except ImportError:  # pyobjc comes with PyAutoGUI; guard anyway
        _HAVE_APPKIT = False
    else:
        _HAVE_APPKIT = True

logger = logging.getLogger("controller_client.person_window_app")

# Exit codes of --probe (and of a normal start that cannot open Tk).
EXIT_OK: Final[int] = 0
EXIT_NO_TKINTER: Final[int] = 3
EXIT_NO_DISPLAY: Final[int] = 4
# --self-check: rendering raised.
EXIT_RENDER_FAILED: Final[int] = 5

_POLL_MS: Final[int] = 50
# How often a hide checks whether the previous front app is back (macOS).
_RESTORE_POLL_MS: Final[int] = 10
_READ_CHUNK_BYTES: Final[int] = 64 * 1024
_TITLE: Final[str] = "Test Runner: your step"
# The pages' default width (the window is about 80 px wider) and the minimum.
_PAGE_WIDTH: Final[int] = 560
_MIN_WIDTH: Final[int] = 520
_MIN_HEIGHT: Final[int] = 360
# Room left for the title bar, menu bar and taskbar when the content is tall,
# plus the header and the notebook's tabs above a page.
_SCREEN_MARGIN: Final[int] = 160
_HEADER_ALLOWANCE: Final[int] = 150
# Width of padding and scrollbars beside a page's text (wrap estimates).
_TEXT_INSET_PX: Final[int] = 70
# Page scrolling: one wheel step, a Windows wheel notch and its steps.
_WHEEL_STEP_PX: Final[int] = 20
_WHEEL_DELTA: Final[int] = 120
_WHEEL_NOTCH_STEPS: Final[int] = 3
_SHIFT_MASK: Final[int] = 0x1
_CONTROL_MASK: Final[int] = 0x4
# Command on macOS (Mod1). On Windows Mod1 is Num Lock, so it never counts.
_COPY_MASK: Final[int] = _CONTROL_MASK | (0x8 if sys.platform == "darwin" else 0)
# Keys a read-only text still takes: moving the cursor (also with Shift to
# select), and copy / select all with the copy modifier.
_NAV_KEYS: Final[frozenset[str]] = frozenset(
    {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"}
)
_COPY_KEYS: Final[frozenset[str]] = frozenset({"c", "a", "slash", "insert"})
_EDIT_EVENTS: Final[tuple[str, ...]] = (
    "<<Paste>>",
    "<<PasteSelection>>",
    "<<Cut>>",
    "<<Clear>>",
    "<<Undo>>",
    "<<Redo>>",
)
_WARN_COLOR: Final[str] = "#b45309"
_ERROR_COLOR: Final[str] = "#b91c1c"
_OK_COLOR: Final[str] = "#15803d"
_MUTED_COLOR: Final[str] = "#6b7280"
# The steps the selected step covers (dark text on amber, readable in dark mode).
_COVERED_BACKGROUND: Final[str] = "#fde68a"
_COVERED_FOREGROUND: Final[str] = "#1f2937"
_ASK_CLOSED: Final[str] = "▸ Ask the AI"
_ASK_OPEN: Final[str] = "▾ Ask the AI"
_TEXT_SIZE_STEP: Final[int] = 2
_HEADING_FONT: Final[str] = "PunkHazardHeading"
_BOLD_FONT: Final[str] = "PunkHazardBold"
# Most lines a block grows to before it scrolls.
_BLOCK_LINES: Final[int] = 12
_CASE_STEPS_MAX_LINES: Final[int] = 10
_CASE_TAB_STEPS_LINES: Final[int] = 30
_CHAT_LINES: Final[int] = 14
_LOG_LINES: Final[int] = 20
_REFIT_DELAY_MS: Final[int] = 150
_MODE_LABELS: Final[dict[ContinueModeWire, str]] = {
    ContinueModeWire.CURRENT_SCREEN: "Continue on the current screen",
    ContinueModeWire.RESTORE: "Restore the earlier steps, then continue",
    ContinueModeWire.FROM_SCRATCH: "Start the case again from scratch",
}
_REDESIGN_ON_RESTORE: Final[str] = (
    "Not available: this case must restore its earlier steps before it "
    "continues, and a redesigned plan continues on the current screen."
)

# Win32 ShowWindow / SetWindowPos arguments.
_SW_SHOWNOACTIVATE: Final[int] = 4
_HWND_TOP: Final[int] = 0
_SWP_NOSIZE: Final[int] = 0x0001
_SWP_NOMOVE: Final[int] = 0x0002
_SWP_NOACTIVATE: Final[int] = 0x0010

_StepKey = tuple[int, int]
# Raised once: ("step", step id, round) or ("help", help request id, 0).
_RaiseKey = tuple[str, int, int]
# A step's case steps label and items, as the case steps box shows them.
_CaseStepsView = tuple[str, tuple[PersonCaseStepPayload, ...]]
# Text with its tags, inserted in order into a block.
_Segments = tuple[tuple[str, tuple[str, ...]], ...]


def _key(step: PersonStepStatePayload) -> _StepKey:
    return (step.person_step_id, step.round)


@dataclass
class _Draft:
    different: bool = False
    note: str = ""
    output: str = ""
    question: str = ""


@dataclass
class _HelpDraft:
    chat: str = ""
    note: str = ""
    # ContinueModeWire value; "" for the stored mode.
    mode: str = ""
    use_plan: bool = False


@dataclass
class _ViewState:
    steps: tuple[PersonStepStatePayload, ...] = ()
    cases: tuple[PersonCaseStatePayload, ...] = ()
    selected_id: int | None = None
    rendered: _StepKey | None = None
    drafts: dict[_StepKey, _Draft] = field(default_factory=dict)
    raised: set[_RaiseKey] = field(default_factory=set)
    # The step whose answer is with the server (buttons stay disabled).
    busy: _StepKey | None = None
    # Mark failed / Mark blocked waiting for its inline confirmation.
    confirm: PersonStepActionKind | None = None
    # "Different…" pressed: the note is shown and required.
    different: bool = False
    connected: bool = True
    # The server takes questions (Ask AI box shown).
    questions_enabled: bool = False
    # The "Ask the AI" section is expanded (one switch for every step).
    ask_open: bool = False
    # The step whose question is with the server (Ask stays disabled).
    asking: _StepKey | None = None
    # What the how-to area and the Q&A box show now (rebuilt only on change).
    rendered_how_to: tuple[PersonHowToPayload, ...] | None = None
    rendered_case_steps: _CaseStepsView | None = None
    rendered_qa: str | None = None
    # Steps and help requests seen, to pick the tab when something new comes.
    seen_steps: frozenset[_StepKey] = frozenset()
    seen_help: frozenset[int] = frozenset()
    # Test case and log tabs: what they show now (re-rendered on change).
    rendered_case: tuple[object, ...] | None = None
    rendered_log: tuple[int, int] | None = None
    # Discussion: the help request on screen, its chat rows and drafts.
    help_id: int | None = None
    rendered_help: tuple[object, ...] | None = None
    rendered_modes: tuple[object, ...] | None = None
    help_drafts: dict[int, _HelpDraft] = field(default_factory=dict)
    help_confirm: PersonHelpActionKind | None = None
    # A request of each kind with the server.
    pause_busy: bool = False
    chat_busy: bool = False
    help_busy: bool = False
    pause_open: bool = False
    # The server's reply to the last pause, shown in the header for its case.
    pause_note: str = ""
    pause_note_pivot: int | None = None
    pause_pivot: int | None = None


class _EventWriter:
    """Writes events to the runner; a broken pipe ends the window."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.broken = False

    def send(self, event: WindowEvent) -> None:
        if self.broken:
            return
        try:
            self._stream.write(encode_event(event))
            self._stream.flush()
        except (OSError, ValueError) as e:
            logger.warning("Lost the Test Runner: %s", e)
            self.broken = True


def _read_commands(fd: int, inbox: "queue.Queue[WindowCommand | None]") -> None:
    """Reader thread: stdin lines to the queue; None when the runner is gone.

    Reads the raw file descriptor (not ``sys.stdin.buffer``): a daemon thread
    blocked inside a buffered reader aborts interpreter shutdown.
    """
    pending = b""
    try:
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            if len(pending) > IPC_TO_WINDOW_MAX_BYTES:
                logger.error("Message from the Test Runner is too long; closing")
                break
            for line in lines:
                try:
                    inbox.put(decode_command(line))
                except ProtocolError as e:
                    logger.warning("Ignoring a message from the Test Runner: %s", e)
    except OSError as e:
        logger.warning("Reading from the Test Runner failed: %s", e)
    inbox.put(None)


def _editable_text(widget: "tk.Text") -> str:
    return widget.get("1.0", "end-1c")


def _set_editable_text(widget: "tk.Text", text: str) -> None:
    widget.delete("1.0", "end")
    widget.insert("1.0", text)


def _wants_output(step: PersonStepStatePayload) -> bool:
    """A pasted output helps the agent check any step with a command to run."""
    return step.kind is PersonStepWireKind.COMMAND or any(
        item.command for item in step.how_to
    )


def _line_estimate(text: str, columns: int) -> int:
    """Roughly how many wrapped lines ``text`` takes ``columns`` characters wide."""
    return sum(max(1, -(-len(line) // columns)) for line in (text.split("\n") or [""]))


def _has_pending_question(step: PersonStepStatePayload) -> bool:
    return any(q.status is PersonStepQuestionWireStatus.PENDING for q in step.questions)


def _qa_text(questions: tuple[PersonStepQuestionPayload, ...]) -> str:
    blocks: list[str] = []
    for q in questions:
        if q.status is PersonStepQuestionWireStatus.PENDING:
            answer = "The AI is thinking…"
        elif q.status is PersonStepQuestionWireStatus.FAILED:
            answer = f"AI: {q.answer or 'The AI could not answer.'}"
        else:
            answer = f"AI: {q.answer}"
        blocks.append(f"You: {q.question}\n{answer}")
    return "\n\n".join(blocks)


def _help_reason_text(help_state: PersonHelpPayload) -> str:
    lines: list[str] = []
    if help_state.paused_by:
        lines.append(f"Paused by {help_state.paused_by}.")
    if help_state.fault_label:
        lines.append(f"Problem: {help_state.fault_label}")
    if help_state.step_num:
        lines.append(f"Stopped at step {help_state.step_num}.")
    if help_state.reason:
        lines.append(help_state.reason)
    if help_state.ask:
        lines.append(f"The agent asks: {help_state.ask}")
    return "\n".join(lines) or "The case waits for a person."


def _plan_draft_segments(draft: PersonPlanDraftPayload) -> _Segments:
    segments: list[tuple[str, tuple[str, ...]]] = []
    if draft.reason:
        segments.append((f"{draft.reason}\n", ()))
    carried = (
        f"; {draft.carried_count} earlier step(s) carried"
        if draft.carried_count
        else ""
    )
    segments.append((f"Resumes at step {draft.resume_step}{carried}.", ("muted",)))
    for row in draft.rows:
        segments.append((f"\n{row.num}. {row.description}", ()))
        if row.expected_result:
            segments.append((f"\n    Expected: {row.expected_result}", ("muted",)))
        if row.state_label:
            segments.append((f"\n    {row.state_label}", ("muted",)))
    return tuple(segments)


def _segments_text(segments: _Segments) -> str:
    return "".join(text for text, _tags in segments)


def _web_url(url: str) -> bool:
    """Only http(s) links from the server open in a browser."""
    return url.startswith(("https://", "http://"))


def _readonly_key(event: "tk.Event[tk.Text]") -> str | None:
    """Drops key presses that would change a read-only text."""
    if event.keysym in _NAV_KEYS:
        return None
    state = event.state if isinstance(event.state, int) else 0
    if state & _COPY_MASK and event.keysym.lower() in _COPY_KEYS:
        return None
    if event.keysym in ("Tab", "ISO_Left_Tab"):
        # Move on instead of inserting a tab.
        target = (
            event.widget.tk_focusPrev()
            if state & _SHIFT_MASK or event.keysym == "ISO_Left_Tab"
            else event.widget.tk_focusNext()
        )
        if target is not None:
            target.focus_set()
    return "break"


def _make_readonly(widget: "tk.Text") -> None:
    """Selectable and copyable, never editable (the state stays normal so the
    selection and copy work on every platform)."""
    widget.bind("<Key>", _readonly_key)
    for sequence in _EDIT_EVENTS:
        widget.bind(sequence, lambda _event: "break")


class _ReadOnlyBlock:
    """A titled, read-only, selectable text with a Copy button.

    The text is replaced only when it changes; the box grows with its text up
    to ``max_lines`` lines, then scrolls (``fixed_lines`` keeps one height).
    """

    def __init__(
        self,
        app: "PersonWindowApp",
        parent: "ttk.Frame",
        *,
        title: str,
        max_lines: int = _BLOCK_LINES,
        mono: bool = False,
        fixed_lines: int = 0,
    ) -> None:
        self._app = app
        self._mono = mono
        self._max_lines = max_lines
        self._fixed_lines = fixed_lines
        self._segments: _Segments | None = None
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        head = ttk.Frame(frame)
        head.columnconfigure(0, weight=1)
        head.grid(row=0, column=0, sticky="ew")
        self.title = ttk.Label(head, text=title)
        self.title.grid(row=0, column=0, sticky="w")
        # Extra buttons go in the head before Copy (column 1..).
        self.head = head
        ttk.Button(head, text="Copy", command=self.copy).grid(
            row=0, column=9, sticky="e", padx=(6, 0)
        )
        holder = ttk.Frame(frame)
        holder.grid(row=1, column=0, sticky="nsew", pady=(2, 0))
        self.holder = holder
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        text = tk.Text(
            holder,
            height=fixed_lines or 1,
            width=1,
            wrap="char" if mono else "word",
            font="TkFixedFont" if mono else "TkTextFont",
            borderwidth=1,
            relief="solid",
            undo=False,
            takefocus=False,
        )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        _make_readonly(text)
        text.tag_configure("muted", foreground=_MUTED_COLOR)
        text.tag_configure("mono", font="TkFixedFont")
        text.tag_configure("warn", foreground=_WARN_COLOR)
        self.frame = frame
        self.text = text
        app.register_block(self)

    def set(self, text: str) -> bool:
        return self.set_segments(((text, ()),))

    def set_segments(self, segments: _Segments) -> bool:
        """Replace the content when it changed; True when it did."""
        if segments == self._segments:
            return False
        self._segments = segments
        text = self.text
        text.delete("1.0", "end")
        for chunk, tags in segments:
            text.insert("end", chunk, tags)
        text.yview_moveto(0.0)
        self.fit()
        return True

    def fit(self) -> None:
        if self._fixed_lines or self._segments is None:
            return
        columns = self._app.columns(self._mono)
        lines = _line_estimate(_segments_text(self._segments), columns)
        self.text.configure(height=max(1, min(lines, self._max_lines)))

    def copy(self) -> None:
        self._app.copy(self.text.get("1.0", "end-1c"))

    def show(self, visible: bool) -> None:
        if visible:
            self.frame.grid()
        else:
            self.frame.grid_remove()


class _ScrollPage:
    """A vertically scrollable page of the notebook.

    The page asks for its content's height up to the screen's, so a long page
    scrolls instead of growing off the screen. The scrollbar shows only when
    the content does not fit.
    """

    def __init__(self, parent: "tk.Misc") -> None:
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        background = ttk.Style(outer).lookup("TFrame", "background")
        canvas = tk.Canvas(
            outer,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=_WHEEL_STEP_PX,
            width=_PAGE_WIDTH,
        )
        if background:
            canvas.configure(background=background)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=self._on_canvas_scroll)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        frame = ttk.Frame(canvas, padding=12)
        frame.columnconfigure(0, weight=1)
        item = canvas.create_window(0, 0, window=frame, anchor="nw")
        frame.bind("<Configure>", self._on_content_configure)
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(item, width=event.width)
        )
        self.outer = outer
        self.canvas = canvas
        self.scrollbar = scroll
        self.frame = frame

    def _on_content_configure(self, _event: "tk.Event[ttk.Frame]") -> None:
        canvas = self.canvas
        frame = self.frame
        canvas.configure(scrollregion=canvas.bbox("all"))
        max_height = canvas.winfo_screenheight() - _SCREEN_MARGIN - _HEADER_ALLOWANCE
        canvas.configure(
            width=max(_PAGE_WIDTH, frame.winfo_reqwidth()),
            height=min(frame.winfo_reqheight(), max_height),
        )

    def _on_canvas_scroll(self, first: float | str, last: float | str) -> None:
        # Tk passes the fractions as strings.
        self.scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()


class PersonWindowApp:
    """The window's widgets and behaviour; runs on the Tk main thread."""

    def __init__(
        self,
        root: "tk.Tk",
        inbox: "queue.Queue[WindowCommand | None]",
        writer: _EventWriter,
        hotkeys: ManualHotkeys,
        *,
        text_size: int = TEXT_SIZE_DEFAULT,
    ) -> None:
        self._root = root
        self._inbox = inbox
        self._writer = writer
        self._view = _ViewState()
        self._front_app = FrontAppKeeper()
        self._hotkeys = hotkeys
        self._hotkeys_wanted = False
        self._hotkey_status: HotkeyStatus | None = None
        # Withdrawn at start; only the runner's Show / Hide change it.
        self._runner_hidden = True
        # A shortcut opened the mark sheet: give the front back after it.
        self._restore_focus = False
        # Windows: the window in front when the shortcut fired.
        self._previous_hwnd = 0
        self._picker_var = tk.StringVar(master=root)
        self._mode_var = tk.StringVar(master=root)
        self._use_plan_var = tk.BooleanVar(master=root, value=False)
        self._url_var = tk.StringVar(master=root)
        self._thread_url_var = tk.StringVar(master=root)
        # Windows wheel delta not yet scrolled (less than one notch).
        self._wheel_rest = 0
        self._blocks: list[_ReadOnlyBlock] = []
        # Labels whose wraplength follows the window width.
        self._wrapped: list[ttk.Label] = []
        self._pages: list[_ScrollPage] = []
        self._refit_job: str | None = None
        self._width = 0
        self._text_size = clamp_text_size(text_size)
        self._fonts = [
            tkfont.nametofont(name, root=root)
            for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont")
        ]
        family = self._fonts[0].actual("family")
        self._heading_font = tkfont.Font(
            root=root, name=_HEADING_FONT, family=family, weight="bold"
        )
        self._bold_font = tkfont.Font(
            root=root, name=_BOLD_FONT, family=family, weight="bold"
        )
        self._apply_text_size()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_user_close)
        if sys.platform == "darwin":
            # Cmd+Q / the app menu's Quit would end Tk; treat it as a close.
            # (Misc.register is typed; createcommand is not in typeshed.)
            close_command = root.register(self._on_user_close)
            root.tk.call("proc", "::tk::mac::Quit", "", close_command)

    # --- helpers for blocks -------------------------------------------------

    def register_block(self, block: _ReadOnlyBlock) -> None:
        self._blocks.append(block)

    def columns(self, mono: bool) -> int:
        """Characters per line of a page's text box (wrap estimate)."""
        width = self._notebook.winfo_width()
        if width <= 1:
            width = _PAGE_WIDTH + 40
        char = self._fonts[2 if mono else 1].measure("0") or 8
        return max(20, (width - _TEXT_INSET_PX) // char)

    def copy(self, text: str) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(text)

    # --- host of the Manual run tab (person_window_manual.ManualHost) -------

    def manual_block(
        self, parent: "ttk.Frame", *, title: str, max_lines: int
    ) -> ManualBlock:
        return _ReadOnlyBlock(self, parent, title=title, max_lines=max_lines)

    def manual_label(
        self, parent: "tk.Misc", text: str = "", foreground: str = ""
    ) -> "ttk.Label":
        return self._label(parent, text, foreground)

    def manual_entry(self, parent: "tk.Misc", *, row: int, height: int) -> "tk.Text":
        return self._entry_text(parent, row=row, height=height)

    # --- host of the shared chat (person_window_chat.ChatHost) -------------

    def chat_block(
        self, parent: "ttk.Frame", *, title: str, fixed_lines: int
    ) -> ChatBlock:
        return _ReadOnlyBlock(self, parent, title=title, fixed_lines=fixed_lines)

    def chat_label(
        self, parent: "tk.Misc", text: str = "", foreground: str = ""
    ) -> "ttk.Label":
        return self._label(parent, text, foreground)

    def chat_entry(self, parent: "tk.Misc", *, row: int, height: int) -> "tk.Text":
        return self._entry_text(parent, row=row, height=height)

    def manual_copy(self, text: str) -> None:
        self.copy(text)

    def manual_send(self, event: WindowEvent) -> None:
        self._writer.send(event)

    def manual_connected(self) -> bool:
        return self._view.connected

    def manual_open_url(self, url: str) -> None:
        self._open_url(url)

    def manual_scroll_area(self, parent: "tk.Misc") -> tuple["ttk.Frame", "ttk.Frame"]:
        page = _ScrollPage(parent)
        # The wheel scrolls it like a tab's page.
        self._pages.append(page)
        return page.outer, page.frame

    def manual_hidden(self) -> bool:
        return self._runner_hidden

    def manual_note_foreground(self, from_shortcut: bool) -> None:
        # A system-wide press while this window is already in front is like
        # a click: the focus stays here after Save (giving it "back" would
        # switch to whatever app was recorded at the last show).
        restore = from_shortcut and not self._window_in_front()
        self._restore_focus = restore
        self._previous_hwnd = 0
        if sys.platform == "win32":
            if restore:
                self._previous_hwnd = int(ctypes.windll.user32.GetForegroundWindow())

    def _window_in_front(self) -> bool:
        """This window (macOS: this process's app) is in front right now."""
        if sys.platform == "win32":
            try:
                frame = int(self._root.wm_frame(), 16)
            except (ValueError, tk.TclError):
                return False
            return int(ctypes.windll.user32.GetForegroundWindow()) == frame
        if sys.platform == "darwin":
            if not _HAVE_APPKIT:
                return False
            front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return front is not None and front.processIdentifier() == os.getpid()
        try:
            return self._root.focus_get() is not None
        except KeyError:
            # Focus in a widget tkinter has no name for (a combobox's list):
            # still this window's.
            return True

    def manual_focus_sheet(self) -> None:
        # The one exception to "never takes the focus": the tester's own key
        # press or click opened the sheet.
        if sys.platform == "win32":
            hwnd = int(self._root.wm_frame(), 16)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        elif sys.platform != "darwin":
            # macOS: showing after the screenshot already activated the app.
            self._root.focus_force()

    def manual_focus_back(self) -> None:
        if not self._restore_focus:
            return
        self._restore_focus = False
        if sys.platform == "win32":
            if self._previous_hwnd:
                ctypes.windll.user32.SetForegroundWindow(self._previous_hwnd)
            self._previous_hwnd = 0
        else:
            self._front_app.restore_now()

    def _label(
        self, parent: "tk.Misc", text: str = "", foreground: str = ""
    ) -> "ttk.Label":
        """A short label that wraps at the window width."""
        label = ttk.Label(
            parent, text=text, wraplength=_PAGE_WIDTH - 40, foreground=foreground
        )
        self._wrapped.append(label)
        return label

    def _entry_text(
        self,
        parent: "tk.Misc",
        *,
        row: int,
        height: int,
        mono: bool = False,
    ) -> "tk.Text":
        """An editable text box with its scrollbar, gridded at ``row``."""
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        widget = tk.Text(
            holder,
            height=height,
            width=1,
            wrap="char" if mono else "word",
            font="TkFixedFont" if mono else "TkTextFont",
            borderwidth=1,
            relief="solid",
            undo=True,
        )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        widget.grid(row=0, column=0, sticky="ew")
        scroll.grid(row=0, column=1, sticky="ns")
        holder.grid(row=row, column=0, sticky="ew", pady=(2, 0))
        return widget

    # --- layout -----------------------------------------------------------

    def _build(self) -> None:
        root = self._root
        root.title(_TITLE)
        root.minsize(_MIN_WIDTH, _MIN_HEIGHT)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        self._build_header()
        self._build_pause_panel()
        notebook = ttk.Notebook(root)
        notebook.grid(row=2, column=0, sticky="nsew", padx=6, pady=(4, 6))
        self._notebook = notebook
        self._step_page = self._add_page("Step")
        self._case_page = self._add_page("Test case")
        self._log_tab = ttk.Frame(notebook, padding=12)
        notebook.add(self._log_tab, text="Log")
        self._discussion_page = self._add_page("Discussion")
        self._manual_page = self._add_page("Manual run")
        self._build_step_tab(self._step_page.frame)
        self._build_case_tab(self._case_page.frame)
        self._build_log_tab(self._log_tab)
        self._build_discussion_tab(self._discussion_page.frame)
        self._manual = ManualView(self, self._root, self._manual_page.frame)
        self._wrap_header()
        for tab in (
            self._case_page.outer,
            self._log_tab,
            self._discussion_page.outer,
            self._manual_page.outer,
        ):
            notebook.hide(tab)
        notebook.bind("<<NotebookTabChanged>>", lambda _event: self._render())
        notebook.bind("<Configure>", self._on_notebook_configure)
        root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        root.bind_all("<Button-4>", self._on_wheel, add="+")
        root.bind_all("<Button-5>", self._on_wheel, add="+")

    def tab_frames(self) -> tuple["ttk.Frame", ...]:
        return (
            self._step_page.outer,
            self._case_page.outer,
            self._log_tab,
            self._discussion_page.outer,
            self._manual_page.outer,
        )

    def _add_page(self, text: str) -> _ScrollPage:
        page = _ScrollPage(self._notebook)
        self._notebook.add(page.outer, text=text)
        self._pages.append(page)
        return page

    def _build_header(self) -> None:
        header = ttk.Frame(self._root, padding=(12, 10, 12, 0))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        # Beside the buttons: wrapped narrower than the pages' labels.
        self._title = ttk.Label(header, font=_HEADING_FONT)
        self._title.grid(row=0, column=0, sticky="w")
        self._subtitle = ttk.Label(header)
        self._subtitle.grid(row=1, column=0, sticky="w")
        self._header_status = ttk.Label(header, foreground=_WARN_COLOR)
        self._header_status.grid(row=2, column=0, sticky="w")
        self._header_labels = (self._title, self._subtitle, self._header_status)
        buttons = ttk.Frame(header)
        buttons.grid(row=0, column=1, rowspan=3, sticky="ne", padx=(8, 0))
        self._header_buttons = buttons
        self._pause_button = ttk.Button(
            buttons, text="Pause…", command=self._toggle_pause_panel
        )
        self._pause_button.pack(side="left")
        self._smaller_button = ttk.Button(
            buttons, text="A−", width=3, command=lambda: self._change_text_size(-1)
        )
        self._smaller_button.pack(side="left", padx=(6, 0))
        self._larger_button = ttk.Button(
            buttons, text="A+", width=3, command=lambda: self._change_text_size(1)
        )
        self._larger_button.pack(side="left", padx=(2, 0))

    def _build_pause_panel(self) -> None:
        panel = ttk.Frame(self._root, padding=(12, 6, 12, 0))
        panel.columnconfigure(0, weight=1)
        panel.grid(row=1, column=0, sticky="ew")
        self._pause_title = self._label(panel, "")
        self._pause_title.grid(row=0, column=0, sticky="w")
        self._pause_label = self._label(
            panel,
            "What do you want to discuss? (optional; sent to the agent once "
            "the case has stopped)",
            foreground=_MUTED_COLOR,
        )
        self._pause_label.grid(row=1, column=0, sticky="w")
        self._pause_text = self._entry_text(panel, row=2, height=3)
        row = ttk.Frame(panel)
        row.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._pause_send = ttk.Button(row, text="Pause", command=self._pause)
        self._pause_send.pack(side="left")
        ttk.Button(row, text="Cancel", command=self._toggle_pause_panel).pack(
            side="left", padx=(6, 0)
        )
        self._pause_status = self._label(panel, "")
        self._pause_status.grid(row=4, column=0, sticky="w", pady=(4, 0))
        panel.grid_remove()
        self._pause_panel = panel

    def _build_step_tab(self, frame: "ttk.Frame") -> None:
        self._picker_frame = ttk.Frame(frame)
        self._picker_label = ttk.Label(self._picker_frame, text="")
        self._picker_label.pack(side="left")
        self._picker = ttk.Combobox(
            self._picker_frame, state="readonly", textvariable=self._picker_var
        )
        self._picker.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._picker.bind("<<ComboboxSelected>>", self._on_pick)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._picker.bind(sequence, self._on_picker_wheel)
        self._picker_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self._step_label = self._label(frame, "")
        self._step_label.grid(row=1, column=0, sticky="w")
        self._status = self._label(frame, "", foreground=_WARN_COLOR)
        self._status.grid(row=2, column=0, sticky="w")

        body = ttk.Frame(frame)
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._body = body

        self._instruction = _ReadOnlyBlock(self, body, title="What to do")
        self._instruction.frame.grid(row=0, column=0, sticky="ew", pady=(6, 0))
        # The test case (TestRail) steps this step covers.
        self._case_steps = _ReadOnlyBlock(
            self, body, title="", max_lines=_CASE_STEPS_MAX_LINES
        )
        self._case_steps.frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._case_steps.frame.grid_remove()
        self._how_to = _ReadOnlyBlock(self, body, title="How to do it")
        self._how_to.frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        # One Copy button per suggested command (rebuilt with the how-to).
        self._how_to_copies = ttk.Frame(self._how_to.frame)
        self._how_to_copies.grid(row=2, column=0, sticky="w", pady=(2, 0))
        self._expected = _ReadOnlyBlock(self, body, title="Expected result")
        self._expected.frame.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self._objection = _ReadOnlyBlock(
            self, body, title="The agent could not confirm your report"
        )
        self._objection.title.configure(foreground=_WARN_COLOR)
        self._objection.frame.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self._command = _ReadOnlyBlock(self, body, title="Command", mono=True)
        self._command.frame.grid(row=5, column=0, sticky="ew", pady=(6, 0))

        self._output_frame = ttk.Frame(body)
        self._output_frame.columnconfigure(0, weight=1)
        ttk.Label(self._output_frame, text="Command output (paste it here)").grid(
            row=0, column=0, sticky="w"
        )
        self._output = self._entry_text(self._output_frame, row=1, height=5, mono=True)
        self._output_frame.grid(row=6, column=0, sticky="ew", pady=(6, 0))

        self._note_frame = ttk.Frame(body)
        self._note_frame.columnconfigure(0, weight=1)
        self._note_label = ttk.Label(self._note_frame, text="Note for the agent")
        self._note_label.grid(row=0, column=0, sticky="w")
        self._note = self._entry_text(self._note_frame, row=1, height=3)
        self._note_frame.grid(row=7, column=0, sticky="ew", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, sticky="w", pady=(10, 0))
        self._buttons = buttons
        first = ttk.Frame(buttons)
        first.pack(anchor="w")
        self._same_button = ttk.Button(
            first, text="Same as expected", command=self._report_same
        )
        self._same_button.pack(side="left")
        self._different_button = ttk.Button(
            first, text="Different…", command=self._start_different
        )
        self._different_button.pack(side="left", padx=(6, 0))
        self._overrule_button = ttk.Button(
            first, text="Overrule", command=self._overrule
        )
        self._overrule_button.pack(side="left", padx=(6, 0))
        second = ttk.Frame(buttons)
        second.pack(anchor="w", pady=(6, 0))
        self._fail_button = ttk.Button(
            second, text="Mark failed", command=self._mark_failed
        )
        self._fail_button.pack(side="left")
        self._block_button = ttk.Button(
            second, text="Mark blocked", command=self._mark_blocked
        )
        self._block_button.pack(side="left", padx=(6, 0))

        # Shown in place of the buttons after Different… (note required).
        self._different_frame = ttk.Frame(body)
        self._different_report = ttk.Button(
            self._different_frame,
            text="Report different",
            command=self._report_different,
        )
        self._different_report.pack(side="left")
        ttk.Button(
            self._different_frame, text="Cancel", command=self._cancel_different
        ).pack(side="left", padx=(6, 0))
        self._different_frame.grid(row=8, column=0, sticky="w", pady=(10, 0))
        self._different_frame.grid_remove()

        # Shown in place of the buttons while Mark failed / blocked waits for
        # its confirmation (no separate dialog: see the module docstring).
        self._confirm_frame = ttk.Frame(body)
        self._confirm_frame.columnconfigure(0, weight=1)
        self._confirm_label = self._label(
            self._confirm_frame, "", foreground=_WARN_COLOR
        )
        self._confirm_label.grid(row=0, column=0, sticky="w")
        confirm_buttons = ttk.Frame(self._confirm_frame)
        confirm_buttons.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._confirm_button = ttk.Button(
            confirm_buttons, text="", command=self._confirm_decision
        )
        self._confirm_button.pack(side="left")
        ttk.Button(confirm_buttons, text="Cancel", command=self._cancel_confirm).pack(
            side="left", padx=(6, 0)
        )
        self._confirm_frame.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        self._confirm_frame.grid_remove()
        self._hint = self._label(body, "", foreground=_MUTED_COLOR)
        self._hint.grid(row=9, column=0, sticky="w", pady=(4, 0))

        self._build_ask(body, row=10)

        self._message = self._label(frame, "")
        self._message.grid(row=4, column=0, sticky="w", pady=(8, 0))

        self._url_frame = self._url_row(frame, "Case page", self._url_var)
        self._url_frame.grid(row=5, column=0, sticky="ew", pady=(8, 0))

    def _url_row(
        self, parent: "ttk.Frame", label: str, variable: "tk.StringVar"
    ) -> "ttk.Frame":
        row = ttk.Frame(parent)
        ttk.Label(row, text=label).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(
            row, text="Open", command=lambda: self._open_url(variable.get())
        ).pack(side="right")
        ttk.Button(row, text="Copy", command=lambda: self.copy(variable.get())).pack(
            side="right", padx=(0, 6)
        )
        return row

    def _build_ask(self, body: "ttk.Frame", *, row: int) -> None:
        """The collapsible "Ask the AI" section (only when questions work)."""
        ask = ttk.Frame(body)
        ask.columnconfigure(0, weight=1)
        ask.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        self._ask_frame = ask
        self._ask_toggle = ttk.Button(ask, text=_ASK_CLOSED, command=self._toggle_ask)
        self._ask_toggle.grid(row=0, column=0, sticky="w")
        content = ttk.Frame(ask)
        content.columnconfigure(0, weight=1)
        content.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._ask_content = content
        self._label(
            content,
            "The AI only advises: it does not act on this machine or answer "
            "the step for you.",
            foreground=_MUTED_COLOR,
        ).grid(row=0, column=0, sticky="w")
        self._qa = _ReadOnlyBlock(self, content, title="Questions and answers")
        self._qa.frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(content, text="Your question").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        self._question = self._entry_text(content, row=3, height=3)
        # <<Modified>> fires when the modified flag turns on; re-arm it each time.
        self._question.bind("<<Modified>>", self._on_question_modified)
        ask_row = ttk.Frame(content)
        ask_row.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self._ask_button = ttk.Button(ask_row, text="Ask", command=self._ask)
        self._ask_button.pack(side="left")
        self._ask_status = self._label(content, "")
        self._ask_status.grid(row=5, column=0, sticky="w", pady=(4, 0))
        content.grid_remove()
        ask.grid_remove()

    def _build_case_tab(self, frame: "ttk.Frame") -> None:
        self._case_empty = self._label(
            frame, "Punk Hazard sent no test case text for this case."
        )
        self._case_empty.grid(row=0, column=0, sticky="w")
        self._preconditions = _ReadOnlyBlock(self, frame, title="Preconditions")
        self._preconditions.frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._case_tab_steps = _ReadOnlyBlock(
            self, frame, title="Steps", max_lines=_CASE_TAB_STEPS_LINES
        )
        self._case_tab_steps.text.tag_configure(
            "covered",
            background=_COVERED_BACKGROUND,
            foreground=_COVERED_FOREGROUND,
        )
        self._case_tab_steps.frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._case_expected = _ReadOnlyBlock(self, frame, title="Expected results")
        self._case_expected.frame.grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def _build_log_tab(self, frame: "ttk.Frame") -> None:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._log = _ReadOnlyBlock(
            self, frame, title="", mono=True, fixed_lines=_LOG_LINES
        )
        ttk.Button(
            self._log.head, text="Open on Punk Hazard", command=self._open_case_page
        ).grid(row=0, column=1, sticky="e")
        self._log.frame.grid(row=0, column=0, sticky="nsew")

    def _build_discussion_tab(self, frame: "ttk.Frame") -> None:
        self._help_reason = _ReadOnlyBlock(self, frame, title="Why the case stopped")
        self._help_reason.frame.grid(row=0, column=0, sticky="ew")
        # Rows 1 (chat), 2 (working) and 3 (message box).
        self._chat = ChatPanel(
            self,
            frame,
            row=1,
            title="Discussion with the agent",
            lines=_CHAT_LINES,
            on_send=self._send_chat,
            on_input=self._on_chat_modified,
        )

        self._proposal = _ReadOnlyBlock(self, frame, title="")
        self._proposal.frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self._plan = _ReadOnlyBlock(
            self, frame, title="The agent's redesigned plan", max_lines=16
        )
        self._plan.frame.grid(row=5, column=0, sticky="ew", pady=(6, 0))

        resolve = ttk.Frame(frame)
        resolve.columnconfigure(0, weight=1)
        resolve.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(resolve, text="How should the case go on?", font=_BOLD_FONT).grid(
            row=0, column=0, sticky="w"
        )
        # Rebuilt only when the choices change (_render_modes).
        self._modes_frame = ttk.Frame(resolve)
        self._modes_frame.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._use_plan = ttk.Checkbutton(
            resolve,
            text="Use the redesigned plan",
            variable=self._use_plan_var,
            command=self._render,
        )
        self._use_plan.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._use_plan_reason = self._label(resolve, "", foreground=_MUTED_COLOR)
        self._use_plan_reason.grid(row=3, column=0, sticky="w")
        ttk.Label(resolve, text="Note (optional)").grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )
        self._help_note = self._entry_text(resolve, row=5, height=3)
        self._help_blockers = self._label(resolve, "", foreground=_WARN_COLOR)
        self._help_blockers.grid(row=6, column=0, sticky="w")
        self._help_buttons = ttk.Frame(resolve)
        self._help_buttons.grid(row=7, column=0, sticky="w", pady=(6, 0))
        self._continue_button = ttk.Button(
            self._help_buttons, text="Continue", command=self._help_continue
        )
        self._continue_button.pack(side="left")
        self._help_fail_button = ttk.Button(
            self._help_buttons,
            text="Mark failed",
            command=lambda: self._help_ask_confirm(PersonHelpActionKind.MARK_FAILED),
        )
        self._help_fail_button.pack(side="left", padx=(6, 0))
        self._help_block_button = ttk.Button(
            self._help_buttons,
            text="Mark blocked",
            command=lambda: self._help_ask_confirm(PersonHelpActionKind.MARK_BLOCKED),
        )
        self._help_block_button.pack(side="left", padx=(6, 0))
        self._help_confirm_frame = ttk.Frame(resolve)
        self._help_confirm_label = self._label(
            self._help_confirm_frame, "", foreground=_WARN_COLOR
        )
        self._help_confirm_label.pack(anchor="w")
        help_confirm_buttons = ttk.Frame(self._help_confirm_frame)
        help_confirm_buttons.pack(anchor="w", pady=(6, 0))
        self._help_confirm_button = ttk.Button(
            help_confirm_buttons, text="", command=self._help_confirm_decision
        )
        self._help_confirm_button.pack(side="left")
        ttk.Button(
            help_confirm_buttons, text="Cancel", command=self._help_cancel_confirm
        ).pack(side="left", padx=(6, 0))
        self._help_confirm_frame.grid(row=7, column=0, sticky="w", pady=(6, 0))
        self._help_confirm_frame.grid_remove()
        self._help_status = self._label(resolve, "")
        self._help_status.grid(row=8, column=0, sticky="w", pady=(4, 0))

        self._thread_url_frame = self._url_row(
            frame, "Open on Punk Hazard", self._thread_url_var
        )
        self._thread_url_frame.grid(row=7, column=0, sticky="ew", pady=(10, 0))

    # --- text size and resizing ---------------------------------------------

    def _apply_text_size(self) -> None:
        size = self._text_size
        for font in self._fonts:
            font.configure(size=size)
        self._heading_font.configure(size=size + 3)
        self._bold_font.configure(size=size)

    def _change_text_size(self, direction: int) -> None:
        size = clamp_text_size(self._text_size + direction * _TEXT_SIZE_STEP)
        if size == self._text_size:
            return
        self._text_size = size
        self._apply_text_size()
        self._update_size_buttons()
        self._wrap_header()
        self._schedule_refit()

    def _update_size_buttons(self) -> None:
        self._smaller_button.configure(
            state="normal" if self._text_size > TEXT_SIZE_MIN else "disabled"
        )
        self._larger_button.configure(
            state="normal" if self._text_size < TEXT_SIZE_MAX else "disabled"
        )

    def _on_notebook_configure(self, event: "tk.Event[ttk.Notebook]") -> None:
        if event.width == self._width:
            return
        self._width = event.width
        wrap = max(200, event.width - _TEXT_INSET_PX)
        for label in self._wrapped:
            label.configure(wraplength=wrap)
        self._wrap_header()
        self._schedule_refit()

    def _wrap_header(self) -> None:
        width = self._notebook.winfo_width()
        if width <= 1:
            width = _PAGE_WIDTH + 40
        wrap = max(160, width - self._header_buttons.winfo_reqwidth() - 30)
        for label in self._header_labels:
            label.configure(wraplength=wrap)

    def _schedule_refit(self) -> None:
        if self._refit_job is not None:
            self._root.after_cancel(self._refit_job)
        self._refit_job = self._root.after(_REFIT_DELAY_MS, self._refit)

    def _refit(self) -> None:
        self._refit_job = None
        for block in self._blocks:
            block.fit()

    # --- scrolling ------------------------------------------------------------

    def _page_of(self, widget: "tk.Misc") -> _ScrollPage | None:
        path = str(widget)
        for page in self._pages:
            if path.startswith(str(page.canvas)):
                return page
        return None

    def _on_wheel(self, event: "tk.Event[tk.Misc]") -> None:
        widget = event.widget
        # The picker's open list is Tk's own widget: tkinter hands its path.
        if ".popdown" in str(widget):
            return
        page = self._page_of(widget)
        if page is None:
            return
        # A text box's own scrollbar scrolls that text box, and a text box
        # that scrolls itself keeps the wheel (its class binding handles it).
        if isinstance(widget, ttk.Scrollbar) and widget is not page.scrollbar:
            return
        if isinstance(widget, tk.Text) and widget.yview() != (0.0, 1.0):
            return
        self._scroll_page(page, event)

    def _on_picker_wheel(self, event: "tk.Event[ttk.Combobox]") -> str:
        # The combobox's own wheel binding would switch the selected step.
        self._scroll_page(self._step_page, event)
        return "break"

    def _scroll_page(
        self,
        page: _ScrollPage,
        event: "tk.Event[tk.Misc] | tk.Event[ttk.Combobox]",
    ) -> None:
        # Shift+wheel is a horizontal swipe (macOS trackpads): no vertical scroll.
        # (state is a string only for events that are not key or button ones.)
        if isinstance(event.state, int) and event.state & _SHIFT_MASK:
            return
        if event.num == 4:
            units = -1
        elif event.num == 5:
            units = 1
        elif sys.platform == "darwin":
            units = -event.delta
        else:
            # Windows: 120 per notch; precision touchpads send smaller deltas.
            self._wheel_rest += event.delta
            notches = int(self._wheel_rest / _WHEEL_DELTA)
            self._wheel_rest -= notches * _WHEEL_DELTA
            units = -notches * _WHEEL_NOTCH_STEPS
        if units:
            page.canvas.yview_scroll(units, "units")

    # --- commands from the runner ------------------------------------------

    def poll(self) -> None:
        try:
            while True:
                command = self._inbox.get_nowait()
                if command is None or isinstance(command, QuitCommand):
                    self._root.destroy()
                    return
                self.apply(command)
        except queue.Empty:
            pass
        if self._writer.broken:
            self._root.destroy()
            return
        for kind in self._hotkeys.drain():
            self._manual.on_shortcut(kind, system_wide=True)
        self.sync_hotkeys()
        self._root.after(_POLL_MS, self.poll)

    def sync_hotkeys(self) -> None:
        """Register the shortcuts while the Manual run tab wants them, and
        show the backend's status in the tab when it changes."""
        wanted = self._manual.shortcuts_wanted()
        if wanted != self._hotkeys_wanted:
            self._hotkeys_wanted = wanted
            self._hotkeys.set_wanted(wanted)
        status = self._hotkeys.status
        if status != self._hotkey_status:
            self._hotkey_status = status
            self._manual.set_shortcut_status(status)

    def apply(self, command: WindowCommand) -> None:
        match command:
            case StateCommand(
                steps=steps, questions_enabled=questions_enabled, cases=cases
            ):
                self._view.questions_enabled = questions_enabled
                self._set_state(steps, cases)
            case ShowCommand():
                self._show()
            case HideCommand(seq=seq):
                self._hide(lambda: self._writer.send(HiddenEvent(seq=seq)))
            case ResultCommand(ok=ok, message=message):
                self._view.busy = None
                self._set_message(message, _OK_COLOR if ok else _ERROR_COLOR)
                self._render()
            case QuestionResultCommand(ok=ok, message=message, manual=True):
                self._manual.on_question_result(ok, message)
            case QuestionResultCommand(ok=ok, message=message):
                self._on_question_result(ok, message)
            case CaseResultCommand(kind=kind, ok=ok, message=message):
                self._on_case_result(kind, ok, message)
            case ConnectionCommand(connected=connected):
                self._view.connected = connected
                self._render()
            case QuitCommand():
                pass
            case ManualStateCommand(state=state):
                self._set_manual_state(state)
            case ManualCaptureResultCommand(
                seq=seq, ok=ok, message=message, key=key, thumbnail_b64=thumbnail
            ):
                self._manual.on_capture_result(seq, ok, message, key, thumbnail)
            case ManualThumbnailCommand(key=key, thumbnail_b64=thumbnail):
                self._manual.on_thumbnail(key, thumbnail)
            case ManualResultCommand(kind=manual_kind, ok=ok, message=message):
                self._manual.on_result(manual_kind, ok, message)

    def _set_state(
        self,
        steps: tuple[PersonStepStatePayload, ...],
        cases: tuple[PersonCaseStatePayload, ...],
    ) -> None:
        view = self._view
        view.steps = steps
        view.cases = cases
        ids = [step.person_step_id for step in steps]
        if view.selected_id not in ids:
            view.selected_id = ids[0] if ids else None
        keys = {_key(step) for step in steps}
        if view.busy is not None and view.busy not in keys:
            view.busy = None
        if view.asking is not None and view.asking not in keys:
            view.asking = None
        view.drafts = {k: d for k, d in view.drafts.items() if k in keys}
        help_ids = frozenset(
            help_state.help_request_id for _case, help_state in self._help_cases()
        )
        view.help_drafts = {k: d for k, d in view.help_drafts.items() if k in help_ids}
        if view.help_id is not None and view.help_id not in help_ids:
            view.help_busy = False
            view.chat_busy = False
        self._pick_tab(frozenset(keys), help_ids)
        if not steps and not help_ids and not self._manual.has_run:
            self._hide()
        self._render()

    def _set_manual_state(self, state: ManualWindowState) -> None:
        previous = self._manual.run_id
        self._manual.apply_state(state)
        has_run = self._manual.has_run
        self._set_tab_visible(self._manual_page.outer, has_run)
        if has_run and self._manual.run_id != previous and not self._view.steps:
            # A new manual run: that is where the tester works now.
            self._select_tab(self._manual_page.outer)
        if not has_run and not self._view.steps and not self._help_cases():
            self._hide()
        self._render()

    def _pick_tab(self, keys: frozenset[_StepKey], help_ids: frozenset[int]) -> None:
        """Bring the person to what is new: a step round, else a help request."""
        view = self._view
        new_steps = keys - view.seen_steps
        new_help = help_ids - view.seen_help
        view.seen_steps = keys
        view.seen_help = help_ids
        if new_steps:
            self._select_tab(self._step_page.outer)
        elif new_help and not keys:
            self._set_tab_visible(self._discussion_page.outer, True)
            self._select_tab(self._discussion_page.outer)

    def _show(self) -> None:
        view = self._view
        manual_run = self._manual.run_id
        if (
            not view.steps
            and not view.cases
            and manual_run is None
            and not self._message.cget("text")
        ):
            return
        root = self._root
        current: set[_RaiseKey] = {("step", *_key(step)) for step in view.steps}
        current |= {
            ("help", help_state.help_request_id, 0)
            for _case, help_state in self._help_cases()
        }
        if manual_run is not None:
            current.add(("manual", manual_run, 0))
        fresh = current - view.raised
        self._front_app.before_show()
        # Never topmost, never focus_force (see the module docstring).
        if sys.platform == "win32":
            root.wm_state("normal")
            hwnd = int(root.wm_frame(), 16)
            ctypes.windll.user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
            if fresh:
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    _HWND_TOP,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
                )
        else:
            root.deiconify()
            if fresh:
                root.lift()
        self._runner_hidden = False
        # Once per new step round or help request.
        view.raised |= fresh

    def _hide(self, done: Callable[[], None] | None = None) -> None:
        """Hide; ``done`` runs once the previous front app is back (macOS)."""
        # A pending confirmation never survives a hide: the person sees the
        # plain buttons again when the window comes back. Typed drafts stay.
        view = self._view
        if view.confirm is not None or view.help_confirm is not None:
            view.confirm = None
            view.help_confirm = None
            self._render()
        # An open picker list is its own Tk window, not hidden by withdraw().
        try:
            self._picker.tk.call("ttk::combobox::Unpost", self._picker)
        except tk.TclError:
            pass
        self._root.withdraw()
        self._runner_hidden = True
        # Unmap now, not at the next idle, so the hidden ack is truthful.
        self._root.update_idletasks()
        if not self._front_app.start_restore():
            if done is not None:
                done()
            return
        self._wait_restored(done)

    def _wait_restored(self, done: Callable[[], None] | None) -> None:
        # The front app changes through the run loop: poll from Tk callbacks.
        if not self._front_app.restored():
            self._root.after(_RESTORE_POLL_MS, self._wait_restored, done)
        elif done is not None:
            done()

    # --- what is shown ------------------------------------------------------

    def _selected(self) -> PersonStepStatePayload | None:
        for step in self._view.steps:
            if step.person_step_id == self._view.selected_id:
                return step
        return None

    def _help_cases(
        self,
    ) -> tuple[tuple[PersonCaseStatePayload, PersonHelpPayload], ...]:
        return tuple(
            (case, help_state)
            for case in self._view.cases
            if case.role is PersonCaseRole.HELP
            and (help_state := case.help) is not None
        )

    def _help_case(
        self,
    ) -> tuple[PersonCaseStatePayload, PersonHelpPayload] | None:
        cases = self._help_cases()
        return cases[0] if cases else None

    def _step_case(self, step: PersonStepStatePayload) -> PersonCaseStatePayload | None:
        """The step's case: by pivot_id; for an older server (0) by title."""
        cases = self._view.cases
        if step.pivot_id:
            return next((c for c in cases if c.pivot_id == step.pivot_id), None)
        matches = [
            c
            for c in cases
            if c.role is PersonCaseRole.STEP
            and c.case_title == step.case_title
            and c.run_name == step.run_name
        ]
        return matches[0] if len(matches) == 1 else None

    def _on_discussion_tab(self) -> bool:
        try:
            return bool(self._notebook.select() == str(self._discussion_page.outer))
        except tk.TclError:
            return False

    def _header_case(self) -> PersonCaseStatePayload | None:
        help_case = self._help_case()
        if help_case is not None and self._on_discussion_tab():
            return help_case[0]
        step = self._selected()
        if step is not None:
            return self._step_case(step)
        if help_case is not None:
            return help_case[0]
        return next(
            (c for c in self._view.cases if c.role is PersonCaseRole.RUNNING), None
        )

    def _set_tab_visible(self, tab: "ttk.Frame", visible: bool) -> None:
        hidden = self._notebook.tab(tab, "state") == "hidden"
        if visible and hidden:
            self._notebook.add(tab)
        elif not visible and not hidden:
            if self._notebook.select() == str(tab):
                self._notebook.select(self._step_page.outer)
            self._notebook.hide(tab)

    def _select_tab(self, tab: "ttk.Frame") -> None:
        if self._notebook.select() != str(tab):
            self._notebook.select(tab)

    # --- rendering ----------------------------------------------------------

    def _render(self) -> None:
        view = self._view
        step = self._selected()
        help_case = self._help_case()
        self._set_tab_visible(self._discussion_page.outer, help_case is not None)
        case = self._header_case()
        has_case = case is not None
        self._set_tab_visible(self._case_page.outer, has_case)
        self._set_tab_visible(self._log_tab, has_case)
        self._render_header(step, case)
        self._render_step(step)
        covered = (
            frozenset(s.num for s in step.case_steps)
            if step is not None and case is not None and self._step_case(step) is case
            else frozenset()
        )
        cover_label = step.case_steps_label if step is not None and covered else ""
        if case is not None:
            self._render_case(case, covered, cover_label)
            self._render_log(case)
        if help_case is not None:
            self._render_discussion(*help_case)
        else:
            view.help_id = None
        self._manual.render()
        self._update_size_buttons()

    def _render_header(
        self,
        step: PersonStepStatePayload | None,
        case: PersonCaseStatePayload | None,
    ) -> None:
        view = self._view
        if case is not None:
            self._title.configure(text=case.case_title)
            sub = " · ".join(x for x in (case.run_name, case.status_label) if x)
        elif step is not None:
            self._title.configure(text=step.case_title)
            sub = step.run_name
        elif self._manual.has_run:
            title, sub = self._manual.header()
            self._title.configure(text=title)
        else:
            self._title.configure(text="Nothing is waiting for you right now.")
            sub = ""
        self._subtitle.configure(text=sub)
        lines = [self._connection_text()]
        if case is None or case.pause is None:
            self._pause_button.pack_forget()
            view.pause_open = False
        else:
            pause = case.pause
            self._pause_button.pack(side="left", before=self._smaller_button)
            if pause.pause_pending:
                self._pause_button.configure(text="Pausing…", state="disabled")
                if pause.pause_requested_by:
                    lines.append(f"Pause requested by {pause.pause_requested_by}.")
            else:
                can = pause.can_pause and view.connected and not view.pause_busy
                self._pause_button.configure(
                    text="Pause…", state="normal" if can else "disabled"
                )
                if not pause.can_pause and pause.pause_blocker:
                    lines.append(pause.pause_blocker)
            if view.pause_note and view.pause_note_pivot == case.pivot_id:
                lines.append(view.pause_note)
            if pause.pause_message_state == "refused":
                note = pause.pause_message_note or "type it in the Discussion tab."
                lines.append(f"Your message was not sent: {note}")
            if not pause.can_pause and not view.pause_busy:
                view.pause_open = False
        self._header_status.configure(text="\n".join(x for x in lines if x))
        self._render_pause_panel(case)

    def _render_pause_panel(self, case: PersonCaseStatePayload | None) -> None:
        view = self._view
        if not view.pause_open or case is None:
            self._pause_panel.grid_remove()
            return
        self._pause_title.configure(text=f"Pause “{case.case_title}”")
        self._pause_send.configure(
            text="Pausing…" if view.pause_busy else "Pause",
            state="normal" if view.connected and not view.pause_busy else "disabled",
        )
        self._pause_panel.grid()

    def _render_step(self, step: PersonStepStatePayload | None) -> None:
        view = self._view
        key = _key(step) if step is not None else None
        if key != view.rendered:
            view.confirm = None
            self._save_draft()
            if key is not None:
                # A message about another step does not belong to this one.
                self._set_message("", _OK_COLOR)
                self._set_ask_status("", _OK_COLOR)
            view.rendered = key
            self._load_draft()
            # A new step or round starts at the top.
            self._step_page.canvas.yview_moveto(0.0)
        self._render_picker(step)
        if step is None:
            self._step_label.configure(
                text=(
                    "No step waits for you."
                    if view.cases
                    else "Nothing is waiting for you right now."
                )
            )
            self._body.grid_remove()
            self._url_frame.grid_remove()
            self._status.configure(text=self._connection_text())
            return
        self._body.grid()
        kind = " · restore step" if step.kind is PersonStepWireKind.RESTORE else ""
        self._step_label.configure(
            text=f"{step.step_label} · attempt {step.round}{kind}"
        )
        status_lines = [self._connection_text()] + list(step.blockers)
        self._status.configure(text="\n".join(line for line in status_lines if line))
        self._instruction.set(step.instruction)
        self._render_case_steps(step)
        self._render_how_to(step)
        self._expected.set(step.expected_result or "(none given)")
        not_confirmed = step.status is PersonStepWireStatus.NOT_CONFIRMED
        if not_confirmed and step.objection:
            self._objection.set(step.objection)
            self._objection.show(True)
        else:
            self._objection.show(False)
        if step.command:
            shell = f" (run it in {step.shell_label})" if step.shell_label else ""
            self._command.title.configure(text=f"Command{shell}")
            self._command.set(step.command)
            self._command.show(True)
        else:
            self._command.show(False)
        if _wants_output(step):
            self._output_frame.grid()
        else:
            self._output_frame.grid_remove()
        if step.case_url:
            self._url_var.set(step.case_url)
            self._url_frame.grid()
        else:
            self._url_frame.grid_remove()
        self._render_buttons(step, not_confirmed)
        self._render_ask(step)

    def _render_case_steps(self, step: PersonStepStatePayload) -> None:
        view = self._view
        shown: _CaseStepsView = (step.case_steps_label, step.case_steps)
        if shown == view.rendered_case_steps:
            return
        view.rendered_case_steps = shown
        block = self._case_steps
        if not step.case_steps_label and not step.case_steps:
            block.show(False)
            return
        label = step.case_steps_label or "test case steps"
        block.title.configure(text=f"Covers {label}")
        segments: list[tuple[str, tuple[str, ...]]] = []
        for index, case_step in enumerate(step.case_steps):
            prefix = "" if index == 0 else "\n"
            segments.append((f"{prefix}{case_step.num}. {case_step.text}", ()))
            if case_step.expected:
                segments.append((f"\nExpected: {case_step.expected}", ("muted",)))
        block.set_segments(tuple(segments))
        if segments:
            block.holder.grid()
        else:
            # A label alone (the server had no step texts): the heading only.
            block.holder.grid_remove()
        block.show(True)

    def _render_how_to(self, step: PersonStepStatePayload) -> None:
        view = self._view
        if step.how_to == view.rendered_how_to:
            return
        view.rendered_how_to = step.how_to
        shell = f" · commands for {step.shell_label}" if step.shell_label else ""
        render_how_to(
            self._how_to,
            self._how_to_copies,
            step.how_to,
            title=f"How to do it{shell}",
            copy=self.copy,
        )

    def _render_ask(self, step: PersonStepStatePayload) -> None:
        view = self._view
        if not view.questions_enabled:
            self._ask_frame.grid_remove()
            return
        self._ask_frame.grid()
        count = f" ({len(step.questions)})" if step.questions else ""
        self._ask_toggle.configure(
            text=(_ASK_OPEN if view.ask_open else _ASK_CLOSED) + count
        )
        if not view.ask_open:
            self._ask_content.grid_remove()
            return
        self._ask_content.grid()
        qa = _qa_text(step.questions) or "No questions yet for this step."
        if qa != view.rendered_qa:
            # Only on change, so a snapshot that changes nothing here keeps
            # the person's scroll position.
            view.rendered_qa = qa
            self._qa.set(qa)
            self._qa.text.see("end")
        self._update_ask_button(step)

    def _update_ask_button(self, step: PersonStepStatePayload) -> None:
        view = self._view
        can_ask = (
            view.connected
            and view.asking is None
            and not _has_pending_question(step)
            and bool(_editable_text(self._question).strip())
        )
        self._ask_button.configure(state="normal" if can_ask else "disabled")
        if view.asking == _key(step):
            self._set_ask_status("Sending your question…", _MUTED_COLOR)

    def _set_ask_status(self, text: str, color: str) -> None:
        self._ask_status.configure(text=text, foreground=color)

    def _connection_text(self) -> str:
        return "" if self._view.connected else "Reconnecting to Punk Hazard…"

    def _render_picker(self, step: PersonStepStatePayload | None) -> None:
        steps = self._view.steps
        if len(steps) < 2:
            self._picker_frame.grid_remove()
            return
        labels = [self._pick_label(s) for s in steps]
        self._picker_label.configure(text=f"{len(steps)} steps waiting:")
        self._picker.configure(values=labels)
        if step is not None:
            self._picker_var.set(self._pick_label(step))
        self._picker_frame.grid()

    @staticmethod
    def _pick_label(step: PersonStepStatePayload) -> str:
        label = f"{step.run_name} · {step.case_title} · {step.step_label}"
        if step.case_steps_label:
            label += f" · {step.case_steps_label}"
        return label

    def _render_buttons(
        self, step: PersonStepStatePayload, not_confirmed: bool
    ) -> None:
        view = self._view
        idle = view.connected and view.busy is None
        can_report = idle and not step.blockers
        for button in (self._same_button, self._different_button):
            button.configure(state="normal" if can_report else "disabled")
        self._different_report.configure(state="normal" if can_report else "disabled")
        if not_confirmed:
            self._overrule_button.pack(
                side="left", padx=(6, 0), after=self._different_button
            )
            self._overrule_button.configure(state="normal" if idle else "disabled")
        else:
            self._overrule_button.pack_forget()
        for button in (self._fail_button, self._block_button):
            button.configure(state="normal" if idle else "disabled")
        # The note: required after Different…, optional with the other
        # decisions that carry it (re-report, overrule, mark).
        if view.different:
            self._note_label.configure(text="What was different? (required)")
        else:
            self._note_label.configure(text="Note for the agent (optional)")
        if view.different or view.confirm is not None or not_confirmed:
            self._note_frame.grid()
        else:
            self._note_frame.grid_remove()
        self._different_frame.grid_remove()
        self._confirm_frame.grid_remove()
        self._buttons.grid_remove()
        if view.confirm is not None:
            failed = view.confirm is PersonStepActionKind.FAIL
            self._confirm_label.configure(
                text=(
                    "Mark this case failed? The rest of the run then continues."
                    if failed
                    else "Mark this case blocked? It can be resumed later; the "
                    "rest of the run then continues."
                )
            )
            self._confirm_button.configure(
                text="Confirm mark failed" if failed else "Confirm mark blocked",
                state="normal" if idle else "disabled",
            )
            self._confirm_frame.grid()
            self._hint.configure(text="")
            return
        if view.different:
            self._different_frame.grid()
        else:
            self._buttons.grid()
        if view.busy is not None:
            hint = "Sending your answer…"
        elif view.different:
            hint = "Describe what you saw instead, then press Report different."
        elif not_confirmed:
            hint = (
                "Same as expected or Different… sends a new report for the "
                "agent to check. Overrule records the step as passed without "
                "another check."
            )
        else:
            hint = "Your report hands the step to the agent to confirm."
        self._hint.configure(text=hint)

    def _set_message(self, text: str, color: str) -> None:
        self._message.configure(text=text, foreground=color)

    def _save_draft(self) -> None:
        if self._view.rendered is None:
            return
        self._view.drafts[self._view.rendered] = _Draft(
            different=self._view.different,
            note=_editable_text(self._note),
            output=_editable_text(self._output),
            question=_editable_text(self._question),
        )

    def _load_draft(self) -> None:
        rendered = self._view.rendered
        draft = self._view.drafts.get(rendered, _Draft()) if rendered else _Draft()
        self._view.different = draft.different
        _set_editable_text(self._note, draft.note)
        _set_editable_text(self._output, draft.output)
        _set_editable_text(self._question, draft.question)
        # Another step's Q&A: rewrite the box on the next render.
        self._view.rendered_qa = None

    # --- test case and log tabs -------------------------------------------------

    def _render_case(
        self,
        case: PersonCaseStatePayload,
        covered: frozenset[int],
        cover_label: str,
    ) -> None:
        view = self._view
        signature: tuple[object, ...] = (
            case.pivot_id,
            case.preconditions,
            case.steps_text,
            case.expected_text,
            case.numbered_steps,
            covered,
            cover_label,
        )
        if signature == view.rendered_case:
            return
        view.rendered_case = signature
        self._preconditions.set(case.preconditions)
        self._preconditions.show(bool(case.preconditions))
        steps = self._case_tab_steps
        if case.numbered_steps:
            segments: list[tuple[str, tuple[str, ...]]] = []
            for index, case_step in enumerate(case.numbered_steps):
                tags: tuple[str, ...] = ("covered",) if case_step.num in covered else ()
                # Newlines stay untagged: only the text itself is tinted.
                if index:
                    segments.append(("\n", ()))
                segments.append((f"{case_step.num}. {case_step.text}", tags))
                if case_step.expected:
                    segments.append(("\n", ()))
                    segments.append(
                        (f"    Expected: {case_step.expected}", tags + ("muted",))
                    )
            steps.set_segments(tuple(segments))
        else:
            steps.set(case.steps_text)
        title = "Steps"
        if covered:
            title += f" · this step covers {cover_label or 'the highlighted ones'}"
        steps.title.configure(text=title)
        steps.show(bool(case.numbered_steps or case.steps_text))
        if covered:
            ranges = steps.text.tag_ranges("covered")
            if ranges:
                steps.text.see(ranges[0])
        self._case_expected.set(case.expected_text)
        self._case_expected.show(bool(case.expected_text))
        empty = not (case.preconditions or case.steps_text or case.numbered_steps)
        if empty and not case.expected_text:
            self._case_empty.grid()
        else:
            self._case_empty.grid_remove()

    def _render_log(self, case: PersonCaseStatePayload) -> None:
        view = self._view
        shown = (case.pivot_id, hash(case.log_tail))
        if shown == view.rendered_log:
            return
        view.rendered_log = shown
        lines = case.log_tail.count("\n") + 1 if case.log_tail else 0
        self._log.title.configure(
            text=f"Showing the last {lines} lines" if lines else "No log lines yet."
        )
        # One insert of at most PERSON_CASE_LOG_TAIL_MAX_CHARS characters.
        self._log.set(case.log_tail)
        self._log.text.see("end")

    def _open_case_page(self) -> None:
        case = self._header_case()
        if case is not None:
            self._open_url(case.case_url)

    def _open_url(self, url: str) -> None:
        if _web_url(url):
            webbrowser.open(url)

    # --- discussion tab -------------------------------------------------------

    def _render_discussion(
        self, case: PersonCaseStatePayload, help_state: PersonHelpPayload
    ) -> None:
        view = self._view
        if help_state.help_request_id != view.help_id:
            self._save_help_draft()
            view.help_id = help_state.help_request_id
            view.help_confirm = None
            view.rendered_help = None
            view.rendered_modes = None
            self._load_help_draft(help_state)
        self._render_chat(help_state)
        signature: tuple[object, ...] = (
            help_state.help_request_id,
            _help_reason_text(help_state),
            help_state.proposal_label,
            help_state.proposal_note,
            help_state.plan_draft,
            help_state.thread_url,
        )
        if signature != view.rendered_help:
            view.rendered_help = signature
            self._help_reason.set(_help_reason_text(help_state))
            if help_state.proposal_label or help_state.proposal_note:
                label = help_state.proposal_label or "a resolution"
                self._proposal.title.configure(text=f"The agent suggests: {label}")
                self._proposal.set(help_state.proposal_note or "(no note)")
                self._proposal.show(True)
            else:
                self._proposal.show(False)
            draft = help_state.plan_draft
            if draft is not None:
                self._plan.set_segments(_plan_draft_segments(draft))
                self._plan.show(True)
            else:
                self._plan.show(False)
            url = help_state.thread_url or case.case_url
            self._thread_url_var.set(url)
            if url:
                self._thread_url_frame.grid()
            else:
                self._thread_url_frame.grid_remove()
        self._chat.set_working(help_state.turn_live)
        self._render_send(help_state)
        self._render_modes(help_state)
        self._render_resolve(help_state)

    def _render_chat(self, help_state: PersonHelpPayload) -> None:
        self._chat.render_rows(help_state.help_request_id, help_state.messages)

    def _render_send(self, help_state: PersonHelpPayload) -> None:
        self._chat.render_send(
            connected=self._view.connected,
            can_send=help_state.can_send,
            busy=self._view.chat_busy,
            send_blocker=help_state.send_blocker,
        )

    def _render_modes(self, help_state: PersonHelpPayload) -> None:
        view = self._view
        signature: tuple[object, ...] = (
            help_state.help_request_id,
            help_state.continue_mode_choices,
            help_state.agent_continue_mode,
        )
        if signature != view.rendered_modes:
            view.rendered_modes = signature
            for child in self._modes_frame.winfo_children():
                child.destroy()
            for mode in help_state.continue_mode_choices:
                text = _MODE_LABELS[mode]
                if mode is help_state.agent_continue_mode:
                    text += "  (the agent suggests this)"
                ttk.Radiobutton(
                    self._modes_frame,
                    text=text,
                    value=mode.value,
                    variable=self._mode_var,
                ).pack(anchor="w")
        values = {mode.value for mode in help_state.continue_mode_choices}
        if self._mode_var.get() not in values:
            stored = help_state.continue_mode.value
            first = help_state.continue_mode_choices[:1]
            self._mode_var.set(
                stored if stored in values else (first[0].value if first else "")
            )
        restore = help_state.continue_mode is ContinueModeWire.RESTORE
        has_draft = help_state.plan_draft is not None
        if not has_draft or restore:
            self._use_plan_var.set(False)
        if has_draft:
            self._use_plan.grid()
            self._use_plan.configure(state="disabled" if restore else "normal")
            self._use_plan_reason.configure(
                text=_REDESIGN_ON_RESTORE if restore else ""
            )
        else:
            self._use_plan.grid_remove()
            self._use_plan_reason.configure(text="")
        forced = self._use_plan_var.get()
        if forced:
            # A redesign continues on the current screen.
            self._mode_var.set(ContinueModeWire.CURRENT_SCREEN.value)
        for radio in self._modes_frame.winfo_children():
            if isinstance(radio, ttk.Radiobutton):
                radio.configure(state="disabled" if forced else "normal")

    def _render_resolve(self, help_state: PersonHelpPayload) -> None:
        view = self._view
        idle = view.connected and not view.help_busy
        self._continue_button.configure(
            state="normal" if idle and help_state.can_continue else "disabled"
        )
        for button in (self._help_fail_button, self._help_block_button):
            button.configure(state="normal" if idle else "disabled")
        self._help_blockers.configure(text="\n".join(help_state.blockers))
        if view.help_confirm is not None:
            failed = view.help_confirm is PersonHelpActionKind.MARK_FAILED
            self._help_confirm_label.configure(
                text=(
                    "Mark this case failed? The rest of the run then continues."
                    if failed
                    else "Mark this case blocked? It can be resumed later; the "
                    "rest of the run then continues."
                )
            )
            self._help_confirm_button.configure(
                text="Confirm mark failed" if failed else "Confirm mark blocked",
                state="normal" if idle else "disabled",
            )
            self._help_buttons.grid_remove()
            self._help_confirm_frame.grid()
        else:
            self._help_confirm_frame.grid_remove()
            self._help_buttons.grid()
        if view.help_busy:
            self._help_status.configure(
                text="Sending your decision…", foreground=_MUTED_COLOR
            )

    def _save_help_draft(self) -> None:
        view = self._view
        if view.help_id is None:
            return
        view.help_drafts[view.help_id] = _HelpDraft(
            chat=self._chat.input_text(),
            note=_editable_text(self._help_note),
            mode=self._mode_var.get(),
            use_plan=self._use_plan_var.get(),
        )

    def _load_help_draft(self, help_state: PersonHelpPayload) -> None:
        draft = self._view.help_drafts.get(help_state.help_request_id, _HelpDraft())
        self._chat.set_input(draft.chat)
        _set_editable_text(self._help_note, draft.note)
        self._mode_var.set(draft.mode or help_state.continue_mode.value)
        self._use_plan_var.set(draft.use_plan)
        self._chat.set_status("")
        self._help_status.configure(text="")

    # --- person actions: step ------------------------------------------------

    def _on_pick(self, _event: "tk.Event[ttk.Combobox]") -> None:
        index = self._picker.current()
        if 0 <= index < len(self._view.steps):
            self._view.selected_id = self._view.steps[index].person_step_id
            self._render()

    def _on_user_close(self) -> None:
        if self._manual.has_run:
            # The tester works only in this window during a manual run.
            self._root.iconify()
            return
        self._hide(lambda: self._writer.send(ClosedByUserEvent()))

    def _note_text(self) -> str | None:
        note = _editable_text(self._note).strip()
        if len(note) > PERSON_STEP_NOTE_MAX_CHARS:
            self._set_message(
                f"The note is too long ({len(note)} of "
                f"{PERSON_STEP_NOTE_MAX_CHARS} characters).",
                _ERROR_COLOR,
            )
            return None
        return note

    def _output_text(self, step: PersonStepStatePayload) -> str | None:
        if not _wants_output(step):
            return ""
        output = _editable_text(self._output)
        if len(output) > PERSON_STEP_OUTPUT_MAX_CHARS:
            self._set_message(
                f"The output is too long ({len(output)} of "
                f"{PERSON_STEP_OUTPUT_MAX_CHARS} characters). Paste the "
                "relevant part.",
                _ERROR_COLOR,
            )
            return None
        return output

    def _report_same(self) -> None:
        step = self._selected()
        if step is None:
            return
        # The note goes along only where it is shown (a re-report).
        shown = step.status is PersonStepWireStatus.NOT_CONFIRMED
        note = self._note_text() if shown else ""
        output = self._output_text(step)
        if note is None or output is None:
            return
        self._send(
            step,
            PersonStepActionKind.REPORT,
            PersonStepWireOutcome.AS_EXPECTED,
            note,
            output,
        )

    def _start_different(self) -> None:
        self._view.different = True
        self._view.confirm = None
        self._render()
        self._note.focus_set()

    def _cancel_different(self) -> None:
        self._view.different = False
        self._render()

    def _report_different(self) -> None:
        step = self._selected()
        note = self._note_text()
        if step is None or note is None:
            return
        if not note:
            self._set_message("Describe in the note what was different.", _ERROR_COLOR)
            return
        output = self._output_text(step)
        if output is None:
            return
        self._send(
            step,
            PersonStepActionKind.REPORT,
            PersonStepWireOutcome.DIFFERENT,
            note,
            output,
        )

    def _overrule(self) -> None:
        step = self._selected()
        note = self._note_text()
        if step is None or note is None:
            return
        if step.status is not PersonStepWireStatus.NOT_CONFIRMED:
            return
        self._send(step, PersonStepActionKind.OVERRULE, None, note, "")

    def _mark_failed(self) -> None:
        self._ask_confirm(PersonStepActionKind.FAIL)

    def _mark_blocked(self) -> None:
        self._ask_confirm(PersonStepActionKind.BLOCK)

    def _ask_confirm(self, kind: PersonStepActionKind) -> None:
        """First click: turn the buttons into Confirm / Cancel in the window."""
        if self._selected() is None or self._note_text() is None:
            return
        self._view.confirm = kind
        self._view.different = False
        self._render()

    def _cancel_confirm(self) -> None:
        self._view.confirm = None
        self._render()

    def _confirm_decision(self) -> None:
        kind = self._view.confirm
        step = self._selected()
        note = self._note_text()
        self._view.confirm = None
        if kind is None or step is None or note is None:
            self._render()
            return
        # A new step or round re-renders and drops the confirmation, so the
        # selected step is the one the person confirmed for.
        self._send(step, kind, None, note, "")

    def _send(
        self,
        step: PersonStepStatePayload,
        kind: PersonStepActionKind,
        outcome: PersonStepWireOutcome | None,
        note: str,
        output: str,
    ) -> None:
        view = self._view
        if not view.connected or view.busy is not None:
            return
        view.busy = _key(step)
        view.different = False
        self._set_message("", _OK_COLOR)
        self._render()
        event = ActionEvent(
            action=PersonStepActionPayload(
                action=kind,
                person_step_id=step.person_step_id,
                round=step.round,
                outcome=outcome,
                note=note,
                output=output,
            )
        )
        # Hide (and give the front back) first: the agent's checks start as
        # soon as the answer lands.
        self._hide(lambda: self._writer.send(event))

    # --- person actions: Ask the AI ---------------------------------------------

    def _toggle_ask(self) -> None:
        self._view.ask_open = not self._view.ask_open
        self._view.rendered_qa = None
        self._render()

    def _on_question_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self._question.edit_modified(False)
        step = self._selected()
        if step is not None and self._view.questions_enabled:
            self._update_ask_button(step)

    def _ask(self) -> None:
        view = self._view
        step = self._selected()
        if step is None or not view.questions_enabled:
            return
        if not view.connected or view.asking is not None:
            return
        if _has_pending_question(step):
            self._set_ask_status(
                "Wait for the AI to answer your last question.", _WARN_COLOR
            )
            return
        question = _editable_text(self._question).strip()
        if not question:
            return
        if len(question) > PERSON_STEP_QUESTION_MAX_CHARS:
            self._set_ask_status(
                f"The question is too long ({len(question)} of "
                f"{PERSON_STEP_QUESTION_MAX_CHARS} characters).",
                _ERROR_COLOR,
            )
            return
        view.asking = _key(step)
        self._render()
        # The window stays up: the person keeps working while the AI answers.
        self._writer.send(
            QuestionEvent(
                question=PersonStepQuestionAskPayload(
                    person_step_id=step.person_step_id,
                    round=step.round,
                    question=question,
                )
            )
        )

    def _on_question_result(self, ok: bool, message: str) -> None:
        view = self._view
        asked = view.asking
        view.asking = None
        if ok and asked is not None:
            # Saved: the question now shows in the Q&A, so clear the field.
            if asked == view.rendered:
                _set_editable_text(self._question, "")
            elif asked in view.drafts:
                view.drafts[asked].question = ""
        self._set_ask_status("", _OK_COLOR)
        if not ok:
            self._set_ask_status(message, _ERROR_COLOR)
        elif asked == view.rendered:
            self._set_ask_status(
                message or "Asked. The answer shows above when it is ready.",
                _OK_COLOR,
            )
        self._render()

    # --- person actions: pause, discussion, help --------------------------------

    def _toggle_pause_panel(self) -> None:
        view = self._view
        view.pause_open = not view.pause_open and not view.pause_busy
        if view.pause_open:
            self._pause_status.configure(text="")
        self._render()
        if view.pause_open:
            self._pause_text.focus_set()

    def _pause(self) -> None:
        view = self._view
        case = self._header_case()
        if case is None or case.pause is None or view.pause_busy:
            return
        if not case.pause.can_pause:
            return
        if not view.connected:
            return
        message = _editable_text(self._pause_text).strip()
        if len(message) > CASE_PAUSE_MESSAGE_MAX_CHARS:
            self._pause_status.configure(
                text=f"The message is too long ({len(message)} of "
                f"{CASE_PAUSE_MESSAGE_MAX_CHARS} characters).",
                foreground=_ERROR_COLOR,
            )
            return
        view.pause_busy = True
        view.pause_pivot = case.pivot_id
        self._pause_status.configure(text="")
        self._render()
        # Pausing never hides or raises the window.
        self._writer.send(
            PauseEvent(
                pause=CasePauseActionPayload(pivot_id=case.pivot_id, message=message)
            )
        )

    def _on_chat_modified(self) -> None:
        help_case = self._help_case()
        if help_case is not None:
            self._render_send(help_case[1])

    def _send_chat(self) -> None:
        view = self._view
        help_case = self._help_case()
        if help_case is None or view.chat_busy or not view.connected:
            return
        case, help_state = help_case
        text = self._chat.input_text().strip()
        if not text:
            return
        if len(text) > PERSON_CHAT_TEXT_MAX_CHARS:
            self._chat.set_status(
                f"The message is too long ({len(text)} of "
                f"{PERSON_CHAT_TEXT_MAX_CHARS} characters).",
                _ERROR_COLOR,
            )
            return
        view.chat_busy = True
        self._chat.set_status("")
        self._render()
        # Sending never hides or raises the window.
        self._writer.send(
            ChatSendEvent(
                send=PersonChatSendPayload(
                    pivot_id=case.pivot_id,
                    help_request_id=help_state.help_request_id,
                    text=text,
                )
            )
        )

    def _help_note_text(self) -> str | None:
        note = _editable_text(self._help_note).strip()
        if len(note) > PERSON_HELP_NOTE_MAX_CHARS:
            self._help_status.configure(
                text=f"The note is too long ({len(note)} of "
                f"{PERSON_HELP_NOTE_MAX_CHARS} characters).",
                foreground=_ERROR_COLOR,
            )
            return None
        return note

    def _help_continue(self) -> None:
        help_case = self._help_case()
        note = self._help_note_text()
        if help_case is None or note is None:
            return
        _case, help_state = help_case
        draft = help_state.plan_draft
        use_plan = draft is not None and self._use_plan_var.get()
        try:
            mode: ContinueModeWire | None = ContinueModeWire(self._mode_var.get())
        except ValueError:
            mode = None
        if use_plan:
            mode = ContinueModeWire.CURRENT_SCREEN
        self._send_help(
            PersonHelpActionKind.CONTINUE,
            note,
            mode,
            # The shown draft's id even when not used: the server supersedes
            # only the draft the person saw (as the web form posts it).
            draft.revision_id if draft is not None else None,
            use_plan,
        )

    def _help_ask_confirm(self, kind: PersonHelpActionKind) -> None:
        if self._help_case() is None or self._help_note_text() is None:
            return
        self._view.help_confirm = kind
        self._render()

    def _help_cancel_confirm(self) -> None:
        self._view.help_confirm = None
        self._render()

    def _help_confirm_decision(self) -> None:
        kind = self._view.help_confirm
        note = self._help_note_text()
        self._view.help_confirm = None
        if kind is None or note is None:
            self._render()
            return
        self._send_help(kind, note, None, None, False)

    def _send_help(
        self,
        kind: PersonHelpActionKind,
        note: str,
        mode: ContinueModeWire | None,
        plan_revision_id: int | None,
        use_plan: bool,
    ) -> None:
        view = self._view
        help_case = self._help_case()
        if help_case is None or view.help_busy or not view.connected:
            return
        case, help_state = help_case
        view.help_busy = True
        self._help_status.configure(text="")
        self._render()
        event = HelpActionEvent(
            action=PersonHelpActionPayload(
                pivot_id=case.pivot_id,
                help_request_id=help_state.help_request_id,
                action=kind,
                note=note,
                continue_mode=mode,
                plan_revision_id=plan_revision_id,
                use_plan_revision=use_plan,
            )
        )
        # Like a step answer: hide first, the run may wake at once.
        self._hide(lambda: self._writer.send(event))

    def _on_case_result(self, kind: CaseRequestKind, ok: bool, message: str) -> None:
        view = self._view
        color = _OK_COLOR if ok else _ERROR_COLOR
        if kind is CaseRequestKind.PAUSE:
            if not view.pause_busy:
                return
            view.pause_busy = False
            if ok:
                view.pause_open = False
                _set_editable_text(self._pause_text, "")
                self._pause_status.configure(text="")
                view.pause_note = message or "Pause requested."
                view.pause_note_pivot = view.pause_pivot
            else:
                view.pause_open = True
                self._pause_status.configure(text=message, foreground=color)
        elif kind is CaseRequestKind.CHAT:
            if not view.chat_busy:
                return
            view.chat_busy = False
            if ok:
                self._chat.set_input("")
            self._chat.set_status(message, color)
        else:
            if not view.help_busy:
                return
            view.help_busy = False
            if ok:
                _set_editable_text(self._help_note, "")
            self._help_status.configure(text=message, foreground=color)
        self._render()


def _open_tk() -> "tk.Tk":
    root = tk.Tk(className="TestRunner")
    root.withdraw()
    # Create the (hidden) native frame now, so the first show is an ordinary
    # non-activating show rather than Tk's first map, which focuses it.
    root.update_idletasks()
    return root


def _probe() -> int:
    if _TK_IMPORT_ERROR is not None:
        print(f"tkinter is not installed in this Python: {_TK_IMPORT_ERROR}")
        return EXIT_NO_TKINTER
    try:
        root = _open_tk()
    except tk.TclError as e:
        print(f"Tk cannot open a window (no display?): {e}")
        return EXIT_NO_DISPLAY
    version = str(root.tk.call("info", "patchlevel"))
    root.destroy()
    print(f"Tk {version}")
    return EXIT_OK


def _self_check(text_size: int) -> int:
    """Render the commands on stdin without showing the window (diagnose)."""
    if _TK_IMPORT_ERROR is not None:
        print(f"tkinter is not installed in this Python: {_TK_IMPORT_ERROR}")
        return EXIT_NO_TKINTER
    try:
        root = _open_tk()
    except tk.TclError as e:
        print(f"Tk cannot open a window (no display?): {e}")
        return EXIT_NO_DISPLAY
    errors: list[str] = []

    def record(
        exc: type[BaseException], val: BaseException, tb: TracebackType | None
    ) -> None:
        errors.append("".join(traceback.format_exception(exc, val, tb)))

    root.report_callback_exception = record
    inbox: "queue.Queue[WindowCommand | None]" = queue.Queue()
    # Nothing is registered system-wide while checking.
    app = PersonWindowApp(
        root,
        inbox,
        _EventWriter(io.BytesIO()),
        ManualHotkeys(enabled=False),
        text_size=text_size,
    )
    tabs: set[str] = set()
    try:
        for line in sys.stdin.buffer.read().splitlines():
            if not line.strip():
                continue
            app.apply(decode_command(line))
            for tab in app.tab_frames():
                if app._notebook.tab(tab, "state") == "hidden":
                    continue
                app._notebook.select(tab)
                root.update()
                tabs.add(str(app._notebook.tab(tab, "text")))
        app._change_text_size(1)
        app._change_text_size(-1)
        root.update()
        app._refit()
        if app._manual.preview_sheet():
            tabs.add("mark sheet")
    except Exception:  # every failure is the check's result
        errors.append(traceback.format_exc())
    root.destroy()
    if errors:
        print(errors[0].strip().splitlines()[-1])
        sys.stderr.write("\n".join(errors))
        return EXIT_RENDER_FAILED
    print("rendered: " + ", ".join(sorted(tabs)))
    return EXIT_OK


def _run(text_size: int, *, manual_shortcuts: bool) -> int:
    if _TK_IMPORT_ERROR is not None:
        logger.error("tkinter is not installed in this Python: %s", _TK_IMPORT_ERROR)
        return EXIT_NO_TKINTER
    # Events own the real stdout; anything else printed goes to stderr.
    writer = _EventWriter(sys.stdout.buffer)
    sys.stdout = sys.stderr
    if manual_shortcuts and sys.platform.startswith("linux"):
        # The X11 shortcut thread shares libX11 with Tk: XInitThreads must
        # come before Tk opens its display (libX11 older than 1.8).
        prepare_x11_threads()
    try:
        root = _open_tk()
    except tk.TclError as e:
        logger.error("Tk cannot open a window (no display?): %s", e)
        return EXIT_NO_DISPLAY
    inbox: "queue.Queue[WindowCommand | None]" = queue.Queue()
    reader = threading.Thread(
        target=_read_commands,
        args=(sys.stdin.fileno(), inbox),
        name="person-window-stdin",
        daemon=True,
    )
    reader.start()
    # On the Tk thread, after the root exists (the macOS backend registers on
    # this thread; the Linux portal names the root as its parent window).
    hotkeys = ManualHotkeys(
        enabled=manual_shortcuts, x11_window_id=_x11_window_id(root)
    )
    try:
        app = PersonWindowApp(root, inbox, writer, hotkeys, text_size=text_size)
        app.sync_hotkeys()
        writer.send(ReadyEvent())
        root.after(_POLL_MS, app.poll)
        root.mainloop()
    finally:
        hotkeys.close()
    return EXIT_OK


def _x11_window_id(root: "tk.Tk") -> int:
    """The root's X11 frame id on Linux (Tk is an X client there), else 0."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        return int(root.wm_frame(), 16)
    except (ValueError, tk.TclError):
        return 0


def _shortcut_detail(status: HotkeyStatus) -> str:
    if status.state is HotkeyState.ACTIVE:
        keys = " · ".join(
            f"{kind.value} {trigger}" for kind, trigger in status.triggers.items()
        )
        return f"{keys}; {status.reason}" if status.reason else keys
    return status.reason or status.state.value


def _probe_shortcuts(*, interactive: bool) -> int:
    """Register the Manual run shortcuts, print the result and release them."""
    if _TK_IMPORT_ERROR is not None:
        print(f"tkinter is not installed in this Python: {_TK_IMPORT_ERROR}")
        return EXIT_NO_TKINTER
    if sys.platform.startswith("linux"):
        # As in _run: before Tk opens its display.
        prepare_x11_threads()
    try:
        root = _open_tk()
    except tk.TclError as e:
        print(f"Tk cannot open a window (no display?): {e}")
        return EXIT_NO_DISPLAY
    try:
        # The press prompt goes to stderr: stdout carries only the result.
        with contextlib.redirect_stdout(sys.stderr):
            status = probe_shortcuts(
                root, interactive=interactive, x11_window_id=_x11_window_id(root)
            )
    finally:
        root.destroy()
    print(f"{status.state.value}\t{_shortcut_detail(status)}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test Runner person-step window")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="check that tkinter and a display work, print one line and exit",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="render the window commands read from stdin without showing the "
        "window, print the tabs shown and exit (0 when nothing failed)",
    )
    parser.add_argument(
        "--probe-shortcuts",
        action="store_true",
        help="register the Manual run Pass / Fail shortcuts, print "
        "'<state><TAB><detail>' and release them",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="with --probe-shortcuts, also wait for the Pass shortcut (a Linux "
        "desktop may show its permission dialog)",
    )
    parser.add_argument(
        "--no-manual-shortcuts",
        action="store_true",
        help="do not register the Manual run shortcuts "
        "(CONTROLLER_MANUAL_SHORTCUTS=false)",
    )
    parser.add_argument(
        "--text-size",
        type=int,
        default=TEXT_SIZE_DEFAULT,
        help=f"base text size in points ({TEXT_SIZE_MIN}-{TEXT_SIZE_MAX})",
    )
    args = parser.parse_args(argv)
    if args.interactive and not args.probe_shortcuts:
        parser.error("--interactive can only be used with --probe-shortcuts")
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [person window] %(levelname)s %(message)s",
    )
    text_size = clamp_text_size(args.text_size)
    if args.probe:
        return _probe()
    if args.self_check:
        return _self_check(text_size)
    if args.probe_shortcuts:
        return _probe_shortcuts(interactive=args.interactive)
    return _run(text_size, manual_shortcuts=not args.no_manual_shortcuts)


if __name__ == "__main__":
    sys.exit(main())
