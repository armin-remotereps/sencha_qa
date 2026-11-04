"""The Manual run tab's mark sheet (Test Runner 0.17.0).

A Pass / Fail shortcut (or the tab's Pass… / Fail… / Edit… buttons) opens it
over the whole window once the first screenshot is staged. It is an inline
frame placed over the root, never a ``Toplevel``: hiding the root for a
screenshot hides the sheet with it, and the window still opens no other
window.

The sheet shows the step, Pass / Fail, the screenshots staged in it (each with
Retake and Remove, up to the server's ``step_max_shots``), the step's note
(required for a Fail), and:

- on the last step left to mark: "Hand in and start the next case" (ticked),
  "Hand in as" Passed / Failed / Blocked (prefilled from the marks) and an
  optional case note;
- on a Fail before that: "Stop here and hand the case in as Failed"
  (unticked).

Save (Ctrl+Enter, macOS Cmd+Enter) and Cancel (Esc). The sheet only holds
widgets and what the tester typed; the mark and the hand-in are the
session's (``person_window_mark_session.MarkSession``, captures through the
tab), which renders it with a ``SheetView``.
"""

import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from controller_client.protocol import (
    MANUAL_STEP_NOTE_MAX_CHARS,
    ManualCaseVerdictWire,
    ManualStepOutcome,
)

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# Same palette as person_window_app.
_WARN_COLOR: Final[str] = "#b45309"
_ERROR_COLOR: Final[str] = "#b91c1c"
_MUTED_COLOR: Final[str] = "#6b7280"
_HEADING_FONT: Final[str] = "PunkHazardHeading"
_BOLD_FONT: Final[str] = "PunkHazardBold"
# Shots per row of the sheet (half-size thumbnails).
_SHOT_COLUMNS: Final[int] = 3
_HALF_CACHE: Final[int] = 24
_VERDICT_LABELS: Final[dict[ManualCaseVerdictWire, str]] = {
    ManualCaseVerdictWire.PASSED: "Passed",
    ManualCaseVerdictWire.FAILED: "Failed",
    ManualCaseVerdictWire.BLOCKED: "Blocked",
}
# Save and Cancel from the keyboard (Command on macOS).
_SAVE_KEYS: Final[tuple[str, ...]] = (
    ("<Command-Return>",) if sys.platform == "darwin" else ("<Control-Return>",)
)
_CANCEL_KEY: Final[str] = "<Escape>"
SAVE_KEY_LABEL: Final[str] = "⌘↩" if sys.platform == "darwin" else "Ctrl+Enter"


class SheetHost(Protocol):
    """What the sheet needs from the window (``PersonWindowApp``)."""

    def manual_label(
        self, parent: "tk.Misc", text: str = "", foreground: str = ""
    ) -> "ttk.Label": ...

    def manual_entry(
        self, parent: "tk.Misc", *, row: int, height: int
    ) -> "tk.Text": ...

    def manual_scroll_area(self, parent: "tk.Misc") -> tuple["ttk.Frame", "ttk.Frame"]:
        """A scrollable area (outer frame, content frame) in ``parent``."""
        ...


@dataclass(frozen=True)
class HandIn:
    """Hand the case in after the mark is saved, then start the next case."""

    verdict: ManualCaseVerdictWire
    note: str


@dataclass(frozen=True)
class SheetView:
    """What the tab tells the sheet on every render."""

    # The sheet's screenshots in order (kept ones of an edited mark first).
    shots: tuple[str, ...]
    max_shots: int
    capturing: bool
    saving: bool
    # Why Save is off for now (the case is paused, ...); blank otherwise.
    blocker: str
    status: str
    status_color: str


class HalfThumbnails:
    """Half-size copies of thumbnails, by screenshot key (LRU)."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, tuple["tk.PhotoImage", "tk.PhotoImage"]] = (
            OrderedDict()
        )

    def get(self, key: str, image: "tk.PhotoImage") -> "tk.PhotoImage":
        cached = self._cache.get(key)
        if cached is not None and cached[0] is image:
            self._cache.move_to_end(key)
            return cached[1]
        half = image.subsample(2)
        self._cache[key] = (image, half)
        while len(self._cache) > _HALF_CACHE:
            self._cache.popitem(last=False)
        return half


def _text(widget: "tk.Text") -> str:
    return widget.get("1.0", "end-1c")


def _set_text(widget: "tk.Text", text: str) -> None:
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.edit_modified(False)


class MarkSheet:
    """The sheet's widgets; every method runs on the Tk thread."""

    def __init__(
        self,
        host: SheetHost,
        root: "tk.Tk",
        *,
        thumbnail: Callable[[str], "tk.PhotoImage | None"],
        on_add: Callable[[], None],
        on_retake: Callable[[str], None],
        on_remove: Callable[[str], None],
        on_save: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._host = host
        self._root = root
        self._thumbnail = thumbnail
        self._on_add = on_add
        self._on_retake = on_retake
        self._on_remove = on_remove
        self._on_save = on_save
        self._on_cancel = on_cancel
        self._open = False
        self._last_step = False
        self._edit = False
        self._other_failed = False
        self._verdict_touched = False
        # The inline confirmation of a hand-in that contradicts the steps.
        self._confirming = ""
        self._view: SheetView | None = None
        # What the shots row shows: rebuilt only when it changes.
        self._rendered_shots: tuple[object, ...] | None = None
        self._outcome_var = tk.StringVar(
            master=root, value=ManualStepOutcome.PASS.value
        )
        self._verdict_var = tk.StringVar(master=root, value="")
        self._start_next_var = tk.BooleanVar(master=root, value=True)
        self._stop_var = tk.BooleanVar(master=root, value=False)
        self._build()
        for sequence in _SAVE_KEYS:
            root.bind_all(sequence, self._on_save_key, add="+")
        root.bind_all(_CANCEL_KEY, self._on_cancel_key, add="+")

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        host = self._host
        # A plain frame over the root's grid; ``place`` never resizes the root.
        outer = ttk.Frame(self._root)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        self._outer = outer
        scroll_outer, body = host.manual_scroll_area(outer)
        scroll_outer.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        self._title = host.manual_label(body, "")
        self._title.configure(font=_HEADING_FONT)
        self._title.grid(row=0, column=0, sticky="w")
        self._step_line = host.manual_label(body, "", foreground=_MUTED_COLOR)
        self._step_line.grid(row=1, column=0, sticky="w", pady=(2, 0))
        choice = ttk.Frame(body)
        choice.grid(row=2, column=0, sticky="w", pady=(8, 0))
        for outcome, label in (
            (ManualStepOutcome.PASS, "Pass"),
            (ManualStepOutcome.FAIL, "Fail"),
        ):
            ttk.Radiobutton(
                choice,
                text=label,
                value=outcome.value,
                variable=self._outcome_var,
                command=self._on_outcome,
            ).pack(side="left", padx=(0, 12))

        ttk.Label(body, text="Screenshots", font=_BOLD_FONT).grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )
        self._shots = ttk.Frame(body)
        self._shots.grid(row=4, column=0, sticky="w", pady=(4, 0))
        self._add_button = ttk.Button(body, text="", command=self._on_add)
        self._add_button.grid(row=5, column=0, sticky="w", pady=(6, 0))

        self._note_label = ttk.Label(body, text="")
        self._note_label.grid(row=6, column=0, sticky="w", pady=(10, 0))
        self._note = host.manual_entry(body, row=7, height=4)
        self._note.bind("<<Modified>>", self._on_note_modified)
        for sequence in _SAVE_KEYS:
            # Before the Text class binding, which would insert a newline.
            self._note.bind(sequence, self._on_save_key)

        self._stop_check = ttk.Checkbutton(
            body,
            text="Stop here and hand the case in as Failed",
            variable=self._stop_var,
            command=self._changed,
        )
        self._stop_check.grid(row=8, column=0, sticky="w", pady=(8, 0))
        self._build_hand_in(body, row=9)

        self._status = host.manual_label(body, "")
        self._status.grid(row=10, column=0, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=11, column=0, sticky="w", pady=(8, 0))
        self._save_button = ttk.Button(buttons, text="Save", command=self._save)
        self._save_button.pack(side="left")
        self._cancel_button = ttk.Button(
            buttons, text="Cancel (Esc)", command=self._cancel
        )
        self._cancel_button.pack(side="left", padx=(6, 0))

    def _build_hand_in(self, body: "ttk.Frame", *, row: int) -> None:
        frame = ttk.Frame(body)
        frame.columnconfigure(0, weight=1)
        frame.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        self._hand_in_frame = frame
        self._last_label = self._host.manual_label(
            frame, "This is the last step left to mark.", foreground=_MUTED_COLOR
        )
        self._last_label.grid(row=0, column=0, sticky="w")
        self._start_next_check = ttk.Checkbutton(
            frame,
            text="Hand in and start the next case",
            variable=self._start_next_var,
            command=self._changed,
        )
        self._start_next_check.grid(row=1, column=0, sticky="w", pady=(4, 0))
        verdicts = ttk.Frame(frame)
        verdicts.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._verdicts = verdicts
        ttk.Label(verdicts, text="Hand in as").pack(side="left", padx=(0, 8))
        for verdict, label in _VERDICT_LABELS.items():
            ttk.Radiobutton(
                verdicts,
                text=label,
                value=verdict.value,
                variable=self._verdict_var,
                command=self._on_verdict,
            ).pack(side="left", padx=(0, 10))
        note_box = ttk.Frame(frame)
        note_box.columnconfigure(0, weight=1)
        note_box.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self._case_note_box = note_box
        self._case_note_label = ttk.Label(note_box, text="")
        self._case_note_label.grid(row=0, column=0, sticky="w")
        self._case_note = self._host.manual_entry(note_box, row=1, height=3)
        self._case_note.bind("<<Modified>>", self._on_case_note_modified)

    # --- what the tab asks --------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    def open(
        self,
        *,
        title: str,
        step_line: str,
        outcome: ManualStepOutcome,
        note: str,
        case_note: str,
        last_step: bool,
        other_failed: bool,
        edit: bool,
    ) -> None:
        """Show the sheet over the window, filled for one step.

        ``last_step``: no other step is left to mark (the hand-in block).
        ``other_failed``: another step of the case is marked failed (the
        verdict prefill). ``edit``: an existing mark (no hand-in, no Stop
        here).
        """
        self._open = True
        self._last_step = last_step and not edit
        self._edit = edit
        self._other_failed = other_failed
        self._verdict_touched = False
        self._confirming = ""
        self._rendered_shots = None
        self._title.configure(text=title)
        self._step_line.configure(text=step_line)
        self._outcome_var.set(outcome.value)
        self._start_next_var.set(True)
        self._stop_var.set(False)
        self._prefill_verdict()
        _set_text(self._note, note)
        _set_text(self._case_note, case_note)
        self._outer.place(in_=self._root, x=0, y=0, relwidth=1.0, relheight=1.0)
        self._outer.tkraise()
        self._update()

    def close(self) -> None:
        self._open = False
        self._view = None
        self._outer.place_forget()

    def focus_note(self) -> None:
        self._note.focus_set()
        self._note.mark_set("insert", "end")

    def outcome(self) -> ManualStepOutcome:
        return ManualStepOutcome(self._outcome_var.get())

    def set_outcome(self, outcome: ManualStepOutcome) -> None:
        self._outcome_var.set(outcome.value)
        self._on_outcome()

    def note(self) -> str:
        return _text(self._note)

    def hand_in(self) -> HandIn | None:
        """The hand-in that follows Save, or None (only the mark)."""
        note = _text(self._case_note)
        if self._last_step:
            if not self._start_next_var.get():
                return None
            try:
                verdict = ManualCaseVerdictWire(self._verdict_var.get())
            except ValueError:
                return None
            return HandIn(verdict=verdict, note=note)
        if self._stop_shown() and self._stop_var.get():
            return HandIn(verdict=ManualCaseVerdictWire.FAILED, note=note)
        return None

    @property
    def confirming(self) -> bool:
        return bool(self._confirming)

    def ask_confirm(self, text: str) -> None:
        """Save once more hands in anyway; any change takes it back."""
        self._confirming = text
        self._update()

    def render(self, view: SheetView) -> None:
        self._view = view
        self._render_shots(view)
        count = len(view.shots)
        if view.max_shots <= 1:
            self._add_button.grid_remove()
        else:
            self._add_button.grid()
            self._add_button.configure(
                text=f"Add screenshot ({min(count + 1, view.max_shots)} of "
                f"{view.max_shots})",
                state=(
                    "normal"
                    if count < view.max_shots and not view.capturing and not view.saving
                    else "disabled"
                ),
            )
        self._update()

    # --- rendering ----------------------------------------------------------

    def _render_shots(self, view: SheetView) -> None:
        images = tuple(self._thumbnail(key) for key in view.shots)
        key = (view.shots, images, view.capturing, view.saving)
        if key == self._rendered_shots:
            return
        self._rendered_shots = key
        for child in self._shots.winfo_children():
            child.destroy()
        busy = view.capturing or view.saving
        for index, (shot, image) in enumerate(zip(view.shots, images)):
            cell = ttk.Frame(self._shots)
            cell.grid(
                row=index // _SHOT_COLUMNS,
                column=index % _SHOT_COLUMNS,
                sticky="nw",
                padx=(0, 6),
                pady=(0, 6),
            )
            thumb = ttk.Label(cell, text="" if image is not None else "…")
            if image is not None:
                thumb.configure(image=image)
            thumb.grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Button(
                cell,
                text="Retake",
                command=self._shot_command(self._on_retake, shot),
                state="disabled" if busy else "normal",
            ).grid(row=1, column=0, sticky="w", pady=(2, 0))
            ttk.Button(
                cell,
                text="Remove",
                command=self._shot_command(self._on_remove, shot),
                # A mark keeps at least one screenshot.
                state="disabled" if busy or len(view.shots) < 2 else "normal",
            ).grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(2, 0))

    @staticmethod
    def _shot_command(action: Callable[[str], None], shot: str) -> Callable[[], None]:
        return lambda: action(shot)

    def _stop_shown(self) -> bool:
        return (
            not self._edit
            and not self._last_step
            and self.outcome() is ManualStepOutcome.FAIL
        )

    def _update(self) -> None:
        """Labels, the hand-in block and Save from what the sheet holds."""
        if not self._open:
            return
        failed = self.outcome() is ManualStepOutcome.FAIL
        self._note_label.configure(
            text="What went wrong? (required)" if failed else "Note (optional)"
        )
        stop = self._stop_shown()
        if stop:
            self._stop_check.grid()
        else:
            self._stop_check.grid_remove()
        hand_in = self.hand_in()
        if self._last_step:
            self._hand_in_frame.grid()
            self._last_label.grid()
            self._start_next_check.grid()
            if self._start_next_var.get():
                self._verdicts.grid()
            else:
                self._verdicts.grid_remove()
        elif stop and self._stop_var.get():
            self._hand_in_frame.grid()
            self._last_label.grid_remove()
            self._start_next_check.grid_remove()
            self._verdicts.grid_remove()
        else:
            self._hand_in_frame.grid_remove()
        if hand_in is not None:
            self._case_note_box.grid()
            self._case_note_label.configure(text=self._case_note_text(hand_in.verdict))
        else:
            self._case_note_box.grid_remove()
        self._update_save(hand_in)

    def _case_note_text(self, verdict: ManualCaseVerdictWire) -> str:
        if verdict is ManualCaseVerdictWire.BLOCKED:
            return "Why is the case blocked? (required)"
        any_failed = self._other_failed or self.outcome() is ManualStepOutcome.FAIL
        if verdict is ManualCaseVerdictWire.FAILED and not any_failed:
            return "Why did the case fail? (required: no step failed)"
        return "Note for the case (optional)"

    def _update_save(self, hand_in: HandIn | None) -> None:
        view = self._view
        note = _text(self._note).strip()
        failed = self.outcome() is ManualStepOutcome.FAIL
        can = (
            view is not None
            and bool(view.shots)
            and not view.capturing
            and not view.saving
            and not view.blocker
            and not (failed and not note)
        )
        if view is not None and view.saving:
            text = "Saving…"
        elif self._confirming:
            text = "Hand in anyway"
        elif hand_in is not None:
            text = f"Save and hand in ({SAVE_KEY_LABEL})"
        else:
            text = f"Save ({SAVE_KEY_LABEL})"
        self._save_button.configure(text=text, state="normal" if can else "disabled")
        self._cancel_button.configure(
            state="disabled" if view is not None and view.saving else "normal"
        )
        if self._confirming:
            status, color = self._confirming, _WARN_COLOR
        elif view is not None and view.capturing:
            status, color = "Taking the screenshot…", _MUTED_COLOR
        elif view is not None and view.status:
            status, color = view.status, view.status_color
        elif view is not None and view.blocker:
            status, color = view.blocker, _WARN_COLOR
        elif len(note) > MANUAL_STEP_NOTE_MAX_CHARS:
            status, color = (
                f"The note is too long ({len(note)} of "
                f"{MANUAL_STEP_NOTE_MAX_CHARS} characters).",
                _ERROR_COLOR,
            )
        elif failed and not note:
            status, color = "Describe in the note what went wrong.", _WARN_COLOR
        else:
            status, color = "", _MUTED_COLOR
        self._status.configure(text=status, foreground=color)

    def _prefill_verdict(self) -> None:
        failed = self._other_failed or self.outcome() is ManualStepOutcome.FAIL
        self._verdict_var.set(
            (
                ManualCaseVerdictWire.FAILED if failed else ManualCaseVerdictWire.PASSED
            ).value
        )

    # --- tester input -------------------------------------------------------

    def _changed(self) -> None:
        self._confirming = ""
        self._update()

    def _on_outcome(self) -> None:
        if not self._verdict_touched:
            self._prefill_verdict()
        self._changed()

    def _on_verdict(self) -> None:
        self._verdict_touched = True
        self._changed()

    def _on_note_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self._note.edit_modified(False)
        self._changed()

    def _on_case_note_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self._case_note.edit_modified(False)
        self._changed()

    def _save(self) -> None:
        if self._open and str(self._save_button.cget("state")) == "normal":
            self._on_save()

    def _cancel(self) -> None:
        if self._open and str(self._cancel_button.cget("state")) == "normal":
            self._on_cancel()

    def _on_save_key(self, _event: "tk.Event[tk.Misc]") -> str | None:
        if not self._open:
            return None
        self._save()
        return "break"

    def _on_cancel_key(self, _event: "tk.Event[tk.Misc]") -> str | None:
        if not self._open:
            return None
        self._cancel()
        return "break"
