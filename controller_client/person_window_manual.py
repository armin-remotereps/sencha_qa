"""The person window's "Manual run" tab (MANUAL_RUN_V1).

Runs inside the window process (``person_window_app``) on the Tk thread. The
tab is a fixed case bar over sub-tabs. The case bar never scrolls, so what the
tester uses all the time is reachable from every sub-tab:

- the upload line (what still waits to be sent);
- while a case is claimed: its title with Pause / Continue testing and
  Release case, the paused banner, then ◀ Previous, the step picker, Next ▶,
  Pass…, Fail… and Hand in…, and one status area (the screenshot, the
  hand-in and the inline confirmation of a release or a contradicting
  hand-in).

The sub-tabs (each its own scroll page; hidden rather than disabled):

- Cases: the run's cases with their status; the next case to do is
  preselected; Start claims it (the server answers; the steps then arrive in
  the next snapshot). The only sub-tab while no case is claimed.
- Preconditions & steps: the claimed case's preconditions and its steps with
  the shown step highlighted and the Pass / Fail marks so far.
- Current step: the shown step's text and expected result, its mark, the
  step's thumbnails with Edit…, the shortcut line and the step's note
  (required for a failed step).
- Discuss (MANUAL_PAUSE_DISCUSS_V1, only when the server sends it): the
  case's discussion with the agent (``person_window_chat``), a message box
  and "Open on Punk Hazard". Its title carries the message count and "…"
  while the agent answers.
- Result: Passed, Failed or Blocked, a note (required for Blocked, and for
  Failed when no step failed) and an optional case screenshot.

A new claim selects Current step, the end of a claim selects Cases, and a
hand-in that cannot go selects the sub-tab where the tester fixes it (the
failing step's note, else Result). Otherwise the tester's choice stays.

Pass… and Fail… take a screenshot with this window hidden (the runner hides
it, captures and shows it again) and then open the mark sheet
(``person_window_mark_sheet``), placed over the whole window whatever sub-tab
shows. Pass / Fail shortcuts (Test Runner 0.17.0): the window's
``ManualHotkeys`` (system-wide, else only while the window is focused,
through ``bind_all``) call ``on_shortcut``, which does what Pass… / Fail… do
for the shown step. The sheet's session
(``person_window_mark_session.MarkSession``): Save records the mark (its
screenshots were staged by the runner until then) and moves to the next step
left to mark; on the last one (or "Stop here" on a Fail) it also hands the
case in, and ``AutoClaim`` claims the next case to do once the snapshot shows
the hand-in landed. Cancel throws the staged screenshots away and leaves the
step as it was. Focus goes back to the app the tester was in when a
system-wide shortcut opened the sheet (not when this window was in front). A
Save or hand-in the runner never answers is given up after 30 s.

Hand in… is local and instant (the runner spools it); a verdict that
contradicts the steps (Passed with a failed step, Failed with no failed step)
asks for an inline confirmation first. Release gives the case back (inline
confirmation; its draft is dropped). While the case is paused
(MANUAL_PAUSE_DISCUSS_V1) the banner says since when and by whom, and Pass,
Fail, Edit, the shortcuts, the case screenshot and Hand in are off (the step
timer stops too); Release and Discuss stay on. Pause and Discuss show only
when the server sends their fields, so an older server leaves them out.

Marks (a step's outcome and screenshot) belong to the runner, which keeps
them in its spool; what the person types (notes, the verdict, the step
timer) belongs to this view and is sent to the runner shortly after every
change, so a restart of either loses nothing. Every text from the server is
plain Tk text. Like the rest of the window, nothing here opens a dialog.
"""

import sys
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from controller_client.manual_hotkeys_base import (
    OFF_STATUS,
    HotkeyKind,
    HotkeyState,
    HotkeyStatus,
)
from controller_client.person_window_chat import ChatHost, ChatPanel, clock
from controller_client.person_window_ipc import (
    ManualCaptureEvent,
    ManualCaseActionEvent,
    ManualChatSendEvent,
    ManualDraftEvent,
    ManualDraftTexts,
    ManualMarkView,
    ManualResultKind,
    ManualSubmitEvent,
    ManualThumbnailRequestEvent,
    ManualWindowState,
    WindowEvent,
    manual_pause_discuss_supported,
    next_seq,
)
from controller_client.person_window_mark_session import (
    AutoClaim,
    MarkSession,
    SessionWindow,
)
from controller_client.person_window_mark_sheet import HalfThumbnails
from controller_client.person_window_scroll import HEADER_ALLOWANCE, SectionPage
from controller_client.person_window_theme import (
    BOLD_FONT,
    ERROR,
    HEADING_FONT,
    HIGHLIGHT_BG,
    MUTED,
    OK,
    TEXT,
    WARN,
)
from controller_client.protocol import (
    MANUAL_STEP_MAX_SECONDS,
    MANUAL_STEP_MAX_SHOTS,
    MANUAL_STEP_NOTE_MAX_CHARS,
    MANUAL_VERDICT_NOTE_MAX_CHARS,
    PERSON_CHAT_TEXT_MAX_CHARS,
    ManualActiveCasePayload,
    ManualCaseActionKind,
    ManualCaseActionPayload,
    ManualCaseListItemPayload,
    ManualCaseVerdictWire,
    ManualChatSendPayload,
    ManualDiscussionPayload,
    ManualShotKind,
    ManualStepOutcome,
    ManualStepPayload,
)

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# Notes and the verdict reach the runner this long after the last change.
_DRAFT_DELAY_MS: Final[int] = 800
_CASE_LIST_ROWS: Final[int] = 8
_STEP_LINES: Final[int] = 10
_OVERVIEW_LINES: Final[int] = 20
_THUMBNAIL_CACHE: Final[int] = 24
# The sub-notebook's tab row, left above a sub-page besides the header
# (HEADER_ALLOWANCE) and the case bar.
_SUBTAB_ALLOWANCE: Final[int] = 34
_DISCUSS_TITLE: Final[str] = "Discuss"
_CHAT_LINES: Final[int] = 12
# Half-size step thumbnails per row when a step has several screenshots.
_STEP_SHOT_COLUMNS: Final[int] = 2
# A hand-in the runner never answers (it dropped the request) is given up
# after this long, so the tester can hand in again.
_SUBMIT_TIMEOUT_MS: Final[int] = 30_000
# The focus-only shortcuts (the window's own key bindings) and the Command
# key of macOS.
_SHORTCUT_MODIFIER: Final[str] = "Command" if sys.platform == "darwin" else "Control"
_SHORTCUT_LETTERS: Final[dict[HotkeyKind, str]] = {
    HotkeyKind.PASS: "p",
    HotkeyKind.FAIL: "f",
}
# The server refuses Continue while a discussion turn is live (its
# MANUAL_PAUSE_AGENT_BUSY); the window says so instead of sending.
_AGENT_BUSY: Final[str] = (
    "The agent is working on the machine. Wait for it to finish or stop it first."
)
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


class ManualHost(ChatHost, SessionWindow, Protocol):
    """What the view needs from the window (``PersonWindowApp``); the mark
    sheet's part is ``SessionWindow``."""

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

    def manual_open_url(self, url: str) -> None: ...

    def manual_hidden(self) -> bool:
        """The runner hid the window (a capture or an agent action)."""
        ...


@dataclass
class _Capture:
    seq: int
    kind: ManualShotKind
    step_num: int | None
    claim_token: str
    # A mark sheet's screenshot (staged until Save).
    staged: bool = False
    # Opens the sheet when it succeeds (Pass… / Fail… / a shortcut).
    opening: bool = False
    # Retake in the sheet: the screenshot it replaces.
    replace: str | None = None


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


def _agent_busy(active: ManualActiveCasePayload) -> bool:
    """A turn of the case's discussion is live: the agent may be on the machine."""
    return active.discussion is not None and active.discussion.turn_live


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
        self._half = HalfThumbnails()
        self._requested: set[str] = set()
        self._seq = 0
        self._capture: _Capture | None = None
        self._claim_busy = False
        self._release_busy = False
        self._submit_busy = False
        # Pause / Continue and a discussion message with the server.
        self._pause_busy = False
        self._chat_busy = False
        # Pause applies to the claimed case (the server sends its fields).
        self._pause_shown = False
        self._confirm: str | None = None
        self._draft_job: str | None = None
        # Gives up a hand-in the runner never answers.
        self._submit_job: str | None = None
        self._shortcut_status = OFF_STATUS
        self._focus_keys_bound = False
        self._verdict_var = tk.StringVar(master=root, value="")
        self._picker_var = tk.StringVar(master=root)
        self._build(frame)
        # The open mark sheet, and the next case after a hand-in from it.
        self._auto_claim = AutoClaim(self, root)
        self._marking = MarkSession(
            host,
            self,
            root,
            thumbnail=self._half_thumbnail,
            auto_claim=self._auto_claim,
        )

    # --- layout -------------------------------------------------------------

    def _build(self, frame: "ttk.Frame") -> None:
        """The case bar over the sub-notebook, in the plain tab ``frame``."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        bar = ttk.Frame(frame, padding=(12, 10, 12, 4))
        bar.columnconfigure(0, weight=1)
        bar.grid(row=0, column=0, sticky="ew")
        self._bar = bar
        notebook = ttk.Notebook(frame)
        notebook.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 6))
        self._subtabs = notebook
        self._cases_page = self._add_subtab("Cases")
        self._steps_page = self._add_subtab("Preconditions & steps")
        self._current_page = self._add_subtab("Current step")
        self._discuss_page = self._add_subtab(_DISCUSS_TITLE)
        self._result_page = self._add_subtab("Result")
        # Shown sub-tabs (the rest are hidden with ``notebook.hide``).
        self._shown_subtabs: set[str] = {
            str(page.outer) for page in self._subtab_pages()
        }
        # The sub-tab the next render selects (a claim started or ended).
        self._want_subtab: SectionPage | None = None
        # The Discuss sub-tab's title as last set.
        self._discuss_title = _DISCUSS_TITLE
        notebook.bind("<<NotebookTabChanged>>", self._on_subtab_changed)
        self._build_bar(bar)
        bar.bind("<Configure>", self._on_bar_configure, add="+")
        self._build_cases(self._cases_page.frame)
        self._build_steps(self._steps_page.frame)
        self._build_current(self._current_page.frame)
        self._build_discuss(self._discuss_page.frame)
        self._build_result(self._result_page.frame)
        self._show_subtabs((self._cases_page,))

    def _add_subtab(self, text: str) -> SectionPage:
        page = self._host.manual_scroll_area(
            self._subtabs, allowance=self._page_allowance
        )
        self._subtabs.add(page.outer, text=text)
        return page

    def _page_allowance(self) -> int:
        """Screen height a sub-page leaves for the header, the case bar and
        the sub-tabs' row, so a long sub-page scrolls instead of pushing the
        window off the screen."""
        # A removed bar keeps its last requested height: it counts as 0.
        bar = self._bar.winfo_reqheight() if self._bar.winfo_manager() else 0
        return HEADER_ALLOWANCE + bar + _SUBTAB_ALLOWANCE

    def _on_bar_configure(self, _event: "tk.Event[ttk.Frame] | None") -> None:
        # The bar's height is in every sub-page's allowance.
        for page in self._subtab_pages():
            page.refit_height()

    def _subtab_pages(self) -> tuple[SectionPage, ...]:
        return (
            self._cases_page,
            self._steps_page,
            self._current_page,
            self._discuss_page,
            self._result_page,
        )

    def _build_bar(self, bar: "ttk.Frame") -> None:
        host = self._host
        self._upload = host.manual_label(bar, "", foreground=MUTED)
        self._upload.grid(row=0, column=0, sticky="w")

        # The claimed case: title, Pause / Release and the paused banner.
        head = ttk.Frame(bar)
        head.columnconfigure(0, weight=1)
        head.grid(row=1, column=0, sticky="ew")
        self._head_frame = head
        self._case_title = host.manual_label(head, "")
        self._case_title.configure(font=HEADING_FONT)
        self._case_title.grid(row=0, column=0, sticky="w")
        self._pause_button = ttk.Button(head, text="Pause", command=self._pause)
        self._pause_button.grid(row=0, column=1, sticky="ne", padx=(8, 0))
        self._release_button = ttk.Button(
            head, text="Release case", command=self._ask_release
        )
        self._release_button.grid(row=0, column=2, sticky="ne", padx=(8, 0))
        self._build_pause(head)

        # The action row: step navigation, then Pass… / Fail… / Hand in….
        actions = ttk.Frame(bar)
        actions.columnconfigure(1, weight=1)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._actions_frame = actions
        self._prev_button = ttk.Button(
            actions, text="◀ Previous", command=lambda: self._move(-1)
        )
        self._prev_button.grid(row=0, column=0, sticky="w")
        self._picker = ttk.Combobox(
            actions, state="readonly", textvariable=self._picker_var
        )
        self._picker.grid(row=0, column=1, sticky="ew", padx=6)
        self._picker.bind("<<ComboboxSelected>>", self._on_pick)
        self._next_button = ttk.Button(
            actions, text="Next ▶", command=lambda: self._move(1)
        )
        self._next_button.grid(row=0, column=2, sticky="e")
        button_row = ttk.Frame(actions)
        button_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._pass_button = ttk.Button(
            button_row,
            text="Pass…",
            command=lambda: self._mark_clicked(ManualShotKind.PASS),
        )
        self._pass_button.pack(side="left")
        self._fail_button = ttk.Button(
            button_row,
            text="Fail…",
            command=lambda: self._mark_clicked(ManualShotKind.FAIL),
        )
        self._fail_button.pack(side="left", padx=(6, 0))
        # The one hand-in path (validation, confirmation, submit).
        self._submit_button = ttk.Button(
            button_row, text="Hand in…", command=self._ask_submit
        )
        self._submit_button.pack(side="left", padx=(18, 0))

        # One status area for the bar's buttons, whatever sub-tab shows.
        status = ttk.Frame(bar)
        status.columnconfigure(0, weight=1)
        status.grid(row=3, column=0, sticky="ew")
        status.grid_remove()
        self._status_frame = status
        self._capture_status = host.manual_label(status, "")
        self._capture_status.grid(row=0, column=0, sticky="w", pady=(4, 0))
        self._submit_status = host.manual_label(status, "")
        self._submit_status.grid(row=1, column=0, sticky="w", pady=(4, 0))
        # Inline confirmation (release or a contradicting hand-in).
        self._confirm_frame = ttk.Frame(status)
        self._confirm_frame.columnconfigure(0, weight=1)
        self._confirm_label = host.manual_label(
            self._confirm_frame, "", foreground=WARN
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
        self._confirm_frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._confirm_frame.grid_remove()

    def _build_pause(self, head: "ttk.Frame") -> None:
        """Under the title: the paused banner with Continue testing, why
        Pause / Continue does not apply now, and the last reply."""
        pause = ttk.Frame(head)
        pause.columnconfigure(0, weight=1)
        pause.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._pause_frame = pause
        self._paused_banner = ttk.Frame(pause)
        self._paused_banner.columnconfigure(0, weight=1)
        self._paused_banner.grid(row=0, column=0, sticky="ew")
        self._paused_label = self._host.manual_label(
            self._paused_banner, "", foreground=WARN
        )
        self._paused_label.configure(font=BOLD_FONT)
        self._paused_label.grid(row=0, column=0, sticky="w")
        self._host.manual_label(
            self._paused_banner,
            "Pass, Fail, screenshots and Hand in are off until you continue. "
            "You can still discuss the case or release it.",
            foreground=MUTED,
        ).grid(row=1, column=0, sticky="w")
        self._continue_button = ttk.Button(
            self._paused_banner, text="Continue testing", command=self._continue
        )
        self._continue_button.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._pause_blocker = self._host.manual_label(pause, "", foreground=MUTED)
        self._pause_blocker.grid(row=1, column=0, sticky="w")
        self._pause_status = self._host.manual_label(pause, "")
        self._pause_status.grid(row=2, column=0, sticky="w")
        pause.grid_remove()

    def _build_cases(self, page: "ttk.Frame") -> None:
        ttk.Label(page, text="Cases of this run", font=BOLD_FONT).grid(
            row=0, column=0, sticky="w"
        )
        holder = ttk.Frame(page)
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
        start_row = ttk.Frame(page)
        start_row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._start_button = ttk.Button(
            start_row, text="Start this case", command=self._start_clicked
        )
        self._start_button.pack(side="left")
        self._list_status = self._host.manual_label(page, "")
        self._list_status.grid(row=3, column=0, sticky="w", pady=(4, 0))

    def _build_steps(self, page: "ttk.Frame") -> None:
        host = self._host
        self._preconditions = host.manual_block(
            page, title="Preconditions", max_lines=_STEP_LINES
        )
        self._preconditions.frame.grid(row=0, column=0, sticky="ew")
        self._overview = host.manual_block(
            page, title="Steps", max_lines=_OVERVIEW_LINES
        )
        self._overview.text.tag_configure(
            "current", background=HIGHLIGHT_BG, foreground=TEXT
        )
        self._overview.text.tag_configure("failed", foreground=ERROR)
        self._overview.text.tag_configure("passed", foreground=OK)
        self._overview.frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))

    def _build_current(self, page: "ttk.Frame") -> None:
        host = self._host
        self._step_text = host.manual_block(page, title="Step", max_lines=_STEP_LINES)
        self._step_text.frame.grid(row=0, column=0, sticky="ew")
        self._expected = host.manual_block(
            page, title="Expected result", max_lines=_STEP_LINES
        )
        self._expected.frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._step_status = host.manual_label(page, "")
        self._step_status.grid(row=2, column=0, sticky="w", pady=(6, 0))
        shot_row = ttk.Frame(page)
        shot_row.grid(row=3, column=0, sticky="w", pady=(4, 0))
        # One label per screenshot of the shown step's mark.
        thumbs = ttk.Frame(shot_row)
        thumbs.grid(row=0, column=0, sticky="nw")
        self._step_thumbs = [ttk.Label(thumbs) for _ in range(MANUAL_STEP_MAX_SHOTS)]
        for index, thumb in enumerate(self._step_thumbs):
            thumb.grid(
                row=index // _STEP_SHOT_COLUMNS,
                column=index % _STEP_SHOT_COLUMNS,
                sticky="nw",
                padx=(0, 4),
                pady=(0, 4),
            )
        self._edit_button = ttk.Button(shot_row, text="Edit…", command=self._edit_mark)
        self._edit_button.grid(row=0, column=1, sticky="nw", padx=(8, 0))
        self._shortcut_line = host.manual_label(page, "", foreground=MUTED)
        self._shortcut_line.grid(row=4, column=0, sticky="w", pady=(4, 0))
        self._note_label = ttk.Label(page, text="")
        self._note_label.grid(row=5, column=0, sticky="w", pady=(6, 0))
        self._note = host.manual_entry(page, row=6, height=3)
        self._note.bind("<<Modified>>", self._on_note_modified)

    def _build_discuss(self, page: "ttk.Frame") -> None:
        self._host.manual_label(
            page,
            "The agent knows this case, its steps, the marks so far and the "
            "knowledge base. It advises only: it does not mark steps or decide "
            "the result.",
            foreground=MUTED,
        ).grid(row=0, column=0, sticky="w")
        # Rows 1 (chat), 2 (answering) and 3 (message box).
        self._chat = ChatPanel(
            self._host,
            page,
            row=1,
            title="Discussion with the agent (this case)",
            lines=_CHAT_LINES,
            on_send=self._send_chat,
            on_input=self._on_chat_input,
            working_text="The agent is answering…",
        )
        self._thread_link = ttk.Button(
            page, text="Open on Punk Hazard", command=self._open_thread
        )
        self._thread_link.grid(row=4, column=0, sticky="w", pady=(6, 0))

    def _build_result(self, page: "ttk.Frame") -> None:
        host = self._host
        ttk.Label(page, text="Case result", font=BOLD_FONT).grid(
            row=0, column=0, sticky="w"
        )
        choices = ttk.Frame(page)
        choices.grid(row=1, column=0, sticky="w", pady=(2, 0))
        for verdict_value, label in _VERDICT_LABELS.items():
            ttk.Radiobutton(
                choices,
                text=label,
                value=verdict_value.value,
                variable=self._verdict_var,
                command=self._on_verdict,
            ).pack(side="left", padx=(0, 10))
        self._verdict_note_label = ttk.Label(page, text="")
        self._verdict_note_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._verdict_note = host.manual_entry(page, row=3, height=3)
        self._verdict_note.bind("<<Modified>>", self._on_verdict_note_modified)
        case_shot = ttk.Frame(page)
        case_shot.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self._case_thumb = ttk.Label(case_shot)
        self._case_thumb.pack(side="left")
        self._case_shot_button = ttk.Button(
            case_shot,
            text="Take a case screenshot (optional)",
            command=self._capture_case,
        )
        self._case_shot_button.pack(side="left", padx=(8, 0), anchor="n")
        self._submit_hint = host.manual_label(
            page,
            "Steps you did not mark are reported as not run. Screenshots upload "
            "after you hand in. Hand in… is in the bar above.",
            foreground=MUTED,
        )
        self._submit_hint.grid(row=5, column=0, sticky="w", pady=(8, 0))

    # --- sub-tabs -------------------------------------------------------------

    def subtab_frames(self) -> tuple["ttk.Frame", ...]:
        """The shown sub-tabs' pages (``outer``), in tab order (``--self-check``
        selects each with ``select_subtab``)."""
        return tuple(
            page.outer
            for page in (
                self._cases_page,
                self._steps_page,
                self._current_page,
                self._discuss_page,
                self._result_page,
            )
            if str(page.outer) in self._shown_subtabs
        )

    def select_subtab(self, frame: "ttk.Frame") -> None:
        """Select a shown sub-tab (one of ``subtab_frames``); never takes the
        focus."""
        if str(frame) in self._shown_subtabs:
            self._subtabs.select(frame)

    def _select(self, page: SectionPage) -> None:
        if str(page.outer) in self._shown_subtabs:
            self._subtabs.select(page.outer)

    def _show_subtabs(self, shown: tuple[SectionPage, ...]) -> None:
        """Show exactly ``shown``; select the wanted sub-tab, else keep the
        tester's choice unless it is hidden now (Current step, else Cases)."""
        notebook = self._subtabs
        names = {str(page.outer) for page in shown}
        for page in shown:
            if str(page.outer) not in self._shown_subtabs:
                # Restored at its earlier position with its options.
                notebook.add(page.outer)
        selected = notebook.select()
        want = self._want_subtab
        self._want_subtab = None
        if want is not None and str(want.outer) in names:
            notebook.select(want.outer)
        elif str(selected) not in names:
            fallback = (
                self._current_page
                if str(self._current_page.outer) in names
                else self._cases_page
            )
            notebook.select(fallback.outer)
        for page in self._subtab_pages():
            if str(page.outer) not in names and str(page.outer) in self._shown_subtabs:
                notebook.hide(page.outer)
        self._shown_subtabs = names

    def _on_subtab_changed(self, _event: "tk.Event[ttk.Notebook]") -> None:
        # What opening the old Discuss toggle did: the latest message shows.
        if str(self._subtabs.select()) == str(self._discuss_page.outer):
            self._chat.block.text.see("end")

    def _sync_status_rows(self) -> None:
        """A status line of the bar takes room only while it says something
        (and a frame left with nothing shown is removed too: Tk keeps the
        size of a frame whose last child is removed)."""
        upload = self._sync_status_row(self._upload)
        capture = self._sync_status_row(self._capture_status)
        submit = self._sync_status_row(self._submit_status)
        pause_blocker = self._sync_status_row(self._pause_blocker)
        pause_status = self._sync_status_row(self._pause_status)
        claimed = self.active_case() is not None
        status = claimed and (capture or submit or self._confirm is not None)
        pause = self._pause_shown and (self._paused() or pause_blocker or pause_status)
        bar = claimed or upload
        bar_was_shown = bool(self._bar.winfo_manager())
        for frame, visible in (
            (self._status_frame, status),
            (self._pause_frame, pause),
            (self._bar, bar),
        ):
            if visible:
                frame.grid()
            else:
                frame.grid_remove()
        if bar != bar_was_shown:
            # Removing or restoring the bar sends no <Configure>.
            self._on_bar_configure(None)

    @staticmethod
    def _sync_status_row(label: "ttk.Label") -> bool:
        """Show ``label`` only while it says something; whether it does."""
        says = bool(str(label.cget("text")))
        if says:
            label.grid()
        else:
            label.grid_remove()
        return says

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
            paused = " · Paused" if state.active.paused else ""
            return state.active.title, f"Manual run · {state.run.name}{paused}"
        return "Manual run", state.run.name

    # --- commands from the runner ---------------------------------------------

    def apply_state(self, state: ManualWindowState) -> None:
        self.save_note()
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
            if active is not None:
                # The runner learns the shown step (progress reports).
                self._schedule_draft()
        self._sync_step_timer(active is not None and active.paused)
        if self._claim_busy and active is not None:
            self._claim_busy = False
        self.render()
        self._auto_claim.on_state(state, self._selected_pivot)

    def _sync_step_timer(self, paused: bool) -> None:
        """The step timer stops while the case is paused."""
        now = time.monotonic()
        if paused and self._step_started is not None:
            if self._step is not None:
                self._seconds[self._step] = self._seconds.get(self._step, 0.0) + (
                    now - self._step_started
                )
            self._step_started = None
        elif not paused and self._step_started is None and self._step is not None:
            self._step_started = now

    def _load_claim(
        self, active: ManualActiveCasePayload | None, texts: ManualDraftTexts | None
    ) -> None:
        """A newly claimed (or no longer claimed) case: fresh widgets."""
        self._flush_draft()
        if self._marking.is_open:
            # The claim ended (or changed): the runner dropped its draft with
            # the staged screenshots.
            self._marking.close(focus_back=True)
        self._loaded_claim = active.claim_token if active is not None else None
        self._confirm = None
        self._capture = None
        self._pause_busy = False
        self._chat_busy = False
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
        self._chat.set_input("")
        self._chat.set_status("")
        self._pause_status.configure(text="")
        self._capture_status.configure(text="")
        self._submit_status.configure(text="")
        if active is None:
            # Handed in or released: preselect the next case to do again.
            self._selected_pivot = None
            self._want_subtab = self._cases_page
        else:
            self._want_subtab = self._current_page
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
        if capture.staged:
            self._marking.on_capture(
                kind=capture.kind,
                claim_token=capture.claim_token,
                opening=capture.opening,
                replace=capture.replace,
                ok=ok,
                message=message,
                key=key,
                thumbnail_b64=thumbnail_b64,
            )
            return
        if ok:
            self.store_thumbnail(key, thumbnail_b64)
            self._capture_status.configure(text="")
            if capture.kind is ManualShotKind.FAIL:
                self._capture_status.configure(
                    text="Describe in the note what went wrong.",
                    foreground=WARN,
                )
                self._note.focus_set()
        else:
            self._capture_status.configure(text=message, foreground=ERROR)
        self.render()

    def on_thumbnail(self, key: str, thumbnail_b64: str) -> None:
        if thumbnail_b64:
            self.store_thumbnail(key, thumbnail_b64)
            self.render()

    def on_result(self, kind: ManualResultKind, ok: bool, message: str) -> None:
        color = OK if ok else ERROR
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
                # A hand-in confirmed after _SUBMIT_TIMEOUT_MS still landed:
                # say so (no next case is started then).
                if ok:
                    self._submit_status.configure(text="")
                    self._list_status.configure(
                        text=message or "Handed in.", foreground=color
                    )
                return
            self._end_submit()
            if ok and self._auto_claim.pending:
                self._list_status.configure(
                    text="Handed in. Starting the next case…", foreground=color
                )
            elif ok:
                self._list_status.configure(text=message, foreground=color)
            else:
                self._auto_claim.cancel()
                self._submit_status.configure(text=message, foreground=color)
        elif kind is ManualResultKind.MARK:
            self._marking.on_mark_result(ok, message)
            return
        elif kind in (ManualResultKind.PAUSE, ManualResultKind.CONTINUE):
            if not self._pause_busy:
                return
            self._pause_busy = False
            # Success shows in the next snapshot (the banner); keep a reply.
            self._pause_status.configure(
                text=message if message or not ok else "", foreground=color
            )
        elif kind is ManualResultKind.CHAT:
            if not self._chat_busy:
                return
            self._chat_busy = False
            if ok:
                self._chat.set_input("")
            self._chat.set_status(message, color)
        else:
            # A capture notice the window missed while it restarted.
            self._capture = None
            self._capture_status.configure(text=message, foreground=color)
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
            for row in (self._head_frame, self._actions_frame):
                row.grid_remove()
            self._show_subtabs((self._cases_page,))
            self._sync_status_rows()
            return
        for row in (self._head_frame, self._actions_frame):
            row.grid()
        shown: tuple[SectionPage, ...] = (
            self._cases_page,
            self._steps_page,
            self._current_page,
        )
        if active.discussion is not None:
            shown += (self._discuss_page,)
        self._show_subtabs(shown + (self._result_page,))
        self._render_active(active)
        self._sync_status_rows()
        capture = self._capture
        self._marking.render(active, capturing=capture is not None and capture.staged)

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
            text="\n".join(parts), foreground=WARN if warn else MUTED
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
            self._list_status.configure(text="This run has no cases.", foreground=MUTED)

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
        self._render_pause(active)
        self._render_discuss(active)
        self._render_verdict()
        self._render_confirm()

    def _paused(self) -> bool:
        active = self.state.active if self.state is not None else None
        return active is not None and active.paused

    def _render_pause(self, active: ManualActiveCasePayload) -> None:
        self._pause_shown = manual_pause_discuss_supported(active)
        if not self._pause_shown:
            self._pause_button.grid_remove()
            return
        connected = self._host.manual_connected()
        if active.paused:
            self._pause_button.grid_remove()
            at = clock(active.paused_at)
            since = f"Paused since {at}" if at else "Paused"
            by = f" (by {active.paused_by})" if active.paused_by else ""
            self._paused_label.configure(text=f"{since}{by}")
            self._paused_banner.grid()
            busy = _agent_busy(active)
            can = (
                active.can_continue and connected and not self._pause_busy and not busy
            )
            self._continue_button.configure(
                text="Continuing…" if self._pause_busy else "Continue testing",
                state="normal" if can else "disabled",
            )
            if not active.can_continue:
                blocker = active.pause_blocker
            else:
                blocker = _AGENT_BUSY if busy else ""
        else:
            self._paused_banner.grid_remove()
            self._pause_button.grid()
            can = (
                active.can_pause
                and connected
                and not self._pause_busy
                and not self._submit_busy
            )
            self._pause_button.configure(
                text="Pausing…" if self._pause_busy else "Pause",
                state="normal" if can else "disabled",
            )
            blocker = "" if active.can_pause else active.pause_blocker
        self._pause_blocker.configure(text=blocker)

    def _render_discuss(self, active: ManualActiveCasePayload) -> None:
        discussion = active.discussion
        if discussion is None:
            # The sub-tab is hidden (an older server sends no discussion).
            return
        count = f" ({len(discussion.messages)})" if discussion.messages else ""
        live = " …" if discussion.turn_live else ""
        title = f"{_DISCUSS_TITLE}{count}{live}"
        if title != self._discuss_title:
            self._discuss_title = title
            self._subtabs.tab(self._discuss_page.outer, text=title)
        # Rendered while another sub-tab shows too: only new rows are appended.
        self._chat.render_rows(active.claim_token, discussion.messages)
        self._chat.set_working(discussion.turn_live)
        self._render_chat_send(discussion)
        if discussion.thread_url:
            self._thread_link.grid()
        else:
            self._thread_link.grid_remove()

    def _render_chat_send(self, discussion: ManualDiscussionPayload) -> None:
        self._chat.render_send(
            connected=self._host.manual_connected(),
            can_send=discussion.can_send,
            busy=self._chat_busy,
            send_blocker=discussion.send_blocker,
        )

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

    def _can_mark(self, step: ManualStepPayload | None) -> bool:
        """Pass… / Fail… / Edit… and the shortcuts may act on the shown step."""
        return (
            step is not None
            and self._capture is None
            and not self._marking.is_open
            and not self._submit_busy
            and not self._paused()
            and self.state is not None
            and not self.state.spool_full
        )

    def _render_step_mark(self, step: ManualStepPayload | None) -> None:
        can_mark = self._can_mark(step)
        mark = self._marks.get(step.num) if step is not None else None
        keys = mark.keys if mark is not None else ()
        for index, thumb in enumerate(self._step_thumbs):
            if index < len(keys):
                self._show_thumbnail(thumb, keys[index], half=len(keys) > 1)
            else:
                thumb.configure(image="")
        if mark is None:
            self._step_status.configure(text="Not marked yet", foreground=MUTED)
            self._note_label.configure(text="Note (optional)")
            self._edit_button.grid_remove()
        else:
            failed = mark.outcome is ManualStepOutcome.FAIL
            shots = f" · {len(keys)} screenshots" if len(keys) > 1 else ""
            self._step_status.configure(
                text=("Failed" if failed else "Passed") + shots,
                foreground=ERROR if failed else OK,
            )
            self._note_label.configure(
                text="What went wrong? (required)" if failed else "Note (optional)"
            )
            self._edit_button.grid()
            self._edit_button.configure(state="normal" if can_mark else "disabled")
        triggers = self._shortcut_status.triggers
        show_keys = self._shortcut_status.state is not HotkeyState.OFF
        for button, kind, label in (
            (self._pass_button, HotkeyKind.PASS, "Pass…"),
            (self._fail_button, HotkeyKind.FAIL, "Fail…"),
        ):
            trigger = triggers.get(kind, "") if show_keys else ""
            button.configure(
                text=f"{label} ({trigger})" if trigger else label,
                state="normal" if can_mark else "disabled",
            )
        self._shortcut_line.configure(text=self._shortcut_text())
        capture = self._capture
        # A screenshot taken in the open sheet shows its progress there.
        if (
            capture is not None
            and capture.step_num is not None
            and (not capture.staged or capture.opening)
        ):
            self._capture_status.configure(
                text="Taking the screenshot…", foreground=MUTED
            )

    def _render_verdict(self) -> None:
        verdict = self._verdict()
        failed_steps = self.failed_steps()
        if verdict is ManualCaseVerdictWire.BLOCKED:
            label = "Why is the case blocked? (required)"
        elif verdict is ManualCaseVerdictWire.FAILED and not failed_steps:
            label = "Why did the case fail? (required: no step failed)"
        else:
            label = "Note for the case (optional)"
        self._verdict_note_label.configure(text=label)
        busy = self._capture is not None or self._submit_busy or self._pause_busy
        paused = self._paused()
        full = self.state is not None and self.state.spool_full
        if self._case_shot_key:
            self._show_thumbnail(self._case_thumb, self._case_shot_key)
            self._case_shot_button.configure(text="Retake the case screenshot")
        else:
            self._case_thumb.configure(image="")
            self._case_shot_button.configure(text="Take a case screenshot (optional)")
        self._case_shot_button.configure(
            state="disabled" if busy or full or paused else "normal"
        )
        self._submit_button.configure(
            text="Handing in…" if self._submit_busy else "Hand in…",
            # Enabled without a verdict: the click says what is missing and
            # opens the Result sub-tab where the verdict is chosen.
            state="disabled" if busy or paused else "normal",
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
            failed = self.failed_steps()
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

    # --- thumbnails ---------------------------------------------------------------

    def store_thumbnail(self, key: str, thumbnail_b64: str) -> None:
        if not key or not thumbnail_b64:
            return
        try:
            image = tk.PhotoImage(master=self._root, data=thumbnail_b64)
        except tk.TclError:
            return
        self._thumbnails[key] = image
        self._thumbnails.move_to_end(key)
        while len(self._thumbnails) > _THUMBNAIL_CACHE:
            evicted, _image = self._thumbnails.popitem(last=False)
            # Asked for again when it shows again.
            self._requested.discard(evicted)

    def _thumbnail(self, key: str) -> "tk.PhotoImage | None":
        """The thumbnail, else None after asking the runner for it (once)."""
        image = self._thumbnails.get(key)
        if image is not None:
            return image
        if key not in self._requested:
            self._requested.add(key)
            self._host.manual_send(ManualThumbnailRequestEvent(key=key))
        return None

    def _half_thumbnail(self, key: str) -> "tk.PhotoImage | None":
        image = self._thumbnail(key)
        return self._half.get(key, image) if image is not None else None

    def _show_thumbnail(
        self, label: "ttk.Label", key: str, *, half: bool = False
    ) -> None:
        image = self._half_thumbnail(key) if half else self._thumbnail(key)
        label.configure(image=image if image is not None else "")

    # --- texts ------------------------------------------------------------------

    def _verdict(self) -> ManualCaseVerdictWire | None:
        try:
            return ManualCaseVerdictWire(self._verdict_var.get())
        except ValueError:
            return None

    def failed_steps(self) -> list[int]:
        return sorted(
            num
            for num, mark in self._marks.items()
            if mark.outcome is ManualStepOutcome.FAIL
        )

    def save_note(self) -> None:
        if self._step is not None and self._loaded_claim is not None:
            self._notes[self._step] = _text(self._note)

    def draft_texts(self) -> ManualDraftTexts:
        self.save_note()
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
                texts=self.draft_texts(),
                current_step=self._step,
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
            self._auto_claim.cancel()
            self._selected_pivot = state.cases[index].pivot_id
            self._list_status.configure(text="")
            self.render()

    def _start_clicked(self) -> None:
        self._auto_claim.cancel()
        self.start_case()

    def start_case(self) -> None:
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

    def go_to(self, num: int) -> None:
        if num == self._step:
            return
        self.save_note()
        now = time.monotonic()
        if self._step is not None and self._step_started is not None:
            self._seconds[self._step] = self._seconds.get(self._step, 0.0) + (
                now - self._step_started
            )
        self._step = num
        self._step_started = None if self._paused() else now
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
            self.go_to(nums[index])

    def _on_pick(self, _event: "tk.Event[ttk.Combobox]") -> None:
        active = self.state.active if self.state is not None else None
        index = self._picker.current()
        if active is not None and 0 <= index < len(active.steps):
            self.go_to(active.steps[index].num)

    def _send_capture(
        self,
        kind: ManualShotKind,
        step_num: int | None,
        *,
        staged: bool = False,
        opening: bool = False,
        replace: str | None = None,
    ) -> bool:
        """Ask the runner for a screenshot; False when one cannot start now."""
        active = self.state.active if self.state is not None else None
        if active is None or self._capture is not None or active.paused:
            return False
        self._seq = next_seq(self._seq)
        self._capture = _Capture(
            seq=self._seq,
            kind=kind,
            step_num=step_num,
            claim_token=active.claim_token,
            staged=staged,
            opening=opening,
            replace=replace,
        )
        self._confirm = None
        if not staged or opening:
            self._capture_status.configure(
                text="Taking the screenshot…", foreground=MUTED
            )
        self.render()
        self._host.manual_send(
            ManualCaptureEvent(
                seq=self._seq,
                pivot_id=active.pivot_id,
                claim_token=active.claim_token,
                kind=kind,
                step_num=step_num,
                staged=staged,
            )
        )
        return True

    def _capture_case(self) -> None:
        self._send_capture(ManualShotKind.CASE, None)

    # --- the mark sheet and the shortcuts -----------------------------------------

    def shortcuts_wanted(self) -> bool:
        """The shortcuts are registered while a case is claimed and not paused."""
        active = self.state.active if self.state is not None else None
        return active is not None and not active.paused

    def set_shortcut_status(self, status: HotkeyStatus) -> None:
        """The backend's status changed (``PersonWindowApp.poll``)."""
        self._shortcut_status = status
        # The window's own bindings stand in while the shortcuts are not
        # registered system-wide (and not turned off).
        self._bind_focus_keys(
            status.state in (HotkeyState.FOCUS_ONLY, HotkeyState.PENDING)
        )
        self.render()

    def _bind_focus_keys(self, bound: bool) -> None:
        if bound == self._focus_keys_bound:
            return
        self._focus_keys_bound = bound
        for kind, letter in _SHORTCUT_LETTERS.items():
            # Shift may or may not turn the keysym upper case (Caps Lock).
            for keysym in (letter.upper(), letter):
                sequence = f"<{_SHORTCUT_MODIFIER}-Shift-KeyPress-{keysym}>"
                if bound:
                    self._root.bind_all(sequence, self._focus_key_handler(kind))
                else:
                    self._root.unbind_all(sequence)

    def _focus_key_handler(
        self, kind: HotkeyKind
    ) -> Callable[["tk.Event[tk.Misc]"], str]:
        def handler(_event: "tk.Event[tk.Misc]") -> str:
            # The window has the focus: it keeps it after Save, like a click.
            self.on_shortcut(kind, system_wide=False)
            return "break"

        return handler

    def _shortcut_text(self) -> str:
        status = self._shortcut_status
        if status.state is HotkeyState.OFF:
            return ""
        if status.state is HotkeyState.PENDING:
            return "Approve the shortcuts in the desktop's dialog…"
        triggers = status.triggers
        keys = (
            f"{triggers.get(HotkeyKind.PASS, '?')} pass · "
            f"{triggers.get(HotkeyKind.FAIL, '?')} fail"
        )
        if status.state is HotkeyState.FOCUS_ONLY:
            reason = f": {status.reason}" if status.reason else ""
            return (
                f"Shortcuts ({keys}) work only while this window is " f"focused{reason}"
            )
        text = f"Shortcuts: {keys}"
        if sys.platform == "darwin":
            text += " (the P and F keys of a US keyboard layout)"
        return f"{text}\n{status.reason}" if status.reason else text

    def on_shortcut(self, kind: HotkeyKind, *, system_wide: bool) -> None:
        """A Pass / Fail shortcut: what Pass… / Fail… do, for the shown step.

        ``system_wide``: from the registered shortcut (any app may be in
        front), not from the window's own key bindings (it has the focus).
        Ignored unless a case is claimed and not paused, a step shows, no
        screenshot is being taken, no sheet is open, the spool has room and
        the runner has not hidden the window (an agent action or a capture).
        """
        active = self.state.active if self.state is not None else None
        if active is None or self._host.manual_hidden():
            return
        if not self._can_mark(self._active_step(active)):
            if self.state is not None and self.state.spool_full:
                self._capture_status.configure(
                    text="The screenshot spool is full: wait for the uploads.",
                    foreground=WARN,
                )
                self._sync_status_rows()
            return
        self._host.manual_note_foreground(system_wide)
        shot_kind = (
            ManualShotKind.PASS if kind is HotkeyKind.PASS else ManualShotKind.FAIL
        )
        self._start_sheet_capture(shot_kind)

    def _mark_clicked(self, kind: ManualShotKind) -> None:
        active = self.state.active if self.state is not None else None
        if active is None or not self._can_mark(self._active_step(active)):
            return
        # The tester is in this window: the focus stays here after Save.
        self._host.manual_note_foreground(False)
        self._start_sheet_capture(kind)

    def _start_sheet_capture(self, kind: ManualShotKind) -> None:
        if self._step is None:
            return
        if not self._send_capture(kind, self._step, staged=True, opening=True):
            self._host.manual_note_foreground(False)

    def _edit_mark(self) -> None:
        """Edit…: the sheet on the shown step's mark, with no new screenshot."""
        active = self.state.active if self.state is not None else None
        step = self._active_step(active) if active is not None else None
        mark = self._marks.get(step.num) if step is not None else None
        if active is None or mark is None or not self._can_mark(step):
            return
        self._marking.edit(active, mark)

    def preview_sheet(self) -> bool:
        """``--self-check``: open and close the sheet on the shown step
        (``MarkSession.preview``). False when no case is claimed."""
        return self._marking.preview()

    # --- MarkTab / AutoClaimTab (``person_window_mark_session``) -------------------

    def active_case(self) -> ManualActiveCasePayload | None:
        return self.state.active if self.state is not None else None

    def shown_step(self) -> ManualStepPayload | None:
        active = self.active_case()
        return self._active_step(active) if active is not None else None

    def step_marks(self) -> Mapping[int, ManualMarkView]:
        return self._marks

    def record_mark(self, mark: ManualMarkView) -> None:
        self._marks[mark.step_num] = mark

    def step_note(self, num: int) -> str:
        return self._notes.get(num, "")

    def keep_step_note(self, num: int, note: str) -> None:
        self._notes[num] = note
        if self._step == num:
            _set_text(self._note, note)
            self._note.edit_modified(False)
        self._schedule_draft()

    def case_note(self) -> str:
        return _text(self._verdict_note)

    def capture_busy(self) -> bool:
        return self._capture is not None

    def capture_for_sheet(
        self, kind: ManualShotKind, step_num: int, *, replace: str | None
    ) -> bool:
        return self._send_capture(kind, step_num, staged=True, replace=replace)

    def show_capture_status(self, text: str, color: str) -> None:
        self._capture_status.configure(text=text, foreground=color)
        self._sync_status_rows()

    def set_case_result(self, verdict: ManualCaseVerdictWire, note: str) -> None:
        self._verdict_var.set(verdict.value)
        _set_text(self._verdict_note, note)
        self._verdict_note.edit_modified(False)

    def show_hand_in_error(self, message: str, step: int | None) -> None:
        self._submit_status.configure(text=message, foreground=ERROR)
        self._select_for_error(step)
        if step is not None:
            self.go_to(step)
        self._sync_status_rows()
        self._schedule_draft()

    def show_list_status(self, text: str, color: str) -> None:
        self._list_status.configure(text=text, foreground=color)

    def claim_idle(self) -> bool:
        return (
            not self._submit_busy
            and not self._claim_busy
            and self._host.manual_connected()
        )

    def _ask_release(self) -> None:
        if self._release_busy or not self._host.manual_connected():
            return
        self._confirm = "release"
        self.render()

    def validation_error(
        self, texts: ManualDraftTexts, failed: list[int] | None = None
    ) -> tuple[str, int | None]:
        """Why the hand-in cannot go yet (and the step to show), or ("", None).

        ``failed``: the failed steps to judge by (the mark sheet's, before
        its mark is saved); the marks by default.
        """
        failed_steps = self.failed_steps() if failed is None else failed
        verdict = texts.verdict
        if verdict is None:
            return "Choose Passed, Failed or Blocked for the case.", None
        for num in failed_steps:
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
            if verdict is ManualCaseVerdictWire.FAILED and not failed_steps:
                return "No step failed: say in the case note why the case failed.", None
        return "", None

    def _ask_submit(self) -> None:
        if self._submit_busy or self._capture is not None or self._paused():
            return
        texts = self.draft_texts()
        message, step = self.validation_error(texts)
        if message:
            self._submit_status.configure(text=message, foreground=ERROR)
            self._select_for_error(step)
            if step is not None:
                self.go_to(step)
                self._note.focus_set()
            self.render()
            return
        self._submit_status.configure(text="")
        failed = self.failed_steps()
        verdict = texts.verdict
        contradicts = (verdict is ManualCaseVerdictWire.PASSED and bool(failed)) or (
            verdict is ManualCaseVerdictWire.FAILED and not failed
        )
        if contradicts:
            self._confirm = "submit"
            self.render()
            return
        self.submit(texts)

    def _select_for_error(self, step: int | None) -> None:
        """A hand-in that cannot go: the sub-tab where the tester fixes it
        (the step's note, else the case result)."""
        self._select(self._current_page if step is not None else self._result_page)

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
            texts = self.draft_texts()
            if self.validation_error(texts)[0]:
                self.render()
                return
            self.submit(texts)

    def submit(self, texts: ManualDraftTexts) -> bool:
        """Hand the case in (spooled by the runner); False when it cannot go."""
        active = self.state.active if self.state is not None else None
        if active is None or active.paused or self._submit_busy:
            return False
        if self._draft_job is not None:
            self._root.after_cancel(self._draft_job)
            self._draft_job = None
        self._submit_busy = True
        self._submit_job = self._root.after(_SUBMIT_TIMEOUT_MS, self._submit_timeout)
        self.render()
        self._host.manual_send(
            ManualSubmitEvent(
                pivot_id=active.pivot_id, claim_token=active.claim_token, texts=texts
            )
        )
        return True

    def _end_submit(self) -> None:
        self._submit_busy = False
        if self._submit_job is not None:
            self._root.after_cancel(self._submit_job)
            self._submit_job = None

    def _submit_timeout(self) -> None:
        """The runner never answered the hand-in (it dropped the request):
        Hand in works again, and no next case is started."""
        self._submit_job = None
        if not self._submit_busy:
            return
        self._submit_busy = False
        if self._auto_claim.pending:
            self._auto_claim.cancel()
            self._list_status.configure(text="")
        self._submit_status.configure(
            text="The Test Runner did not confirm the hand-in. Hand in again.",
            foreground=ERROR,
        )
        self.render()

    # --- pause and discuss (MANUAL_PAUSE_DISCUSS_V1) -------------------------------

    def _send_pause_action(self, kind: ManualCaseActionKind) -> None:
        active = self.state.active if self.state is not None else None
        if active is None or self._pause_busy or not self._host.manual_connected():
            return
        allowed = (
            active.can_continue and not _agent_busy(active)
            if active.paused
            else active.can_pause
        )
        if not allowed or (kind is ManualCaseActionKind.CONTINUE) != active.paused:
            return
        # Notes typed so far reach the runner first (it reports the marks
        # before a pause).
        self._flush_draft()
        self._pause_busy = True
        self._pause_status.configure(text="")
        self.render()
        # Never hides or raises the window.
        self._host.manual_send(
            ManualCaseActionEvent(
                action=ManualCaseActionPayload(
                    action=kind,
                    pivot_id=active.pivot_id,
                    claim_token=active.claim_token,
                )
            )
        )

    def _pause(self) -> None:
        self._send_pause_action(ManualCaseActionKind.PAUSE)

    def _continue(self) -> None:
        self._send_pause_action(ManualCaseActionKind.CONTINUE)

    def _on_chat_input(self) -> None:
        active = self.state.active if self.state is not None else None
        if active is not None and active.discussion is not None:
            self._render_chat_send(active.discussion)

    def _send_chat(self) -> None:
        active = self.state.active if self.state is not None else None
        if active is None or active.discussion is None:
            return
        if self._chat_busy or not self._host.manual_connected():
            return
        if not active.discussion.can_send:
            return
        text = self._chat.input_text().strip()
        if not text:
            return
        if len(text) > PERSON_CHAT_TEXT_MAX_CHARS:
            self._chat.set_status(
                f"The message is too long ({len(text)} of "
                f"{PERSON_CHAT_TEXT_MAX_CHARS} characters).",
                ERROR,
            )
            return
        # Notes typed so far reach the runner first (it reports the marks
        # before the message).
        self._flush_draft()
        self._chat_busy = True
        self._chat.set_status("")
        self.render()
        # Never hides or raises the window.
        self._host.manual_send(
            ManualChatSendEvent(
                send=ManualChatSendPayload(
                    pivot_id=active.pivot_id,
                    claim_token=active.claim_token,
                    text=text,
                )
            )
        )

    def _open_thread(self) -> None:
        active = self.state.active if self.state is not None else None
        if active is not None and active.discussion is not None:
            self._host.manual_open_url(active.discussion.thread_url)
