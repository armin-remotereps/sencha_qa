"""The person window's "Manual run" tab (MANUAL_RUN_V1).

Runs inside the window process (``person_window_app``) on the Tk thread. The
tester works through a Manual run here:

- Cases: the run's cases with their status; the next case to do is
  preselected; Start claims it (the server answers; the steps then arrive in
  the next snapshot).
- The claimed case: its title, preconditions and verbatim steps, one step at
  a time (Previous / Next and a step picker). Each step has Pass and Fail,
  which take a screenshot with this window hidden (the runner hides it,
  captures and shows it again), a thumbnail with Retake, and a note
  (required for a failed step). Ask the AI about the shown step.
- The case result: Passed, Failed or Blocked, a note (required for Blocked,
  and for Failed when no step failed), an optional case screenshot and Hand
  in. A verdict that contradicts the steps (Passed with a failed step,
  Failed with no failed step) asks for an inline confirmation first. Hand in
  is local and instant (the runner spools it); the next case is preselected.
- Release gives the case back (inline confirmation; its draft is dropped).
- The upload line says what still waits to be sent.

Marks (a step's outcome and screenshot) belong to the runner, which keeps
them in its spool; what the person types (notes, the verdict, the step
timer) belongs to this view and is sent to the runner shortly after every
change, so a restart of either loses nothing. Every text from the server is
plain Tk text. Like the rest of the window, nothing here opens a dialog.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final, Protocol

from controller_client.person_window_ipc import (
    ManualCaptureEvent,
    ManualCaseActionEvent,
    ManualDraftEvent,
    ManualDraftTexts,
    ManualMarkView,
    ManualResultKind,
    ManualSubmitEvent,
    ManualThumbnailRequestEvent,
    ManualWindowState,
    QuestionEvent,
    WindowEvent,
    next_seq,
)
from controller_client.protocol import (
    MANUAL_STEP_MAX_SECONDS,
    MANUAL_STEP_NOTE_MAX_CHARS,
    MANUAL_VERDICT_NOTE_MAX_CHARS,
    PERSON_STEP_QUESTION_MAX_CHARS,
    ManualActiveCasePayload,
    ManualCaseActionKind,
    ManualCaseActionPayload,
    ManualCaseListItemPayload,
    ManualCaseVerdictWire,
    ManualShotKind,
    ManualStepOutcome,
    ManualStepPayload,
    PersonStepQuestionAskPayload,
    PersonStepQuestionPayload,
    PersonStepQuestionWireStatus,
)

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# Same palette as person_window_app.
_WARN_COLOR: Final[str] = "#b45309"
_ERROR_COLOR: Final[str] = "#b91c1c"
_OK_COLOR: Final[str] = "#15803d"
_MUTED_COLOR: Final[str] = "#6b7280"
_CURRENT_BACKGROUND: Final[str] = "#fde68a"
_CURRENT_FOREGROUND: Final[str] = "#1f2937"
_HEADING_FONT: Final[str] = "PunkHazardHeading"
_BOLD_FONT: Final[str] = "PunkHazardBold"
# Notes and the verdict reach the runner this long after the last change.
_DRAFT_DELAY_MS: Final[int] = 800
_CASE_LIST_ROWS: Final[int] = 8
_STEP_LINES: Final[int] = 10
_OVERVIEW_LINES: Final[int] = 8
_THUMBNAIL_CACHE: Final[int] = 24
_ASK_CLOSED: Final[str] = "▸ Ask the AI about this step"
_ASK_OPEN: Final[str] = "▾ Ask the AI about this step"
_STATUS_LABELS: Final[dict[str, str]] = {
    "created": "To do",
    "in_progress": "In progress",
    "success": "Passed",
    "failed": "Failed",
    "blocked": "Blocked",
    "cancelled": "Cancelled",
    "needs_retest": "Needs re-test",
    "need_help": "Need help",
    "awaiting_person": "Your turn",
}
_VERDICT_LABELS: Final[dict[ManualCaseVerdictWire, str]] = {
    ManualCaseVerdictWire.PASSED: "Passed",
    ManualCaseVerdictWire.FAILED: "Failed",
    ManualCaseVerdictWire.BLOCKED: "Blocked",
}

_Segments = tuple[tuple[str, tuple[str, ...]], ...]


class ManualBlock(Protocol):
    """A read-only text block of the window (``_ReadOnlyBlock``)."""

    frame: "ttk.Frame"
    title: "ttk.Label"
    text: "tk.Text"

    def set(self, text: str) -> bool: ...

    def set_segments(self, segments: _Segments) -> bool: ...

    def show(self, visible: bool) -> None: ...


class ManualHost(Protocol):
    """What the view needs from the window (``PersonWindowApp``)."""

    def manual_block(
        self, parent: "ttk.Frame", *, title: str, max_lines: int
    ) -> ManualBlock: ...

    def manual_label(
        self, parent: "tk.Misc", text: str = "", foreground: str = ""
    ) -> "ttk.Label": ...

    def manual_entry(
        self, parent: "tk.Misc", *, row: int, height: int
    ) -> "tk.Text": ...

    def manual_send(self, event: WindowEvent) -> None: ...

    def manual_connected(self) -> bool: ...


@dataclass
class _Capture:
    seq: int
    kind: ManualShotKind
    step_num: int | None


def _text(widget: "tk.Text") -> str:
    return widget.get("1.0", "end-1c")


def _set_text(widget: "tk.Text", text: str) -> None:
    widget.delete("1.0", "end")
    widget.insert("1.0", text)


def _status_label(case: ManualCaseListItemPayload, submitted: bool) -> str:
    if submitted:
        return "Handed in, sending"
    return _STATUS_LABELS.get(case.status, case.status or "?")


def _case_line(case: ManualCaseListItemPayload, submitted: bool) -> str:
    ref = f"C{case.testrail_id} · " if case.testrail_id else ""
    who = f"  ({case.claimed_label})" if case.claimed_label and not submitted else ""
    return f"{_status_label(case, submitted)}  ·  {ref}{case.title}{who}"


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


class ManualView:
    """The Manual run tab; every method runs on the Tk thread."""

    def __init__(self, host: ManualHost, root: "tk.Tk", frame: "ttk.Frame") -> None:
        self._host = host
        self._root = root
        self.state: ManualWindowState | None = None
        self._selected_pivot: int | None = None
        # The claim whose texts are in the widgets.
        self._loaded_claim: str | None = None
        self._step: int | None = None
        self._notes: dict[int, str] = {}
        self._seconds: dict[int, float] = {}
        self._step_started: float | None = None
        self._marks: dict[int, ManualMarkView] = {}
        self._case_shot_key = ""
        self._thumbnails: OrderedDict[str, tk.PhotoImage] = OrderedDict()
        self._requested: set[str] = set()
        self._seq = 0
        self._capture: _Capture | None = None
        self._claim_busy = False
        self._release_busy = False
        self._submit_busy = False
        self._asking = False
        self._ask_open = False
        self._rendered_qa: str | None = None
        self._confirm: str | None = None
        self._draft_job: str | None = None
        self._verdict_var = tk.StringVar(master=root, value="")
        self._picker_var = tk.StringVar(master=root)
        self._build(frame)

    # --- layout -------------------------------------------------------------

    def _build(self, frame: "ttk.Frame") -> None:
        host = self._host
        self._upload = host.manual_label(frame, "", foreground=_MUTED_COLOR)
        self._upload.grid(row=0, column=0, sticky="w")

        cases = ttk.Frame(frame)
        cases.columnconfigure(0, weight=1)
        cases.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._cases_frame = cases
        ttk.Label(cases, text="Cases of this run", font=_BOLD_FONT).grid(
            row=0, column=0, sticky="w"
        )
        holder = ttk.Frame(cases)
        holder.columnconfigure(0, weight=1)
        holder.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._case_list = tk.Listbox(
            holder,
            height=_CASE_LIST_ROWS,
            activestyle="dotbox",
            exportselection=False,
            font="TkTextFont",
        )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self._case_list.yview)
        self._case_list.configure(yscrollcommand=scroll.set)
        self._case_list.grid(row=0, column=0, sticky="ew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._case_list.bind("<<ListboxSelect>>", self._on_case_select)
        start_row = ttk.Frame(cases)
        start_row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._start_button = ttk.Button(
            start_row, text="Start this case", command=self._start
        )
        self._start_button.pack(side="left")
        self._list_status = host.manual_label(cases, "")
        self._list_status.grid(row=3, column=0, sticky="w", pady=(4, 0))

        active = ttk.Frame(frame)
        active.columnconfigure(0, weight=1)
        active.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._active_frame = active
        head = ttk.Frame(active)
        head.columnconfigure(0, weight=1)
        head.grid(row=0, column=0, sticky="ew")
        self._case_title = host.manual_label(head, "")
        self._case_title.configure(font=_HEADING_FONT)
        self._case_title.grid(row=0, column=0, sticky="w")
        self._release_button = ttk.Button(
            head, text="Release case", command=self._ask_release
        )
        self._release_button.grid(row=0, column=1, sticky="ne", padx=(8, 0))
        self._preconditions = host.manual_block(
            active, title="Preconditions", max_lines=_STEP_LINES
        )
        self._preconditions.frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._overview = host.manual_block(
            active, title="Steps", max_lines=_OVERVIEW_LINES
        )
        self._overview.text.tag_configure(
            "current", background=_CURRENT_BACKGROUND, foreground=_CURRENT_FOREGROUND
        )
        self._overview.text.tag_configure("failed", foreground=_ERROR_COLOR)
        self._overview.text.tag_configure("passed", foreground=_OK_COLOR)
        self._overview.frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        nav = ttk.Frame(active)
        nav.columnconfigure(1, weight=1)
        nav.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._prev_button = ttk.Button(
            nav, text="◀ Previous", command=lambda: self._move(-1)
        )
        self._prev_button.grid(row=0, column=0, sticky="w")
        self._picker = ttk.Combobox(
            nav, state="readonly", textvariable=self._picker_var
        )
        self._picker.grid(row=0, column=1, sticky="ew", padx=6)
        self._picker.bind("<<ComboboxSelected>>", self._on_pick)
        self._next_button = ttk.Button(
            nav, text="Next ▶", command=lambda: self._move(1)
        )
        self._next_button.grid(row=0, column=2, sticky="e")

        self._step_text = host.manual_block(active, title="Step", max_lines=_STEP_LINES)
        self._step_text.frame.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self._expected = host.manual_block(
            active, title="Expected result", max_lines=_STEP_LINES
        )
        self._expected.frame.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self._step_status = host.manual_label(active, "")
        self._step_status.grid(row=6, column=0, sticky="w", pady=(6, 0))
        shot_row = ttk.Frame(active)
        shot_row.grid(row=7, column=0, sticky="w", pady=(4, 0))
        self._step_thumb = ttk.Label(shot_row)
        self._step_thumb.pack(side="left")
        self._retake_button = ttk.Button(
            shot_row, text="Retake", command=self._retake_step
        )
        self._retake_button.pack(side="left", padx=(8, 0), anchor="n")
        buttons = ttk.Frame(active)
        buttons.grid(row=8, column=0, sticky="w", pady=(6, 0))
        self._pass_button = ttk.Button(
            buttons,
            text="Pass (screenshot)",
            command=lambda: self._capture_step(ManualShotKind.PASS),
        )
        self._pass_button.pack(side="left")
        self._fail_button = ttk.Button(
            buttons,
            text="Fail (screenshot)",
            command=lambda: self._capture_step(ManualShotKind.FAIL),
        )
        self._fail_button.pack(side="left", padx=(6, 0))
        self._note_label = ttk.Label(active, text="")
        self._note_label.grid(row=9, column=0, sticky="w", pady=(6, 0))
        self._note = host.manual_entry(active, row=10, height=3)
        self._note.bind("<<Modified>>", self._on_note_modified)
        self._capture_status = host.manual_label(active, "")
        self._capture_status.grid(row=11, column=0, sticky="w", pady=(4, 0))
        self._build_ask(active, row=12)

        verdict = ttk.Frame(active)
        verdict.columnconfigure(0, weight=1)
        verdict.grid(row=13, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(verdict, text="Case result", font=_BOLD_FONT).grid(
            row=0, column=0, sticky="w"
        )
        choices = ttk.Frame(verdict)
        choices.grid(row=1, column=0, sticky="w", pady=(2, 0))
        for verdict_value, label in _VERDICT_LABELS.items():
            ttk.Radiobutton(
                choices,
                text=label,
                value=verdict_value.value,
                variable=self._verdict_var,
                command=self._on_verdict,
            ).pack(side="left", padx=(0, 10))
        self._verdict_note_label = ttk.Label(verdict, text="")
        self._verdict_note_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._verdict_note = host.manual_entry(verdict, row=3, height=3)
        self._verdict_note.bind("<<Modified>>", self._on_verdict_note_modified)
        case_shot = ttk.Frame(verdict)
        case_shot.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self._case_thumb = ttk.Label(case_shot)
        self._case_thumb.pack(side="left")
        self._case_shot_button = ttk.Button(
            case_shot,
            text="Take a case screenshot (optional)",
            command=self._capture_case,
        )
        self._case_shot_button.pack(side="left", padx=(8, 0), anchor="n")
        submit_row = ttk.Frame(verdict)
        submit_row.grid(row=5, column=0, sticky="w", pady=(10, 0))
        self._submit_button = ttk.Button(
            submit_row, text="Hand in", command=self._ask_submit
        )
        self._submit_button.pack(side="left")
        self._submit_hint = host.manual_label(
            verdict,
            "Steps you did not mark are reported as not run. Screenshots upload "
            "after you hand in.",
            foreground=_MUTED_COLOR,
        )
        self._submit_hint.grid(row=6, column=0, sticky="w", pady=(4, 0))

        # Inline confirmation (release or a contradicting hand-in).
        self._confirm_frame = ttk.Frame(active)
        self._confirm_frame.columnconfigure(0, weight=1)
        self._confirm_label = host.manual_label(
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
        self._confirm_frame.grid(row=14, column=0, sticky="ew", pady=(10, 0))
        self._confirm_frame.grid_remove()
        self._submit_status = host.manual_label(active, "")
        self._submit_status.grid(row=15, column=0, sticky="w", pady=(4, 0))

    def _build_ask(self, parent: "ttk.Frame", *, row: int) -> None:
        ask = ttk.Frame(parent)
        ask.columnconfigure(0, weight=1)
        ask.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        self._ask_frame = ask
        self._ask_toggle = ttk.Button(ask, text=_ASK_CLOSED, command=self._toggle_ask)
        self._ask_toggle.grid(row=0, column=0, sticky="w")
        content = ttk.Frame(ask)
        content.columnconfigure(0, weight=1)
        content.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._ask_content = content
        self._host.manual_label(
            content,
            "The AI only advises: it does not act on this machine or decide a "
            "result for you.",
            foreground=_MUTED_COLOR,
        ).grid(row=0, column=0, sticky="w")
        self._qa = self._host.manual_block(
            content, title="Questions and answers (this case)", max_lines=12
        )
        self._qa.frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(content, text="Your question").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        self._question = self._host.manual_entry(content, row=3, height=3)
        self._question.bind("<<Modified>>", self._on_question_modified)
        ask_row = ttk.Frame(content)
        ask_row.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self._ask_button = ttk.Button(ask_row, text="Ask", command=self._ask)
        self._ask_button.pack(side="left")
        self._ask_status = self._host.manual_label(content, "")
        self._ask_status.grid(row=5, column=0, sticky="w", pady=(4, 0))
        content.grid_remove()

    # --- what the window asks -----------------------------------------------

    @property
    def has_run(self) -> bool:
        return self.state is not None and self.state.run is not None

    @property
    def run_id(self) -> int | None:
        state = self.state
        return state.run.run_id if state is not None and state.run else None

    def header(self) -> tuple[str, str]:
        """The window header's title and subtitle while only this tab shows."""
        state = self.state
        if state is None or state.run is None:
            return "", ""
        if state.active is not None:
            return state.active.title, f"Manual run · {state.run.name}"
        return "Manual run", state.run.name

    # --- commands from the runner ---------------------------------------------

    def apply_state(self, state: ManualWindowState) -> None:
        self._save_note()
        self.state = state
        active = state.active
        draft = state.draft
        if (
            draft is not None
            and active is not None
            and draft.claim_token == active.claim_token
        ):
            self._marks = {mark.step_num: mark for mark in draft.marks}
            self._case_shot_key = draft.case_shot_key
        else:
            self._marks = {}
            self._case_shot_key = ""
        claim = active.claim_token if active is not None else None
        if claim != self._loaded_claim:
            self._load_claim(
                active, draft.texts if draft is not None and claim else None
            )
        if self._claim_busy and active is not None:
            self._claim_busy = False
        self.render()

    def _load_claim(
        self, active: ManualActiveCasePayload | None, texts: ManualDraftTexts | None
    ) -> None:
        """A newly claimed (or no longer claimed) case: fresh widgets."""
        self._flush_draft()
        self._loaded_claim = active.claim_token if active is not None else None
        self._confirm = None
        self._capture = None
        self._asking = False
        self._rendered_qa = None
        self._thumbnails.clear()
        self._requested.clear()
        self._notes = dict(texts.notes) if texts is not None else {}
        self._seconds = (
            {num: float(value) for num, value in texts.seconds.items()}
            if texts is not None
            else {}
        )
        verdict = texts.verdict if texts is not None else None
        self._verdict_var.set(verdict.value if verdict is not None else "")
        _set_text(self._verdict_note, texts.verdict_note if texts is not None else "")
        _set_text(self._question, "")
        self._capture_status.configure(text="")
        self._submit_status.configure(text="")
        self._ask_status.configure(text="")
        if active is None:
            # Handed in or released: preselect the next case to do again.
            self._selected_pivot = None
        if active is None or not active.steps:
            self._step = None
        else:
            unmarked = [s.num for s in active.steps if s.num not in self._marks]
            self._step = unmarked[0] if unmarked else active.steps[0].num
        self._step_started = time.monotonic() if self._step is not None else None
        _set_text(self._note, self._notes.get(self._step, "") if self._step else "")
        self._note.edit_modified(False)

    def on_capture_result(
        self, seq: int, ok: bool, message: str, key: str, thumbnail_b64: str
    ) -> None:
        capture = self._capture
        if capture is None or capture.seq != seq:
            return
        self._capture = None
        if ok:
            self._store_thumbnail(key, thumbnail_b64)
            self._capture_status.configure(text="")
            if capture.kind is ManualShotKind.FAIL:
                self._capture_status.configure(
                    text="Describe in the note what went wrong.",
                    foreground=_WARN_COLOR,
                )
                self._note.focus_set()
        else:
            self._capture_status.configure(text=message, foreground=_ERROR_COLOR)
        self.render()

    def on_thumbnail(self, key: str, thumbnail_b64: str) -> None:
        if thumbnail_b64:
            self._store_thumbnail(key, thumbnail_b64)
            self.render()

    def on_result(self, kind: ManualResultKind, ok: bool, message: str) -> None:
        color = _OK_COLOR if ok else _ERROR_COLOR
        if kind is ManualResultKind.CLAIM:
            if not self._claim_busy:
                return
            if not ok:
                self._claim_busy = False
            self._list_status.configure(text="" if ok else message, foreground=color)
        elif kind is ManualResultKind.RELEASE:
            if not self._release_busy:
                return
            self._release_busy = False
            target = self._list_status if ok else self._submit_status
            target.configure(
                text=message or ("Released." if ok else ""), foreground=color
            )
        elif kind is ManualResultKind.SUBMIT:
            if not self._submit_busy:
                return
            self._submit_busy = False
            if ok:
                self._list_status.configure(text=message, foreground=color)
            else:
                self._submit_status.configure(text=message, foreground=color)
        else:
            # A capture notice the window missed while it restarted.
            self._capture = None
            self._capture_status.configure(text=message, foreground=color)
        self.render()

    def on_question_result(self, ok: bool, message: str) -> None:
        if not self._asking:
            return
        self._asking = False
        if ok:
            _set_text(self._question, "")
            self._ask_status.configure(
                text=message or "Asked. The answer shows above when it is ready.",
                foreground=_OK_COLOR,
            )
        else:
            self._ask_status.configure(text=message, foreground=_ERROR_COLOR)
        self.render()

    # --- rendering --------------------------------------------------------------

    def render(self) -> None:
        state = self.state
        if state is None or state.run is None:
            return
        self._render_upload(state)
        self._render_cases(state)
        active = state.active
        if active is None:
            self._active_frame.grid_remove()
            return
        self._active_frame.grid()
        self._render_active(active)

    def _render_upload(self, state: ManualWindowState) -> None:
        upload = state.upload
        parts: list[str] = []
        connected = self._host.manual_connected()
        if upload.waiting_cases and not connected:
            parts.append(
                f"Offline: {upload.waiting_cases} handed-in case(s) waiting to upload."
            )
        elif upload.unsent_cases:
            parts.append(f"Sending {upload.unsent_cases} result(s)…")
        elif upload.waiting_shots:
            verb = "Uploading" if upload.sending else "Waiting to upload"
            parts.append(f"{verb} {upload.waiting_shots} screenshot(s)…")
        if state.spool_full:
            parts.append(
                "The screenshot spool is full: no new screenshots until the "
                "uploads catch up."
            )
        if upload.message:
            parts.append(upload.message)
        warn = state.spool_full or bool(upload.message)
        self._upload.configure(
            text="\n".join(parts), foreground=_WARN_COLOR if warn else _MUTED_COLOR
        )

    def _render_cases(self, state: ManualWindowState) -> None:
        submitted = set(state.submitted_pivots)
        cases = state.cases
        ids = [case.pivot_id for case in cases]
        if self._selected_pivot not in ids:
            self._selected_pivot = next(
                (
                    case.pivot_id
                    for case in cases
                    if case.status == "created" and case.pivot_id not in submitted
                ),
                ids[0] if ids else None,
            )
        lines = [_case_line(case, case.pivot_id in submitted) for case in cases]
        if list(self._case_list.get(0, "end")) != lines:
            top = self._case_list.yview()[0]
            self._case_list.delete(0, "end")
            for line in lines:
                self._case_list.insert("end", line)
            self._case_list.yview_moveto(top)
        self._case_list.selection_clear(0, "end")
        if self._selected_pivot is not None:
            index = ids.index(self._selected_pivot)
            self._case_list.selection_set(index)
            self._case_list.see(index)
        selected = next((c for c in cases if c.pivot_id == self._selected_pivot), None)
        can_start = (
            self._host.manual_connected()
            and not self._claim_busy
            and state.active is None
            and selected is not None
            and selected.status == "created"
            and not selected.claimed_label
            and selected.pivot_id not in submitted
        )
        self._start_button.configure(
            text="Starting…" if self._claim_busy else "Start this case",
            state="normal" if can_start else "disabled",
        )
        if not cases:
            self._list_status.configure(
                text="This run has no cases.", foreground=_MUTED_COLOR
            )

    def _active_step(self, active: ManualActiveCasePayload) -> ManualStepPayload | None:
        return next((s for s in active.steps if s.num == self._step), None)

    def _render_active(self, active: ManualActiveCasePayload) -> None:
        self._case_title.configure(text=active.title)
        if active.preconditions:
            self._preconditions.set(active.preconditions)
            self._preconditions.show(True)
        else:
            self._preconditions.show(False)
        self._render_overview(active)
        step = self._active_step(active)
        total = len(active.steps)
        nums = [s.num for s in active.steps]
        index = nums.index(step.num) if step is not None else -1
        self._picker.configure(
            values=[self._pick_label(s, total) for s in active.steps]
        )
        if step is not None:
            self._picker_var.set(self._pick_label(step, total))
        self._prev_button.configure(state="normal" if index > 0 else "disabled")
        self._next_button.configure(
            state="normal" if 0 <= index < total - 1 else "disabled"
        )
        if step is None:
            self._step_text.set("This case has no steps.")
            self._expected.show(False)
        else:
            self._step_text.title.configure(text=f"Step {step.num} of {total}")
            self._step_text.set(step.text)
            self._expected.set(step.expected or "(none given)")
            self._expected.show(True)
        self._render_step_mark(step)
        self._render_ask(active)
        self._render_verdict()
        self._render_confirm()

    @staticmethod
    def _pick_label(step: ManualStepPayload, total: int) -> str:
        first_line = step.text.strip().splitlines()[0] if step.text.strip() else ""
        if len(first_line) > 60:
            first_line = first_line[:59] + "…"
        return f"Step {step.num} of {total} · {first_line}"

    def _render_overview(self, active: ManualActiveCasePayload) -> None:
        segments: list[tuple[str, tuple[str, ...]]] = []
        for i, step in enumerate(active.steps):
            mark = self._marks.get(step.num)
            tags: tuple[str, ...]
            if mark is None:
                symbol, tags = "·", ()
            elif mark.outcome is ManualStepOutcome.FAIL:
                symbol, tags = "✗", ("failed",)
            else:
                symbol, tags = "✓", ("passed",)
            if step.num == self._step:
                tags = tags + ("current",)
            first_line = step.text.strip().splitlines()[0] if step.text.strip() else ""
            gap = "\n" if i else ""
            segments.append((f"{gap}{symbol} {step.num}. {first_line}", tags))
        self._overview.set_segments(tuple(segments))

    def _render_step_mark(self, step: ManualStepPayload | None) -> None:
        busy = self._capture is not None
        can_capture = (
            step is not None
            and not busy
            and not self._submit_busy
            and self.state is not None
            and not self.state.spool_full
        )
        mark = self._marks.get(step.num) if step is not None else None
        if mark is None:
            self._step_status.configure(text="Not marked yet", foreground=_MUTED_COLOR)
            self._note_label.configure(text="Note (optional)")
            self._retake_button.pack_forget()
            self._step_thumb.configure(image="")
        else:
            failed = mark.outcome is ManualStepOutcome.FAIL
            self._step_status.configure(
                text="Failed" if failed else "Passed",
                foreground=_ERROR_COLOR if failed else _OK_COLOR,
            )
            self._note_label.configure(
                text="What went wrong? (required)" if failed else "Note (optional)"
            )
            self._show_thumbnail(self._step_thumb, mark.key)
            self._retake_button.pack(side="left", padx=(8, 0), anchor="n")
            self._retake_button.configure(state="normal" if can_capture else "disabled")
        for button in (self._pass_button, self._fail_button):
            button.configure(state="normal" if can_capture else "disabled")
        if busy and self._capture is not None and self._capture.step_num is not None:
            self._capture_status.configure(
                text="Taking the screenshot…", foreground=_MUTED_COLOR
            )

    def _render_verdict(self) -> None:
        verdict = self._verdict()
        failed_steps = self._failed_steps()
        if verdict is ManualCaseVerdictWire.BLOCKED:
            label = "Why is the case blocked? (required)"
        elif verdict is ManualCaseVerdictWire.FAILED and not failed_steps:
            label = "Why did the case fail? (required: no step failed)"
        else:
            label = "Note for the case (optional)"
        self._verdict_note_label.configure(text=label)
        busy = self._capture is not None or self._submit_busy
        full = self.state is not None and self.state.spool_full
        if self._case_shot_key:
            self._show_thumbnail(self._case_thumb, self._case_shot_key)
            self._case_shot_button.configure(text="Retake the case screenshot")
        else:
            self._case_thumb.configure(image="")
            self._case_shot_button.configure(text="Take a case screenshot (optional)")
        self._case_shot_button.configure(state="disabled" if busy or full else "normal")
        self._submit_button.configure(
            text="Handing in…" if self._submit_busy else "Hand in",
            state="disabled" if busy or verdict is None else "normal",
        )
        self._release_button.configure(
            state=(
                "normal"
                if self._host.manual_connected()
                and not self._release_busy
                and not self._submit_busy
                else "disabled"
            )
        )

    def _render_confirm(self) -> None:
        if self._confirm is None:
            self._confirm_frame.grid_remove()
            return
        if self._confirm == "release":
            self._confirm_label.configure(
                text="Release this case? It goes back to the list and its marks "
                "and screenshots on this machine are discarded."
            )
            self._confirm_button.configure(text="Release")
        else:
            verdict = self._verdict()
            failed = self._failed_steps()
            if verdict is ManualCaseVerdictWire.PASSED:
                steps = ", ".join(str(num) for num in failed)
                text = (
                    f"Step(s) {steps} failed, but you chose Passed for the case. "
                    "Hand it in as Passed?"
                )
            else:
                text = "No step failed, but you chose Failed for the case. Hand it in as Failed?"
            self._confirm_label.configure(text=text)
            self._confirm_button.configure(text="Hand in anyway")
        self._confirm_frame.grid()

    def _render_ask(self, active: ManualActiveCasePayload) -> None:
        count = f" ({len(active.questions)})" if active.questions else ""
        self._ask_toggle.configure(
            text=(_ASK_OPEN if self._ask_open else _ASK_CLOSED) + count
        )
        if not self._ask_open:
            self._ask_content.grid_remove()
            return
        self._ask_content.grid()
        qa = _qa_text(active.questions) or "No questions yet for this case."
        if qa != self._rendered_qa:
            self._rendered_qa = qa
            self._qa.set(qa)
            self._qa.text.see("end")
        self._update_ask_button(active)

    def _update_ask_button(self, active: ManualActiveCasePayload) -> None:
        pending = any(
            q.status is PersonStepQuestionWireStatus.PENDING for q in active.questions
        )
        can_ask = (
            active.can_ask
            and self._host.manual_connected()
            and not self._asking
            and not pending
            and bool(_text(self._question).strip())
        )
        self._ask_button.configure(state="normal" if can_ask else "disabled")
        if not active.can_ask:
            self._ask_status.configure(
                text="Asking the AI is not available for this case right now.",
                foreground=_MUTED_COLOR,
            )
        elif self._asking:
            self._ask_status.configure(
                text="Sending your question…", foreground=_MUTED_COLOR
            )

    # --- thumbnails ---------------------------------------------------------------

    def _store_thumbnail(self, key: str, thumbnail_b64: str) -> None:
        if not key or not thumbnail_b64:
            return
        try:
            image = tk.PhotoImage(master=self._root, data=thumbnail_b64)
        except tk.TclError:
            return
        self._thumbnails[key] = image
        self._thumbnails.move_to_end(key)
        while len(self._thumbnails) > _THUMBNAIL_CACHE:
            self._thumbnails.popitem(last=False)

    def _show_thumbnail(self, label: "ttk.Label", key: str) -> None:
        image = self._thumbnails.get(key)
        if image is not None:
            label.configure(image=image)
            return
        label.configure(image="")
        if key not in self._requested:
            self._requested.add(key)
            self._host.manual_send(ManualThumbnailRequestEvent(key=key))

    # --- texts ------------------------------------------------------------------

    def _verdict(self) -> ManualCaseVerdictWire | None:
        try:
            return ManualCaseVerdictWire(self._verdict_var.get())
        except ValueError:
            return None

    def _failed_steps(self) -> list[int]:
        return sorted(
            num
            for num, mark in self._marks.items()
            if mark.outcome is ManualStepOutcome.FAIL
        )

    def _save_note(self) -> None:
        if self._step is not None and self._loaded_claim is not None:
            self._notes[self._step] = _text(self._note)

    def _texts(self) -> ManualDraftTexts:
        self._save_note()
        seconds = dict(self._seconds)
        if self._step is not None and self._step_started is not None:
            seconds[self._step] = seconds.get(self._step, 0.0) + (
                time.monotonic() - self._step_started
            )
        return ManualDraftTexts(
            notes={num: note for num, note in self._notes.items() if note},
            seconds={
                num: min(int(value), MANUAL_STEP_MAX_SECONDS)
                for num, value in seconds.items()
                if value >= 1
            },
            verdict=self._verdict(),
            verdict_note=_text(self._verdict_note),
        )

    def _schedule_draft(self) -> None:
        if self._draft_job is not None:
            self._root.after_cancel(self._draft_job)
        self._draft_job = self._root.after(_DRAFT_DELAY_MS, self._flush_draft)

    def _flush_draft(self) -> None:
        if self._draft_job is not None:
            self._root.after_cancel(self._draft_job)
            self._draft_job = None
        active = self.state.active if self.state is not None else None
        if active is None or active.claim_token != self._loaded_claim:
            return
        self._host.manual_send(
            ManualDraftEvent(
                pivot_id=active.pivot_id,
                claim_token=active.claim_token,
                texts=self._texts(),
            )
        )

    def _on_note_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self._note.edit_modified(False)
        self._schedule_draft()

    def _on_verdict_note_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self._verdict_note.edit_modified(False)
        self._schedule_draft()

    def _on_verdict(self) -> None:
        self._confirm = None
        self._schedule_draft()
        self.render()

    def _on_question_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self._question.edit_modified(False)
        active = self.state.active if self.state is not None else None
        if active is not None and self._ask_open:
            self._update_ask_button(active)

    # --- person actions -----------------------------------------------------------

    def _on_case_select(self, _event: "tk.Event[tk.Listbox]") -> None:
        state = self.state
        if state is None:
            return
        # (curselection is untyped in typeshed.)
        index = next(
            (
                i
                for i in range(len(state.cases))
                if self._case_list.selection_includes(i)
            ),
            -1,
        )
        if 0 <= index < len(state.cases):
            self._selected_pivot = state.cases[index].pivot_id
            self._list_status.configure(text="")
            self.render()

    def _start(self) -> None:
        state = self.state
        if state is None or state.active is not None or self._selected_pivot is None:
            return
        if self._claim_busy or not self._host.manual_connected():
            return
        self._claim_busy = True
        self._list_status.configure(text="")
        self.render()
        self._host.manual_send(
            ManualCaseActionEvent(
                action=ManualCaseActionPayload(
                    action=ManualCaseActionKind.CLAIM,
                    pivot_id=self._selected_pivot,
                    claim_token="",
                )
            )
        )

    def _go_to(self, num: int) -> None:
        if num == self._step:
            return
        self._save_note()
        now = time.monotonic()
        if self._step is not None and self._step_started is not None:
            self._seconds[self._step] = self._seconds.get(self._step, 0.0) + (
                now - self._step_started
            )
        self._step = num
        self._step_started = now
        _set_text(self._note, self._notes.get(num, ""))
        self._note.edit_modified(False)
        self._capture_status.configure(text="")
        self._schedule_draft()
        self.render()

    def _move(self, delta: int) -> None:
        active = self.state.active if self.state is not None else None
        if active is None or self._step is None:
            return
        nums = [s.num for s in active.steps]
        index = nums.index(self._step) + delta if self._step in nums else 0
        if 0 <= index < len(nums):
            self._go_to(nums[index])

    def _on_pick(self, _event: "tk.Event[ttk.Combobox]") -> None:
        active = self.state.active if self.state is not None else None
        index = self._picker.current()
        if active is not None and 0 <= index < len(active.steps):
            self._go_to(active.steps[index].num)

    def _send_capture(self, kind: ManualShotKind, step_num: int | None) -> None:
        active = self.state.active if self.state is not None else None
        if active is None or self._capture is not None:
            return
        self._seq = next_seq(self._seq)
        self._capture = _Capture(seq=self._seq, kind=kind, step_num=step_num)
        self._confirm = None
        self._capture_status.configure(
            text="Taking the screenshot…", foreground=_MUTED_COLOR
        )
        self.render()
        self._host.manual_send(
            ManualCaptureEvent(
                seq=self._seq,
                pivot_id=active.pivot_id,
                claim_token=active.claim_token,
                kind=kind,
                step_num=step_num,
            )
        )

    def _capture_step(self, kind: ManualShotKind) -> None:
        if self._step is not None:
            self._send_capture(kind, self._step)

    def _retake_step(self) -> None:
        mark = self._marks.get(self._step) if self._step is not None else None
        if mark is not None:
            kind = (
                ManualShotKind.FAIL
                if mark.outcome is ManualStepOutcome.FAIL
                else ManualShotKind.PASS
            )
            self._capture_step(kind)

    def _capture_case(self) -> None:
        self._send_capture(ManualShotKind.CASE, None)

    def _ask_release(self) -> None:
        if self._release_busy or not self._host.manual_connected():
            return
        self._confirm = "release"
        self.render()

    def _validation_error(self, texts: ManualDraftTexts) -> tuple[str, int | None]:
        """Why the hand-in cannot go yet (and the step to show), or ("", None)."""
        verdict = texts.verdict
        if verdict is None:
            return "Choose Passed, Failed or Blocked for the case.", None
        for num in self._failed_steps():
            note = texts.notes.get(num, "").strip()
            if not note:
                return f"Step {num} failed: describe in its note what went wrong.", num
        for num, note in texts.notes.items():
            if len(note.strip()) > MANUAL_STEP_NOTE_MAX_CHARS:
                return (
                    f"The note of step {num} is too long ({len(note.strip())} of "
                    f"{MANUAL_STEP_NOTE_MAX_CHARS} characters).",
                    num,
                )
        verdict_note = texts.verdict_note.strip()
        if len(verdict_note) > MANUAL_VERDICT_NOTE_MAX_CHARS:
            return (
                f"The case note is too long ({len(verdict_note)} of "
                f"{MANUAL_VERDICT_NOTE_MAX_CHARS} characters).",
                None,
            )
        if not verdict_note:
            if verdict is ManualCaseVerdictWire.BLOCKED:
                return "Say in the case note why the case is blocked.", None
            if verdict is ManualCaseVerdictWire.FAILED and not self._failed_steps():
                return "No step failed: say in the case note why the case failed.", None
        return "", None

    def _ask_submit(self) -> None:
        if self._submit_busy or self._capture is not None:
            return
        texts = self._texts()
        message, step = self._validation_error(texts)
        if message:
            self._submit_status.configure(text=message, foreground=_ERROR_COLOR)
            if step is not None:
                self._go_to(step)
                self._note.focus_set()
            self.render()
            return
        self._submit_status.configure(text="")
        failed = self._failed_steps()
        verdict = texts.verdict
        contradicts = (verdict is ManualCaseVerdictWire.PASSED and bool(failed)) or (
            verdict is ManualCaseVerdictWire.FAILED and not failed
        )
        if contradicts:
            self._confirm = "submit"
            self.render()
            return
        self._submit(texts)

    def _cancel_confirm(self) -> None:
        self._confirm = None
        self.render()

    def _confirm_decision(self) -> None:
        confirm = self._confirm
        self._confirm = None
        active = self.state.active if self.state is not None else None
        if active is None:
            self.render()
            return
        if confirm == "release":
            self._release_busy = True
            self._submit_status.configure(text="")
            self.render()
            self._host.manual_send(
                ManualCaseActionEvent(
                    action=ManualCaseActionPayload(
                        action=ManualCaseActionKind.RELEASE,
                        pivot_id=active.pivot_id,
                        claim_token=active.claim_token,
                    )
                )
            )
        elif confirm == "submit":
            texts = self._texts()
            if self._validation_error(texts)[0]:
                self.render()
                return
            self._submit(texts)

    def _submit(self, texts: ManualDraftTexts) -> None:
        active = self.state.active if self.state is not None else None
        if active is None:
            return
        if self._draft_job is not None:
            self._root.after_cancel(self._draft_job)
            self._draft_job = None
        self._submit_busy = True
        self.render()
        self._host.manual_send(
            ManualSubmitEvent(
                pivot_id=active.pivot_id, claim_token=active.claim_token, texts=texts
            )
        )

    def _toggle_ask(self) -> None:
        self._ask_open = not self._ask_open
        self._rendered_qa = None
        self.render()

    def _ask(self) -> None:
        active = self.state.active if self.state is not None else None
        if active is None or self._asking or not self._host.manual_connected():
            return
        if not active.can_ask:
            return
        if any(
            q.status is PersonStepQuestionWireStatus.PENDING for q in active.questions
        ):
            self._ask_status.configure(
                text="Wait for the AI to answer your last question.",
                foreground=_WARN_COLOR,
            )
            return
        question = _text(self._question).strip()
        if not question:
            return
        if len(question) > PERSON_STEP_QUESTION_MAX_CHARS:
            self._ask_status.configure(
                text=f"The question is too long ({len(question)} of "
                f"{PERSON_STEP_QUESTION_MAX_CHARS} characters).",
                foreground=_ERROR_COLOR,
            )
            return
        self._asking = True
        self.render()
        # The window stays up: the tester keeps working while the AI answers.
        self._host.manual_send(
            QuestionEvent(
                question=PersonStepQuestionAskPayload(
                    person_step_id=0,
                    round=0,
                    question=question,
                    pivot_id=active.pivot_id,
                    claim_token=active.claim_token,
                    step_num=self._step,
                )
            )
        )
