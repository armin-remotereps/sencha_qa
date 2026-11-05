"""The Manual run tab's mark sheet session and the next case after a hand-in.

``MarkSession`` drives the mark sheet (``person_window_mark_sheet.MarkSheet``)
for one step of one claim:

- it opens once the first staged screenshot of Pass… / Fail… / a shortcut
  arrives, or on Edit… with the step's mark and no new screenshot;
- Add screenshot / Retake / Remove stage or throw away screenshots;
- Save records the mark (``ManualMarkCommitEvent``), moves to the next step
  left to mark and, on the last one (or "Stop here" on a Fail), hands the
  case in after the tab's hand-in rules, asking inline when the verdict
  contradicts the steps;
- Cancel throws the staged screenshots away and leaves the step as it was.

A Save the runner never answers (it dropped the request: no coordinator) is
given up after ``SAVE_TIMEOUT_MS``, so the tester can save again. A late
"saved" after that still records the step (the mark is in the spool by
then) but never hands in or moves on.

``AutoClaim`` claims the preselected case to do once the snapshot shows that
the case handed in from the sheet landed (the server keeps one claim per run
until then), and gives up after ``AUTO_CLAIM_WAIT_MS``.

Both reach the tab only through ``MarkTab`` / ``AutoClaimTab`` (implemented
by ``person_window_manual.ManualView``) and the window through
``SessionWindow``; every method runs on the Tk thread.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from controller_client.person_window_ipc import (
    ManualDraftTexts,
    ManualMarkCommitEvent,
    ManualMarkDiscardEvent,
    ManualMarkView,
    ManualWindowState,
    WindowEvent,
)
from controller_client.person_window_mark_sheet import (
    HandIn,
    MarkSheet,
    SheetHost,
    SheetView,
)
from controller_client.person_window_theme import ERROR, MUTED, WARN
from controller_client.protocol import (
    MANUAL_STEP_NOTE_MAX_CHARS,
    ManualActiveCasePayload,
    ManualCaseVerdictWire,
    ManualShotKind,
    ManualStepOutcome,
    ManualStepPayload,
)

try:
    import tkinter as tk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# After a hand-in from the sheet, the next case is claimed once the snapshot
# shows the hand-in landed; the window stops waiting after this long.
AUTO_CLAIM_WAIT_MS: Final[int] = 120_000
# Save waits this long for the runner's answer (it is local: the runner
# stages and commits in its spool); then the sheet takes Save and Cancel
# again.
SAVE_TIMEOUT_MS: Final[int] = 30_000


class SessionWindow(SheetHost, Protocol):
    """What the session needs from the window (``PersonWindowApp``)."""

    def manual_send(self, event: WindowEvent) -> None: ...

    def manual_note_foreground(self, from_shortcut: bool) -> None:
        """Remember the app in front (a shortcut), or forget it (a click)."""
        ...

    def manual_focus_sheet(self) -> None:
        """Bring the window to the front for the open mark sheet."""
        ...

    def manual_focus_back(self) -> None:
        """Give the front back to the app remembered by a shortcut."""
        ...


class AutoClaimTab(Protocol):
    """What ``AutoClaim`` needs from the tab (``ManualView``)."""

    def show_list_status(self, text: str, color: str) -> None: ...

    def claim_idle(self) -> bool:
        """Connected, with no claim and no hand-in waiting for an answer."""
        ...

    def start_case(self) -> None:
        """Start this case (claim the selected case)."""
        ...


class MarkTab(Protocol):
    """What ``MarkSession`` needs from the tab (``ManualView``)."""

    def active_case(self) -> ManualActiveCasePayload | None: ...

    def shown_step(self) -> ManualStepPayload | None: ...

    def step_marks(self) -> Mapping[int, ManualMarkView]: ...

    def record_mark(self, mark: ManualMarkView) -> None:
        """Show a saved mark at once (the next snapshot says the same)."""
        ...

    def failed_steps(self) -> list[int]: ...

    def step_note(self, num: int) -> str: ...

    def keep_step_note(self, num: int, note: str) -> None:
        """The step's note from now on (the draft carries it)."""
        ...

    def save_note(self) -> None:
        """Keep what the shown step's note box holds."""
        ...

    def case_note(self) -> str: ...

    def draft_texts(self) -> ManualDraftTexts: ...

    def validation_error(
        self, texts: ManualDraftTexts, failed: list[int] | None = None
    ) -> tuple[str, int | None]: ...

    def capture_busy(self) -> bool: ...

    def capture_for_sheet(
        self, kind: ManualShotKind, step_num: int, *, replace: str | None
    ) -> bool:
        """A staged screenshot for the open sheet; False when it cannot start."""
        ...

    def store_thumbnail(self, key: str, thumbnail_b64: str) -> None: ...

    def go_to(self, num: int) -> None: ...

    def render(self) -> None: ...

    def show_capture_status(self, text: str, color: str) -> None: ...

    def set_case_result(self, verdict: ManualCaseVerdictWire, note: str) -> None:
        """The tab's Case result, as chosen in the sheet."""
        ...

    def show_hand_in_error(self, message: str, step: int | None) -> None:
        """The hand-in cannot go: say why in the tab (and show the step)."""
        ...

    def submit(self, texts: ManualDraftTexts) -> bool: ...


@dataclass
class _SheetSession:
    """The open mark sheet: one step of one claim."""

    claim_token: str
    step_num: int
    # The sheet's screenshots in order; ``staged`` are the ones taken in it
    # (an edited mark's kept ones are not).
    keys: list[str]
    staged: set[str]
    # Opened on an existing mark (Edit…): no hand-in, Save does not advance.
    edit: bool
    # The edited mark as it was (Save without a change sends nothing).
    original: tuple[ManualStepOutcome, tuple[str, ...]] | None = None
    saving: bool = False
    # A Save was given up on (SAVE_TIMEOUT_MS); its late answer still counts.
    save_timed_out: bool = False
    hand_in: HandIn | None = None
    outcome: ManualStepOutcome = ManualStepOutcome.PASS
    status: str = ""
    status_color: str = MUTED


class AutoClaim:
    """Start the next case after a hand-in from the sheet."""

    def __init__(self, tab: AutoClaimTab, root: "tk.Tk") -> None:
        self._tab = tab
        self._root = root
        # The case handed in from the sheet whose next case is claimed once
        # the snapshot shows the hand-in landed.
        self._pivot_id: int | None = None
        self._job: str | None = None

    @property
    def pending(self) -> bool:
        return self._pivot_id is not None

    def begin(self, pivot_id: int) -> None:
        self.cancel()
        self._pivot_id = pivot_id
        self._job = self._root.after(AUTO_CLAIM_WAIT_MS, self._timeout)
        self._tab.show_list_status("Handed in. Starting the next case…", MUTED)

    def cancel(self) -> None:
        self._pivot_id = None
        if self._job is not None:
            self._root.after_cancel(self._job)
            self._job = None

    def _timeout(self) -> None:
        self._job = None
        if self._pivot_id is None:
            return
        self._pivot_id = None
        self._tab.show_list_status(
            "The next case did not start on its own: press Start this case "
            "when the case you handed in shows its result.",
            WARN,
        )

    def on_state(self, state: ManualWindowState, selected_pivot: int | None) -> None:
        """Claim the preselected case once the handed-in case has landed.

        The server keeps one claim per run until the hand-in arrives, so the
        window waits until the snapshot no longer lists the case as handed
        in (spooled) nor in progress.
        """
        pivot_id = self._pivot_id
        if pivot_id is None or state.active is not None:
            return
        if not self._tab.claim_idle():
            return
        if pivot_id in state.submitted_pivots:
            return
        case = next((c for c in state.cases if c.pivot_id == pivot_id), None)
        if case is not None and case.status == "in_progress":
            return
        self.cancel()
        submitted = set(state.submitted_pivots)
        selected = next((c for c in state.cases if c.pivot_id == selected_pivot), None)
        if (
            selected is None
            or selected.status != "created"
            or selected.claimed_label
            or selected.pivot_id in submitted
        ):
            self._tab.show_list_status("No case left to do in this run.", MUTED)
            return
        self._tab.start_case()


class MarkSession:
    """The open mark sheet of the Manual run tab and what its buttons do."""

    def __init__(
        self,
        window: SessionWindow,
        tab: MarkTab,
        root: "tk.Tk",
        *,
        thumbnail: Callable[[str], "tk.PhotoImage | None"],
        auto_claim: AutoClaim,
    ) -> None:
        self._window = window
        self._tab = tab
        self._root = root
        self._auto_claim = auto_claim
        self._session: _SheetSession | None = None
        # Gives up a Save the runner never answers.
        self._save_job: str | None = None
        self._sheet = MarkSheet(
            window,
            root,
            thumbnail=thumbnail,
            on_add=self._add,
            on_retake=self._retake,
            on_remove=self._remove,
            on_save=self._save,
            on_cancel=self._cancel,
        )

    @property
    def is_open(self) -> bool:
        return self._session is not None

    # --- opening ------------------------------------------------------------

    def edit(self, active: ManualActiveCasePayload, mark: ManualMarkView) -> None:
        """Edit…: the sheet on the shown step's mark, with no new screenshot."""
        self._window.manual_note_foreground(False)
        self._open(
            active,
            mark.outcome,
            keys=list(mark.keys),
            staged=set(),
            edit=True,
        )

    def on_capture(
        self,
        *,
        kind: ManualShotKind,
        claim_token: str,
        opening: bool,
        replace: str | None,
        ok: bool,
        message: str,
        key: str,
        thumbnail_b64: str,
    ) -> None:
        """A staged screenshot arrived (or failed): open the sheet with it,
        or add it to the open one."""
        session = self._session
        active = self._tab.active_case()
        current = active is not None and active.claim_token == claim_token
        if not ok:
            if session is not None:
                session.status, session.status_color = message, ERROR
            else:
                # No screenshot, no sheet: nothing took the focus.
                self._window.manual_note_foreground(False)
                self._tab.show_capture_status(message, ERROR)
            self._tab.render()
            return
        self._tab.store_thumbnail(key, thumbnail_b64)
        if session is None:
            if opening and current and active is not None:
                self._tab.show_capture_status("", MUTED)
                outcome = (
                    ManualStepOutcome.FAIL
                    if kind is ManualShotKind.FAIL
                    else ManualStepOutcome.PASS
                )
                self._open(active, outcome, keys=[key], staged={key}, edit=False)
            elif current and active is not None:
                # The sheet was cancelled while the screenshot was taken.
                self._discard((key,))
            return
        session.status = ""
        if replace is not None and replace in session.keys:
            session.keys[session.keys.index(replace)] = key
            if replace in session.staged:
                session.staged.discard(replace)
                self._discard((replace,))
        else:
            session.keys.append(key)
        session.staged.add(key)
        self._tab.render()

    def _open(
        self,
        active: ManualActiveCasePayload,
        outcome: ManualStepOutcome,
        *,
        keys: list[str],
        staged: set[str],
        edit: bool,
        focus: bool = True,
    ) -> None:
        step = self._tab.shown_step()
        if step is None:
            return
        self._tab.save_note()
        marks = self._tab.step_marks()
        others_unmarked = any(
            s.num not in marks for s in active.steps if s.num != step.num
        )
        other_failed = any(num != step.num for num in self._tab.failed_steps())
        mark = marks.get(step.num)
        self._session = _SheetSession(
            claim_token=active.claim_token,
            step_num=step.num,
            keys=keys,
            staged=staged,
            edit=edit,
            original=(mark.outcome, mark.keys) if edit and mark is not None else None,
        )
        first_line = step.text.strip().splitlines()[0] if step.text.strip() else ""
        expected = f"Expected: {step.expected}" if step.expected else ""
        self._sheet.open(
            title=f"Step {step.num} of {len(active.steps)} · {first_line}",
            step_line=expected,
            outcome=outcome,
            note=self._tab.step_note(step.num),
            case_note=self._tab.case_note(),
            last_step=not others_unmarked,
            other_failed=other_failed,
            edit=edit,
        )
        self._tab.render()
        if focus:
            self._window.manual_focus_sheet()
            self._sheet.focus_note()

    def render(self, active: ManualActiveCasePayload, *, capturing: bool) -> None:
        """``capturing``: a screenshot for the sheet is being taken."""
        session = self._session
        if session is None:
            return
        blocker = (
            "The case is paused: continue testing to save the mark."
            if active.paused
            else ""
        )
        self._sheet.render(
            SheetView(
                shots=tuple(session.keys),
                max_shots=active.step_max_shots,
                capturing=capturing,
                saving=session.saving,
                blocker=blocker,
                status=session.status,
                status_color=session.status_color,
            )
        )

    # --- the sheet's buttons --------------------------------------------------

    def _capture_kind(self) -> ManualShotKind:
        return (
            ManualShotKind.FAIL
            if self._sheet.outcome() is ManualStepOutcome.FAIL
            else ManualShotKind.PASS
        )

    def _add(self) -> None:
        session = self._session
        active = self._tab.active_case()
        if session is None or active is None or session.saving:
            return
        if len(session.keys) >= active.step_max_shots:
            return
        session.status = ""
        self._tab.capture_for_sheet(
            self._capture_kind(), session.step_num, replace=None
        )

    def _retake(self, key: str) -> None:
        session = self._session
        if session is None or session.saving or key not in session.keys:
            return
        session.status = ""
        self._tab.capture_for_sheet(self._capture_kind(), session.step_num, replace=key)

    def _remove(self, key: str) -> None:
        session = self._session
        if session is None or session.saving or self._tab.capture_busy():
            return
        if key not in session.keys or len(session.keys) < 2:
            return
        session.keys.remove(key)
        if key in session.staged:
            session.staged.discard(key)
            self._discard((key,))
        self._tab.render()

    def _discard(self, keys: tuple[str, ...]) -> None:
        active = self._tab.active_case()
        if active is None or not keys:
            return
        self._window.manual_send(
            ManualMarkDiscardEvent(
                pivot_id=active.pivot_id, claim_token=active.claim_token, keys=keys
            )
        )

    def _save(self) -> None:
        session = self._session
        active = self._tab.active_case()
        if session is None or active is None or session.saving:
            return
        if self._tab.capture_busy() or active.paused or not session.keys:
            return
        if active.claim_token != session.claim_token:
            return
        outcome = self._sheet.outcome()
        note = self._sheet.note()
        if outcome is ManualStepOutcome.FAIL and not note.strip():
            return
        if len(note.strip()) > MANUAL_STEP_NOTE_MAX_CHARS:
            return
        hand_in = self._sheet.hand_in()
        if hand_in is not None and not self._check_hand_in(
            session, outcome, note, hand_in
        ):
            return
        self._tab.keep_step_note(session.step_num, note)
        keys = tuple(session.keys)
        if session.original == (outcome, keys) and hand_in is None:
            # Edit… with only the note changed: the mark stays as it is.
            self.close(focus_back=True)
            self._tab.render()
            return
        session.saving = True
        session.save_timed_out = False
        session.status = ""
        session.hand_in = hand_in
        session.outcome = outcome
        self._save_job = self._root.after(SAVE_TIMEOUT_MS, self._save_timeout)
        self._tab.render()
        self._window.manual_send(
            ManualMarkCommitEvent(
                pivot_id=active.pivot_id,
                claim_token=active.claim_token,
                step_num=session.step_num,
                outcome=outcome,
                keys=keys,
            )
        )

    def _check_hand_in(
        self,
        session: _SheetSession,
        outcome: ManualStepOutcome,
        note: str,
        hand_in: HandIn,
    ) -> bool:
        """The hand-in rules for the marks as they will be after Save; asks
        inline when the verdict contradicts the steps. True to go on."""
        failed = sorted(
            {num for num in self._tab.failed_steps() if num != session.step_num}
            | ({session.step_num} if outcome is ManualStepOutcome.FAIL else set())
        )
        texts = self._tab.draft_texts()
        notes = dict(texts.notes)
        notes[session.step_num] = note
        texts = ManualDraftTexts(
            notes={num: text for num, text in notes.items() if text},
            seconds=texts.seconds,
            verdict=hand_in.verdict,
            verdict_note=hand_in.note,
        )
        message, _step = self._tab.validation_error(texts, failed)
        if message:
            session.status, session.status_color = message, ERROR
            self._tab.render()
            return False
        contradicts = (
            hand_in.verdict is ManualCaseVerdictWire.PASSED and bool(failed)
        ) or (hand_in.verdict is ManualCaseVerdictWire.FAILED and not failed)
        if contradicts and not self._sheet.confirming:
            if hand_in.verdict is ManualCaseVerdictWire.PASSED:
                steps = ", ".join(str(num) for num in failed)
                text = (
                    f"Step(s) {steps} failed, but you chose Passed for the case. "
                    "Hand it in as Passed?"
                )
            else:
                text = "No step failed, but you chose Failed for the case. Hand it in as Failed?"
            self._sheet.ask_confirm(text)
            return False
        return True

    def _cancel_save_timer(self) -> None:
        if self._save_job is not None:
            self._root.after_cancel(self._save_job)
            self._save_job = None

    def _save_timeout(self) -> None:
        """The runner never answered Save (it dropped the request): Save and
        Cancel work again."""
        self._save_job = None
        session = self._session
        if session is None or not session.saving:
            return
        session.saving = False
        session.save_timed_out = True
        session.status, session.status_color = (
            "The Test Runner did not confirm the mark yet. Save again.",
            ERROR,
        )
        self._tab.render()

    def on_mark_result(self, ok: bool, message: str) -> None:
        """The runner's answer to Save."""
        session = self._session
        if session is None or not (session.saving or (ok and session.save_timed_out)):
            return
        # Confirmed after SAVE_TIMEOUT_MS: the sheet was editable again
        # meanwhile, so the step is recorded but nothing is handed in and the
        # tab does not move on (the tester decides from the tab).
        late = not session.saving
        self._cancel_save_timer()
        session.saving = False
        session.save_timed_out = False
        if not ok:
            session.status, session.status_color = (
                message or "The mark was not saved.",
                ERROR,
            )
            self._tab.render()
            return
        step_num = session.step_num
        self._tab.record_mark(
            ManualMarkView(
                step_num=step_num, outcome=session.outcome, keys=tuple(session.keys)
            )
        )
        hand_in = session.hand_in
        edit = session.edit
        if late:
            self._tab.keep_step_note(step_num, self._sheet.note())
            current = self._sheet.hand_in() if hand_in is not None else None
            self.close(focus_back=False)
            if current is not None:
                self._tab.set_case_result(current.verdict, current.note)
                self._tab.show_hand_in_error("Step saved. Hand in from the bar.", None)
            self._tab.render()
            return
        self.close(focus_back=False)
        if hand_in is not None:
            self._hand_in(hand_in)
        elif not edit:
            following = self._next_unmarked(step_num)
            if following is not None:
                self._tab.go_to(following)
        self._tab.render()
        self._window.manual_focus_back()

    def _hand_in(self, hand_in: HandIn) -> None:
        """The case result chosen in the sheet, then the tab's hand-in."""
        active = self._tab.active_case()
        if active is None:
            return
        self._tab.set_case_result(hand_in.verdict, hand_in.note)
        texts = self._tab.draft_texts()
        message, step = self._tab.validation_error(texts)
        if message:
            # The mark is saved; the tester fixes the rest in the tab.
            self._tab.show_hand_in_error(message, step)
            return
        if self._tab.submit(texts):
            self._auto_claim.begin(active.pivot_id)

    def _next_unmarked(self, after: int) -> int | None:
        """The next step left to mark after ``after``, else the first one."""
        active = self._tab.active_case()
        if active is None:
            return None
        marks = self._tab.step_marks()
        unmarked = [s.num for s in active.steps if s.num not in marks]
        later = [num for num in unmarked if num > after]
        if later:
            return later[0]
        return unmarked[0] if unmarked else None

    def _cancel(self) -> None:
        session = self._session
        if session is None or session.saving:
            return
        # The step stays as it was: its staged screenshots go, its note was
        # never touched. A screenshot still being taken is thrown away when
        # it arrives.
        self._discard(tuple(key for key in session.keys if key in session.staged))
        self.close(focus_back=True)
        self._tab.render()

    def close(self, *, focus_back: bool) -> None:
        self._cancel_save_timer()
        self._session = None
        self._sheet.close()
        if focus_back:
            self._window.manual_focus_back()

    def preview(self) -> bool:
        """``--self-check``: open the sheet on the shown step without a
        screenshot, switch it to Fail and close it (nothing is sent). False
        when no case is claimed."""
        active = self._tab.active_case()
        if active is None or self._tab.shown_step() is None:
            return False
        keys = next((list(mark.keys) for mark in self._tab.step_marks().values()), [])
        self._open(
            active,
            ManualStepOutcome.PASS,
            keys=keys,
            staged=set(),
            edit=False,
            focus=False,
        )
        self._root.update()
        self._sheet.set_outcome(ManualStepOutcome.FAIL)
        self._tab.render()
        self._root.update()
        self.close(focus_back=False)
        self._tab.render()
        return True
