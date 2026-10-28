"""The Test Runner's person-step window (Human in the loop runs).

Run by the runner as ``python -m controller_client.person_window_app``; Tk
owns this process's main thread (an AppKit requirement on macOS). Commands
arrive as JSON lines on stdin (``person_window_ipc``), read by a thread into a
queue that the Tk loop polls; events go back on stdout. The window never
talks to the server.

Every text from the server is shown as plain Tk text (labels, read-only Text
widgets): nothing is interpreted as markup. The window is never topmost,
never grabs focus, starts withdrawn and only shows when the runner says so;
it raises itself once per new step round and hides itself when the person
answers. Closing it only hides it until the next step. Mark failed / Mark
blocked confirm inline (the buttons turn into Confirm / Cancel): the window
opens no other window or dialog, so hiding it hides everything it shows.

Showing never activates the window where the platform allows it:

- Windows: ``wm state normal`` (Tk shows with SW_SHOWNOACTIVATE and, unlike
  ``deiconify``, neither raises nor focuses) plus an explicit
  ``ShowWindow(SW_SHOWNOACTIVATE)`` on the frame; the raise per new round is
  ``SetWindowPos(HWND_TOP, SWP_NOACTIVATE)``. The frame is created hidden at
  start, so the first show goes the same way.
- macOS: Tk activates the Python app whenever it maps a window (measured;
  there is no non-activating map for a window that takes typing), so the
  runner shows it only for a new step round or after the agent has been idle
  for a while (``person_window``); never ``focus_force``. The app that was
  front before a show is recorded and re-activated on every hide (agent
  action, answer, close) before the hide is acknowledged
  (``person_window_front_app``), so the agent's keys reach that app.
- Linux/X11: ``deiconify`` and ``raise`` without any focus request; the window
  manager's focus-stealing prevention decides.

``--probe`` checks that tkinter imports and a Tk display opens, prints one
line and exits (0 when usable).
"""

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO, Final

from controller_client.exceptions import ProtocolError
from controller_client.person_window_front_app import FrontAppKeeper
from controller_client.person_window_ipc import (
    IPC_TO_WINDOW_MAX_BYTES,
    ActionEvent,
    ClosedByUserEvent,
    ConnectionCommand,
    HiddenEvent,
    HideCommand,
    QuitCommand,
    ReadyEvent,
    ResultCommand,
    ShowCommand,
    StateCommand,
    WindowCommand,
    WindowEvent,
    decode_command,
    encode_event,
)
from controller_client.protocol import (
    PERSON_STEP_NOTE_MAX_CHARS,
    PERSON_STEP_OUTPUT_MAX_CHARS,
    PersonStepActionKind,
    PersonStepActionPayload,
    PersonStepStatePayload,
    PersonStepWireKind,
    PersonStepWireOutcome,
    PersonStepWireStatus,
)

_TK_IMPORT_ERROR: ImportError | None = None
try:
    import tkinter as tk
    from tkinter import ttk
except ImportError as _e:  # tkinter is an optional part of some Pythons
    _TK_IMPORT_ERROR = _e

logger = logging.getLogger("controller_client.person_window_app")

# Exit codes of --probe (and of a normal start that cannot open Tk).
EXIT_OK: Final[int] = 0
EXIT_NO_TKINTER: Final[int] = 3
EXIT_NO_DISPLAY: Final[int] = 4

_POLL_MS: Final[int] = 50
# How often a hide checks whether the previous front app is back (macOS).
_RESTORE_POLL_MS: Final[int] = 10
_READ_CHUNK_BYTES: Final[int] = 64 * 1024
_TITLE: Final[str] = "Test Runner: your step"
_WRAP: Final[int] = 480
_WARN_COLOR: Final[str] = "#b45309"
_ERROR_COLOR: Final[str] = "#b91c1c"
_OK_COLOR: Final[str] = "#15803d"

# Win32 ShowWindow / SetWindowPos arguments.
_SW_SHOWNOACTIVATE: Final[int] = 4
_HWND_TOP: Final[int] = 0
_SWP_NOSIZE: Final[int] = 0x0001
_SWP_NOMOVE: Final[int] = 0x0002
_SWP_NOACTIVATE: Final[int] = 0x0010

_StepKey = tuple[int, int]


def _key(step: PersonStepStatePayload) -> _StepKey:
    return (step.person_step_id, step.round)


@dataclass
class _Draft:
    outcome: str = PersonStepWireOutcome.AS_EXPECTED.value
    note: str = ""
    output: str = ""


@dataclass
class _ViewState:
    steps: tuple[PersonStepStatePayload, ...] = ()
    selected_id: int | None = None
    rendered: _StepKey | None = None
    drafts: dict[_StepKey, _Draft] = field(default_factory=dict)
    raised: set[_StepKey] = field(default_factory=set)
    # The step whose answer is with the server (buttons stay disabled).
    busy: _StepKey | None = None
    # Mark failed / Mark blocked waiting for its inline confirmation.
    confirm: PersonStepActionKind | None = None
    connected: bool = True


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


def _set_readonly_text(widget: "tk.Text", text: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")


def _editable_text(widget: "tk.Text") -> str:
    return widget.get("1.0", "end-1c")


def _set_editable_text(widget: "tk.Text", text: str) -> None:
    widget.delete("1.0", "end")
    widget.insert("1.0", text)


class PersonWindowApp:
    """The window's widgets and behaviour; runs on the Tk main thread."""

    def __init__(
        self,
        root: "tk.Tk",
        inbox: "queue.Queue[WindowCommand | None]",
        writer: _EventWriter,
    ) -> None:
        self._root = root
        self._inbox = inbox
        self._writer = writer
        self._view = _ViewState()
        self._front_app = FrontAppKeeper()
        self._outcome = tk.StringVar(
            master=root, value=PersonStepWireOutcome.AS_EXPECTED.value
        )
        self._picker_var = tk.StringVar(master=root)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_user_close)
        if sys.platform == "darwin":
            # Cmd+Q / the app menu's Quit would end Tk; treat it as a close.
            # (Misc.register is typed; createcommand is not in typeshed.)
            close_command = root.register(self._on_user_close)
            root.tk.call("proc", "::tk::mac::Quit", "", close_command)

    # --- layout -----------------------------------------------------------

    def _build(self) -> None:
        root = self._root
        root.title(_TITLE)
        root.minsize(420, 360)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        self._frame = frame

        self._header = ttk.Label(
            frame, text="", font=("TkDefaultFont", 13, "bold"), wraplength=_WRAP
        )
        self._header.grid(row=0, column=0, sticky="w")
        self._subheader = ttk.Label(frame, text="", wraplength=_WRAP)
        self._subheader.grid(row=1, column=0, sticky="w", pady=(2, 6))

        self._picker_frame = ttk.Frame(frame)
        self._picker_label = ttk.Label(self._picker_frame, text="")
        self._picker_label.pack(side="left")
        self._picker = ttk.Combobox(
            self._picker_frame, state="readonly", textvariable=self._picker_var
        )
        self._picker.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._picker.bind("<<ComboboxSelected>>", self._on_pick)
        self._picker_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self._status = ttk.Label(
            frame, text="", foreground=_WARN_COLOR, wraplength=_WRAP
        )
        self._status.grid(row=3, column=0, sticky="w")

        body = ttk.Frame(frame)
        body.grid(row=4, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        self._body = body

        ttk.Label(body, text="What to do").grid(row=0, column=0, sticky="w")
        self._instruction = self._text(body, row=1, height=6, readonly=True)
        ttk.Label(body, text="Expected result").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        self._expected = self._text(body, row=3, height=3, readonly=True)

        self._objection_frame = ttk.Frame(body)
        self._objection_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self._objection_frame,
            text="The agent could not confirm your report",
            foreground=_WARN_COLOR,
        ).grid(row=0, column=0, sticky="w")
        self._objection = self._text(
            self._objection_frame, row=1, height=3, readonly=True
        )
        self._objection_frame.grid(row=4, column=0, sticky="ew", pady=(6, 0))

        self._command_frame = ttk.Frame(body)
        self._command_frame.columnconfigure(0, weight=1)
        command_head = ttk.Frame(self._command_frame)
        command_head.grid(row=0, column=0, sticky="ew")
        self._command_label = ttk.Label(command_head, text="Command")
        self._command_label.pack(side="left")
        ttk.Button(command_head, text="Copy", command=self._copy_command).pack(
            side="right"
        )
        self._command = self._text(
            self._command_frame, row=1, height=3, readonly=True, mono=True
        )
        self._command_frame.grid(row=5, column=0, sticky="ew", pady=(6, 0))

        outcome_row = ttk.Frame(body)
        outcome_row.grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Label(outcome_row, text="How did it go?").pack(side="left")
        ttk.Radiobutton(
            outcome_row,
            text="As expected",
            value=PersonStepWireOutcome.AS_EXPECTED.value,
            variable=self._outcome,
        ).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(
            outcome_row,
            text="Different",
            value=PersonStepWireOutcome.DIFFERENT.value,
            variable=self._outcome,
        ).pack(side="left", padx=(8, 0))

        ttk.Label(body, text="Note for the agent").grid(
            row=7, column=0, sticky="w", pady=(6, 0)
        )
        self._note = self._text(body, row=8, height=3, readonly=False)

        self._output_frame = ttk.Frame(body)
        self._output_frame.columnconfigure(0, weight=1)
        ttk.Label(self._output_frame, text="Command output").grid(
            row=0, column=0, sticky="w"
        )
        self._output = self._text(
            self._output_frame, row=1, height=5, readonly=False, mono=True
        )
        self._output_frame.grid(row=9, column=0, sticky="ew", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=10, column=0, sticky="w", pady=(10, 0))
        self._buttons = buttons
        self._report_button = ttk.Button(buttons, text="Report", command=self._report)
        self._report_button.pack(side="left")
        self._overrule_button = ttk.Button(
            buttons, text="Overrule", command=self._overrule
        )
        self._overrule_button.pack(side="left", padx=(6, 0))
        self._fail_button = ttk.Button(
            buttons, text="Mark failed", command=self._mark_failed
        )
        self._fail_button.pack(side="left", padx=(6, 0))
        self._block_button = ttk.Button(
            buttons, text="Mark blocked", command=self._mark_blocked
        )
        self._block_button.pack(side="left", padx=(6, 0))

        # Shown in place of the buttons while Mark failed / blocked waits for
        # its confirmation (no separate dialog: see the module docstring).
        self._confirm_frame = ttk.Frame(body)
        self._confirm_frame.columnconfigure(0, weight=1)
        self._confirm_label = ttk.Label(
            self._confirm_frame, text="", foreground=_WARN_COLOR, wraplength=_WRAP
        )
        self._confirm_label.grid(row=0, column=0, columnspan=2, sticky="w")
        confirm_buttons = ttk.Frame(self._confirm_frame)
        confirm_buttons.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._confirm_button = ttk.Button(
            confirm_buttons, text="", command=self._confirm_decision
        )
        self._confirm_button.pack(side="left")
        ttk.Button(confirm_buttons, text="Cancel", command=self._cancel_confirm).pack(
            side="left", padx=(6, 0)
        )
        self._confirm_frame.grid(row=10, column=0, sticky="ew", pady=(10, 0))
        self._confirm_frame.grid_remove()
        self._hint = ttk.Label(body, text="", wraplength=_WRAP)
        self._hint.grid(row=11, column=0, sticky="w", pady=(4, 0))

        self._message = ttk.Label(frame, text="", wraplength=_WRAP)
        self._message.grid(row=5, column=0, sticky="w", pady=(8, 0))

        self._url_frame = ttk.Frame(frame)
        ttk.Label(self._url_frame, text="Case page").pack(side="left")
        self._url_var = tk.StringVar(master=root)
        url_entry = ttk.Entry(
            self._url_frame, textvariable=self._url_var, state="readonly"
        )
        url_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(self._url_frame, text="Copy", command=self._copy_url).pack(
            side="right"
        )
        self._url_frame.grid(row=6, column=0, sticky="ew", pady=(8, 0))

    def _text(
        self,
        parent: "ttk.Frame",
        *,
        row: int,
        height: int,
        readonly: bool,
        mono: bool = False,
    ) -> "tk.Text":
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=0, sticky="ew")
        holder.columnconfigure(0, weight=1)
        widget = tk.Text(
            holder,
            height=height,
            width=60,
            wrap="char" if mono else "word",
            font="TkFixedFont" if mono else "TkTextFont",
            borderwidth=1,
            relief="solid",
            undo=not readonly,
        )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        widget.grid(row=0, column=0, sticky="ew")
        scroll.grid(row=0, column=1, sticky="ns")
        if readonly:
            widget.configure(state="disabled", takefocus=False)
        return widget

    # --- commands from the runner ------------------------------------------

    def poll(self) -> None:
        try:
            while True:
                command = self._inbox.get_nowait()
                if command is None or isinstance(command, QuitCommand):
                    self._root.destroy()
                    return
                self._apply(command)
        except queue.Empty:
            pass
        if self._writer.broken:
            self._root.destroy()
            return
        self._root.after(_POLL_MS, self.poll)

    def _apply(self, command: WindowCommand) -> None:
        match command:
            case StateCommand(steps=steps):
                self._set_steps(steps)
            case ShowCommand():
                self._show()
            case HideCommand(seq=seq):
                self._hide(lambda: self._writer.send(HiddenEvent(seq=seq)))
            case ResultCommand(ok=ok, message=message):
                self._view.busy = None
                self._set_message(message, _OK_COLOR if ok else _ERROR_COLOR)
                self._render()
            case ConnectionCommand(connected=connected):
                self._view.connected = connected
                self._render()
            case QuitCommand():
                pass

    def _set_steps(self, steps: tuple[PersonStepStatePayload, ...]) -> None:
        view = self._view
        view.steps = steps
        ids = [step.person_step_id for step in steps]
        if view.selected_id not in ids:
            view.selected_id = ids[0] if ids else None
        keys = {_key(step) for step in steps}
        if view.busy is not None and view.busy not in keys:
            view.busy = None
        view.drafts = {k: d for k, d in view.drafts.items() if k in keys}
        if not steps:
            self._hide()
        self._render()

    def _show(self) -> None:
        if not self._view.steps and not self._message.cget("text"):
            return
        root = self._root
        fresh = {_key(step) for step in self._view.steps} - self._view.raised
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
        # Once per new step round.
        self._view.raised |= fresh

    def _hide(self, done: Callable[[], None] | None = None) -> None:
        """Hide; ``done`` runs once the previous front app is back (macOS)."""
        # A pending confirmation never survives a hide: the person sees the
        # plain buttons again when the window comes back.
        if self._view.confirm is not None:
            self._view.confirm = None
            self._render()
        # An open picker list is its own Tk window, not hidden by withdraw().
        try:
            self._picker.tk.call("ttk::combobox::Unpost", self._picker)
        except tk.TclError:
            pass
        self._root.withdraw()
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

    # --- rendering ----------------------------------------------------------

    def _selected(self) -> PersonStepStatePayload | None:
        for step in self._view.steps:
            if step.person_step_id == self._view.selected_id:
                return step
        return None

    def _render(self) -> None:
        view = self._view
        step = self._selected()
        key = _key(step) if step is not None else None
        if key != view.rendered:
            view.confirm = None
            self._save_draft()
            if key is not None:
                # A message about another step does not belong to this one.
                self._set_message("", _OK_COLOR)
            view.rendered = key
            self._load_draft()
        self._render_picker(step)
        if step is None:
            self._header.configure(text="Nothing is waiting for you right now.")
            self._subheader.configure(text="")
            self._body.grid_remove()
            self._url_frame.grid_remove()
            self._status.configure(text=self._connection_text())
            return
        self._body.grid()
        self._header.configure(text=f"{step.run_name} · {step.case_title}")
        kind = " · restore step" if step.kind is PersonStepWireKind.RESTORE else ""
        self._subheader.configure(
            text=f"{step.step_label} · attempt {step.round}{kind}"
        )
        status_lines = [self._connection_text()] + list(step.blockers)
        self._status.configure(text="\n".join(line for line in status_lines if line))
        _set_readonly_text(self._instruction, step.instruction)
        _set_readonly_text(self._expected, step.expected_result or "(none given)")
        not_confirmed = step.status is PersonStepWireStatus.NOT_CONFIRMED
        if not_confirmed and step.objection:
            _set_readonly_text(self._objection, step.objection)
            self._objection_frame.grid()
        else:
            self._objection_frame.grid_remove()
        if step.command:
            shell = f" (run it in {step.shell_label})" if step.shell_label else ""
            self._command_label.configure(text=f"Command{shell}")
            _set_readonly_text(self._command, step.command)
            self._command_frame.grid()
        else:
            self._command_frame.grid_remove()
        if step.kind is PersonStepWireKind.COMMAND:
            self._output_frame.grid()
        else:
            self._output_frame.grid_remove()
        if step.case_url:
            self._url_var.set(step.case_url)
            self._url_frame.grid()
        else:
            self._url_frame.grid_remove()
        self._render_buttons(step, not_confirmed)

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
        return f"{step.run_name} · {step.case_title} · {step.step_label}"

    def _render_buttons(
        self, step: PersonStepStatePayload, not_confirmed: bool
    ) -> None:
        view = self._view
        idle = view.connected and view.busy is None
        can_report = idle and not step.blockers
        self._report_button.configure(
            text="Report again" if not_confirmed else "Report",
            state="normal" if can_report else "disabled",
        )
        if not_confirmed:
            self._overrule_button.pack(
                side="left", padx=(6, 0), after=self._report_button
            )
            self._overrule_button.configure(state="normal" if idle else "disabled")
        else:
            self._overrule_button.pack_forget()
        for button in (self._fail_button, self._block_button):
            button.configure(state="normal" if idle else "disabled")
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
            self._buttons.grid_remove()
            self._confirm_frame.grid()
            self._hint.configure(text="")
            return
        self._confirm_frame.grid_remove()
        self._buttons.grid()
        if view.busy is not None:
            hint = "Sending your answer…"
        elif not_confirmed:
            hint = (
                "Report again sends a new report for the agent to check. Overrule "
                "records the step as passed without another check."
            )
        else:
            hint = "Report hands the step to the agent to confirm."
        self._hint.configure(text=hint)

    def _set_message(self, text: str, color: str) -> None:
        self._message.configure(text=text, foreground=color)

    def _save_draft(self) -> None:
        if self._view.rendered is None:
            return
        self._view.drafts[self._view.rendered] = _Draft(
            outcome=self._outcome.get(),
            note=_editable_text(self._note),
            output=_editable_text(self._output),
        )

    def _load_draft(self) -> None:
        rendered = self._view.rendered
        draft = self._view.drafts.get(rendered, _Draft()) if rendered else _Draft()
        self._outcome.set(draft.outcome)
        _set_editable_text(self._note, draft.note)
        _set_editable_text(self._output, draft.output)

    # --- person actions ----------------------------------------------------

    def _on_pick(self, _event: "tk.Event[ttk.Combobox]") -> None:
        index = self._picker.current()
        if 0 <= index < len(self._view.steps):
            self._view.selected_id = self._view.steps[index].person_step_id
            self._render()

    def _on_user_close(self) -> None:
        self._hide(lambda: self._writer.send(ClosedByUserEvent()))

    def _copy(self, text: str) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(text)

    def _copy_command(self) -> None:
        step = self._selected()
        if step is not None:
            self._copy(step.command)

    def _copy_url(self) -> None:
        step = self._selected()
        if step is not None and step.case_url:
            self._copy(step.case_url)

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

    def _report(self) -> None:
        step = self._selected()
        note = self._note_text()
        if step is None or note is None:
            return
        try:
            outcome = PersonStepWireOutcome(self._outcome.get())
        except ValueError:
            self._set_message("Choose how the step went.", _ERROR_COLOR)
            return
        if outcome is PersonStepWireOutcome.DIFFERENT and not note:
            self._set_message("Describe in the note what was different.", _ERROR_COLOR)
            return
        output = ""
        if step.kind is PersonStepWireKind.COMMAND:
            output = _editable_text(self._output)
            if len(output) > PERSON_STEP_OUTPUT_MAX_CHARS:
                self._set_message(
                    f"The output is too long ({len(output)} of "
                    f"{PERSON_STEP_OUTPUT_MAX_CHARS} characters). Paste the "
                    "relevant part.",
                    _ERROR_COLOR,
                )
                return
        self._send(step, PersonStepActionKind.REPORT, outcome, note, output)

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


def _run() -> int:
    if _TK_IMPORT_ERROR is not None:
        logger.error("tkinter is not installed in this Python: %s", _TK_IMPORT_ERROR)
        return EXIT_NO_TKINTER
    # Events own the real stdout; anything else printed goes to stderr.
    writer = _EventWriter(sys.stdout.buffer)
    sys.stdout = sys.stderr
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
    app = PersonWindowApp(root, inbox, writer)
    writer.send(ReadyEvent())
    root.after(_POLL_MS, app.poll)
    root.mainloop()
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Runner person-step window")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="check that tkinter and a display work, print one line and exit",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [person window] %(levelname)s %(message)s",
    )
    if args.probe:
        return _probe()
    return _run()


if __name__ == "__main__":
    sys.exit(main())
