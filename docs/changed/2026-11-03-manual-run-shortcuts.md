# Manual run shortcuts and mark sheet

Date: 2026-11-03
Plan: docs/plan/feat-manual-run-shortcuts.md

## What changed
- `controller_client/manual_hotkeys.py`, `manual_hotkeys_base.py` (new): `ManualHotkeys` facade (queue drained by the window's `poll`), `HotkeyBackend` contract, `probe_shortcuts`.
- `controller_client/manual_hotkeys_windows.py` (RegisterHotKey on its own message thread), `manual_hotkeys_macos.py` (Carbon RegisterEventHotKey on the Tk thread, no permission), `manual_hotkeys_linux.py` (session-type selection), `manual_hotkeys_portal.py` (Wayland GlobalShortcuts portal via `dbus-fast`, Registry id `com.punkhazard.TestRunner` + user `.desktop` file), `manual_hotkeys_x11.py` (XGrabKey via ctypes, `prepare_x11_threads` before Tk opens) (all new).
- `controller_client/person_window_mark_sheet.py`, `person_window_mark_session.py` (new): the inline mark sheet (note, Add / Retake / Remove up to `step_max_shots`, last-step "Hand in as", Fail "Stop here", Save Ctrl/Cmd+Enter, Cancel Esc), its session logic and the auto-claim of the next case.
- `controller_client/person_window_manual.py`: `on_shortcut` gate (claimed, not paused, step shown, not busy, window not hidden by the runner), Pass…/Fail… open the sheet, Retake → Edit…, shortcut status line, focus-only `bind_all` fallback, several thumbnails per step, 30 s timeouts for an unanswered Save or hand-in.
- `controller_client/person_window_app.py`: creates `ManualHotkeys`, enables it while `shortcuts_wanted()`, focus to the sheet and back to the previous app (only after a system-wide press while another app was in front), `--probe-shortcuts [--interactive]`, `--no-manual-shortcuts`; `person_window_front_app.py`: `restore_now()`.
- `controller_client/manual_spool.py` (draft format v2: marks are tuples, staged shots, `commit_mark`, `discard_staged`, `drop_staged`; v1 drafts still read) and `manual_coordinator.py` (staged captures, `_commit_mark`, `_discard`, multi-key hand-in, staged shots dropped on window restart via `ReadyEvent`).
- `controller_client/person_window_ipc.py`: `ManualCaptureEvent.staged`, `ManualMarkCommitEvent`, `ManualMarkDiscardEvent`, `ManualResultKind.MARK`, `ManualMarkView.keys`. `person_window.py` routes them and passes `--no-manual-shortcuts`; `config.py` / `example.env` / `main.py`: `CONTROLLER_MANUAL_SHORTCUTS`.
- `controller_client/protocol.py`: `MANUAL_STEP_MAX_SHOTS` 1 → 5, `ManualActiveCasePayload.step_max_shots`, CLIENT_VERSION 0.17.0.
- Server: `projects/runner_manual_state_services.py` sends `step_max_shots`; `manual_case_submit_services.py` stores `attempt_key` per manifest entry; `manual_screenshot_services._store` links every step shot to the step's attempt.
- Diagnostics: "Manual shortcuts" step in `diagnose --window` (a warning, never a failure); the self-check renders the sheet.

## Why
Manual testers wanted to mark a step without leaving the app under test: one key takes the screenshot, a small sheet takes the note and extra shots, and Save moves on to the next step and then the next case.

## Behaviour and contract changes
- Controller protocol 0.17.0, no new capability: `manual_run_state.active.step_max_shots` (absent = 1); hand-ins may carry up to 5 screenshots per step. Deploy the server first, then update runners.
- Window IPC: `manual_capture.staged`, `manual_mark_commit` (answered by `manual_result` kind `mark`), `manual_mark_discard`; draft marks carry `keys`.
- Runner setting `CONTROLLER_MANUAL_SHORTCUTS` (default true). The combinations are swallowed system-wide only while a case is claimed and not paused.
- Linux Wayland writes `~/.local/share/applications/com.punkhazard.TestRunner.desktop` once (Exec limited to `[A-Za-z0-9._/+-]`, else `true`).
- No migration, URL or server setting.

## Notes for future changes
- Not yet tried on real machines: a real key press on macOS (registration works under Tk here), Windows (incl. an elevated app in front), GNOME 48+/KDE 6 portal, X11. If the portal re-prompts on every new session, set `KEEP_SESSION_OPEN = True` in `manual_hotkeys_portal.py`.
- The coordinator allows `step_max_shots + 1` staged shots for one step so Retake works on a full sheet; Save keeps at most `step_max_shots`. `commit_mark` also accepts keys already in the step's mark (Edit…).
- A Save confirmed after its 30 s timeout records the step but never hands in or advances (the sheet was editable again); a hand-in confirmed late shows "Handed in." with no auto-claim.
- macOS uses physical ANSI key positions (P / F on a US layout).
