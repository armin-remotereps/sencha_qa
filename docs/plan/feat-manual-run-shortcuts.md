# feat: Manual run keyboard shortcuts with a mark sheet

Status: Done
Created: 2026-11-03
Request: In the Test Runner's Manual run tab, the tester gets two system-wide
shortcuts while performing a claimed case: Cmd+Shift+P (macOS) / Ctrl+Shift+P
(Windows, Linux) for Pass and Cmd/Ctrl+Shift+F for Fail.

- A shortcut first takes a screenshot, using the existing capture path that
  hides the window and waits for the hide to be confirmed.
- It then opens a small "mark sheet" in the window. There the tester can type
  a note (required for a Fail), add extra screenshots or retake one.
- Save records the mark and moves straight on to the next step.
- After the last step, Save hands the case in and starts the next case, so the
  window shows the next case.

## Decisions (answered 2026-11-03)

- **D1 Per-step.** Save moves to the next unmarked step. When no unmarked step
  is left, the sheet also shows "Hand in as" (prefilled: Passed if every step
  passed, Failed if any failed) and an optional case note, with "Hand in and
  start the next case" ticked. Save then hands the case in and claims the next
  To-do case. A one-step case behaves like a per-case verdict.
- **D2 Up to 5 screenshots per step.** This needs a protocol change and a
  server change.
  - The server advertises the cap as `step_max_shots` in its snapshot. A
    runner never sends more shots than the snapshot allows, and without the key
    it sends one.
  - The server is deployed first.
- **D3 System-wide on Linux too, in this change.**
  - Wayland: xdg-desktop-portal GlobalShortcuts (GNOME 48+, KDE Plasma 6).
    The compositor's one-time permission prompt is accepted.
  - X11: `XGrabKey` through ctypes on libX11.
  - Fallback when neither works: the shortcuts work only while the window has
    focus, and the tab says why.
  - There is a diagnose check.
  - **No new dependency**: the portal uses `dbus-fast`, already a Linux
    requirement (`dbus-fast~=5.2.0; sys_platform != "win32"`, typed, used by
    `gnome_remote_session.py`), and X11 uses ctypes.
- **D4 Swallow the combinations**, only while a case is claimed and not paused.
  The Linux portal is subject to a compositor caveat (Risks).
- **D5 After a failed step,** Save continues to the next step. The Fail sheet
  has an unticked "Stop here and hand the case in as Failed".
- **D6** The tab's Pass/Fail buttons open the same sheet, and their labels
  show the combination. The tab's Retake becomes "Edit…", which reopens the
  sheet for a marked step with no new capture and does not advance after Save.
- **D7 Cancel** discards the screenshots staged in the sheet and leaves the
  step exactly as it was before the shortcut.
- **D8 Fixed combinations.** `CONTROLLER_MANUAL_SHORTCUTS` (default on) is the
  only switch. On the portal the user may rebind the shortcuts in the
  compositor's dialog; the tab then shows the triggers it reports.

## Context

- **Manual run tab** (`controller_client/person_window_manual.py`, `ManualView`):
  - Step "Pass (screenshot)" / "Fail (screenshot)" call `_capture_step` →
    `ManualCaptureEvent`. The mark is created when the capture succeeds, with
    one screenshot per step. Retake replaces it.
  - Notes are window texts (`ManualDraftTexts.notes`, `_schedule_draft`).
  - Navigation: `_move` / `_go_to`.
  - Hand in: `_ask_submit` → `_validation_error` → `_submit`.
  - Start: `_render_cases` preselects the first To-do case; `_start` claims it.
  - Pause disables captures and the hand-in (`_paused`).
- **Window process** (`controller_client/person_window_app.py`):
  - Tk owns the main thread. A stdin reader thread feeds a `queue.Queue`, and
    `poll()` drains it every `_POLL_MS`.
  - Invariants: the window never takes focus, is never topmost and opens no
    other window or dialog, so hiding the root hides everything.
  - Windows shows non-activating (`SW_SHOWNOACTIVATE`). On macOS a map
    activates the Python app, and `FrontAppKeeper`
    (`person_window_front_app.py`) gives the front back on hide.
  - While a manual run shows, closing the window minimizes it.
  - On Linux, Tk runs as an X client (XWayland under GNOME Wayland).
- **Runner process:**
  - `person_window.py`: `hidden_for_capture()` is a confirmed hide, then the
    capture, then an immediate show. `hidden_for_action()` hides the window
    for every agent action (`client._dispatch` → `_window_hidden`).
  - `manual_capture.py`: `ManualCapturer.capture`, serialized.
  - `manual_coordinator.py`: `_capture`, `_save_texts`, `_submit`,
    `build_submit_payload`, progress reports.
  - `manual_spool.py`: `ManualDraft.marks: Mapping[int, SpoolShot]`.
    `add_shot` replaces the step's shot and deletes the old file. Draft format
    version 1.
  - `client.py`: `_forward_manual_requests` sends claim and release to the
    server and everything else to the coordinator.
  - `manual_uploader.py` sends spooled hand-ins.
  - Linux desktop: `desktop_gnome.py` and `gnome_remote_session.py`. The latter
    uses `dbus_fast.aio.MessageBus` with low-level `Message` calls.
- **Protocol** (`controller_client/protocol.py`):
  - `CLIENT_VERSION = "0.16.0"`.
  - `MANUAL_STEP_MAX_SHOTS = 1`. `ManualStepResultPayload.shots` is already a
    tuple.
  - `_check_manual_manifest` and `parse_manual_case_submit_payload` run on
    both sides, and a refusal drops the spooled hand-in.
- **Server:**
  - `projects/manual_case_claim_services.py`: one claim per run
    (`_OTHER_CLAIMED` until the hand-in lands).
  - `manual_case_submit_services._with_manual_attempts`: a step's attempt id is
    `manual_attempt_id(step.shots[0])`.
  - `manual_screenshot_services._store`: a step shot's `attempt_id` is
    `manual_attempt_id(payload.key)`, which assumes one shot per step.
  - `manual_case_display_services._group_shots` and
    `testrail_evidence.select_manual_evidence` already group several shots per
    step.
- **Agent input:**
  - In a Manual run, agent input comes only from Machine setup, and
    `run_setup_task` is refused while a claimed case is not paused
    (`docs/changed/2026-11-01-setup-during-manual-run.md`).
  - `input_guard.py` guards the runner's own injection; it is unaffected.

## Goals

- Pass/Fail shortcuts that work system-wide on Windows, macOS and Linux (portal
  on Wayland, XGrabKey on X11) while a manual case is claimed and not paused.
  Focus-only is the stated fallback.
- A shortcut takes a confirmed-hide screenshot, then opens the sheet. Save
  advances; after the last step it hands in and claims the next case.
- Up to 5 screenshots per step, with older servers and runners unaffected.
- No new third-party dependency, so `controller_client/requirements.txt` does
  not change.

## Non-goals

- A Blocked verdict from a shortcut (it stays in the tab's Case result).
- User-defined combinations in the runner.
- Shortcuts for Human in the loop steps or outside the Manual run tab.
- Any change to TestRail text, the AI rewrite or hand-in judgement, beyond
  accepting several screenshots per step.

## Design

### Data model

No database migration.

**Wire protocol** (`controller_client/protocol.py`, shared with the server):

- `MANUAL_STEP_MAX_SHOTS` 1 → 5. `MANUAL_SUBMISSION_MAX_SHOTS` follows (501).
  - This relaxes the parser, so one-shot hand-ins from older runners still
    parse.
  - Add an assert that the manifest still fits in
    `MANUAL_CASE_SUBMIT_MAX_BYTES`.
- `ManualActiveCasePayload.step_max_shots: int = 1`, serialized and parsed,
  clamped to 1..`MANUAL_STEP_MAX_SHOTS`. When the key is absent (an older
  server), the value is 1.
- `CLIENT_VERSION` 0.16.0 → **0.17.0**, so `runner_update_offer` offers the
  update.
- **No new `ClientCapability`.** No server behaviour depends on the runner
  having shortcuts, and older runners ignore `step_max_shots`.

**Server-stored JSON:** at hand-in, every stored
`ManualCaseSubmission.shot_manifest` entry gains `attempt_key`, the first
screenshot key of its step taken from `payload.steps`. Older entries lack it,
and `_store` then falls back to the shot's own key, which is today's
behaviour.

**Runner spool** (`manual_spool.py`, draft `_FORMAT_VERSION` 2):

- `marks: Mapping[int, tuple[SpoolShot, ...]]`.
- `staged: tuple[SpoolShot, ...]`: shots of an open sheet that are not saved
  yet.
- Version 1 drafts load as one-shot marks.
- `commit()` deletes staged files before the folder moves to the outbox.

### Services

All modules are runner-side unless marked server.

**Hotkey layer** (window process; all backends have the same shape):

- `controller_client/manual_hotkeys.py` (new, shared core):
  - `HotkeyKind` (PASS, FAIL).
  - `HotkeyStatus`: state `active | focus_only | off | pending`, a user-safe
    `reason`, and `triggers: dict[HotkeyKind, str]` (the labels to show).
  - `HotkeyBackend` Protocol: `enable()`, `disable()`, `close()`, `status`.
    Calls are idempotent and never raise.
  - `ManualHotkeys`: owns a thread-safe `queue.Queue[HotkeyKind]`
    (`drain()`), picks the backend (`win32` / `darwin` /
    `manual_hotkeys_linux.select_backend`) and exposes `status`.
  - `probe_shortcuts(root) -> HotkeyStatus`, used by `--probe-shortcuts`.
  - Every backend only puts events on the queue. `PersonWindowApp.poll()`
    drains it on the Tk thread, so widgets are touched from one thread only.
- `controller_client/manual_hotkeys_windows.py` (new):
  - `RegisterHotKey(NULL, id, MOD_CONTROL|MOD_SHIFT|MOD_NOREPEAT, 'P'/'F')`
    on a dedicated thread running a `GetMessageW` loop.
  - Enable and disable are `PostThreadMessageW(WM_APP+n)`: NULL-hwnd hotkeys
    belong to the thread that registered them.
  - `WM_HOTKEY` → queue. `ERROR_HOTKEY_ALREADY_REGISTERED` → focus_only with
    reason "Ctrl+Shift+P is used by another app".
  - The registration is exclusive, so the combination is swallowed.
- `controller_client/manual_hotkeys_macos.py` (new):
  - Carbon `RegisterEventHotKey` (kVK_ANSI_P / kVK_ANSI_F, `cmdKey|shiftKey`)
    plus `InstallEventHandler(GetApplicationEventTarget(), …)` via ctypes on
    `Carbon.framework`.
  - Called on the Tk main thread, and the handler runs there.
  - Needs no Accessibility or Input Monitoring permission, and swallows the
    combination.
  - `eventHotKeyExistsErr` → focus_only.
  - Keep a module-level reference to the `CFUNCTYPE` callback.
  - Key codes are physical ANSI positions; the tab hint says so.
- `controller_client/manual_hotkeys_linux.py` (new):
  `select_backend(queue, x11_window_id) -> HotkeyBackend`.
  - The session type comes from `XDG_SESSION_TYPE`, else from
    `WAYLAND_DISPLAY` / `DISPLAY`.
  - `wayland`: `PortalHotkeys`. If the portal is missing or binding fails,
    focus_only with the reason.
  - `x11`: `X11Hotkeys`. If libX11 is not found or the grab fails, focus_only.
  - Anything else: focus_only, "No global shortcut service on this desktop".
  - Never use XGrabKey on Wayland: XWayland grabs see only X clients' keys.
- `controller_client/manual_hotkeys_portal.py` (new, Wayland,
  `org.freedesktop.portal.GlobalShortcuts` v1/v2):
  - **Thread:** a daemon thread `person-window-portal` with its own asyncio
    loop and a private `dbus_fast.aio.MessageBus` (session bus). Low-level
    `Message` calls, matching `gnome_remote_session.py`. The Tk thread calls
    in with `asyncio.run_coroutine_threadsafe`.
  - **App id:**
    - Once per connection and before any other portal call,
      `org.freedesktop.host.portal.Registry.Register("com.punkhazard.TestRunner",
      {})` (xdg-desktop-portal ≥ 1.19/1.20). The id must match a `.desktop`
      basename.
    - So the backend first ensures
      `~/.local/share/applications/com.punkhazard.TestRunner.desktop` exists:
      written atomically if missing, `NoDisplay=true`, `Name=Test Runner`,
      `Exec=` the managed `start-test-runner.sh` when present
      (`update_layout`), else `true`.
    - If the Registry interface is missing, it goes on: the portal derives the
      id from the systemd scope, and the status notes it when binding then
      fails.
  - **Enable:**
    1. `CreateSession({handle_token, session_handle_token})`, then wait for
       `Request.Response`.
    2. `BindShortcuts(session, [("pass", {description: "Pass the current
       step", preferred_trigger: "CTRL+SHIFT+p"}), ("fail", {…,
       "CTRL+SHIFT+f"})], parent_window, {handle_token})`.
       `parent_window = "x11:<hex of Tk wm_frame()>"`, because Tk is an X
       client.
    3. Response code 0 → `active`, with `trigger_description` per shortcut as
       the triggers. Code 1 (the user denied) → focus_only "Shortcut permission
       was denied in the desktop's dialog". A shortcut missing from the
       response → focus_only for that kind.
    - The `Activated(session, id, …)` signal → queue. `Deactivated` is
      ignored.
    - A session binds only once (spec), so each enable is a new session.
  - **Disable:** `org.freedesktop.portal.Session.Close` on the session, which
    releases the grab.
  - **Re-prompt guard:** if the compositor shows its dialog again on every new
    session (verify on GNOME 50 and Plasma 6), the backend switches to
    keep-open mode. The session stays open from the first enable until the
    window process ends, and `disable()` only makes it ignore activations.
    The combinations then stay swallowed while the window lives (Risks; the
    tab says so).
  - **Timeouts:** each portal round trip is bounded at 60 s, since the user
    may sit in the dialog. Status is `pending`, shown as "Approve the
    shortcuts in the desktop's dialog…".
- `controller_client/manual_hotkeys_x11.py` (new, X11):
  - ctypes on `ctypes.util.find_library("X11")`, with `XInitThreads()` before
    anything else.
  - A dedicated thread with its own `XOpenDisplay(None)`.
  - Key codes come from `XKeysymToKeycode(XK_p / XK_f)`.
  - `XGrabKey` on the root window for `ControlMask|ShiftMask` and each
    combination with `LockMask` and `Mod2Mask` (Caps Lock and Num Lock),
    `GrabModeAsync`.
  - Errors are trapped by a temporary `XSetErrorHandler` plus `XSync`.
    `BadAccess` → focus_only "Ctrl+Shift+P is used by another app".
  - The loop runs `select()` on `ConnectionNumber(display)` plus a wake pipe,
    and drains `XPending` / `XNextEvent`. `KeyPress` → queue.
  - Enable, disable and close go through the wake pipe. `XUngrabKey` and
    `XCloseDisplay` run on that thread.
  - The grab swallows the combination.
- Focus-only fallback (all platforms): `ManualView` binds
  `<Control-Shift-KeyPress-P>` / `-F` (macOS `<Command-Shift-…>`) with
  `bind_all` while the backend status is not `active`.

**Spool / coordinator / manager:**

- `controller_client/manual_spool.py`:
  - `add_shot(..., staged=True)`.
  - `commit_mark(draft, step_num, kind, keys)`: the staged keys become the
    step's mark. The shots it replaces and the step's other staged shots are
    deleted.
  - `discard_staged(draft, keys)`.
  - `drop_staged(draft)`, called on load.
  - Read version 1 drafts.
- `controller_client/manual_coordinator.py`:
  - `_capture` honours `staged` and refuses a staged capture past
    `active.step_max_shots`.
  - New `_commit_mark` checks the claim, that the case is not paused, and that
    the keys are staged for that step with count ≤ `step_max_shots`. Then it
    commits, schedules progress and pushes the view.
  - New `_discard`.
  - `build_submit_payload`: all keys of each mark in order, each announced in
    `shots`.
  - `build_progress_payload` and `_draft_view` work on tuples.
  - Staged shots are dropped when a draft loads.
- `controller_client/person_window.py`: `ManualWindowRequest` and the `_serve`
  routing take the new events. The spawn args add `--no-manual-shortcuts` when
  config turns the shortcuts off.
- `controller_client/config.py` + `controller_client/example.env`:
  `CONTROLLER_MANUAL_SHORTCUTS` (bool, default true).

**Server (D2):**

- `projects/runner_manual_state_services.py`:
  `active.step_max_shots = MANUAL_STEP_MAX_SHOTS`.
- `projects/manual_case_submit_services.py`: writes `attempt_key` into every
  stored manifest entry.
- `projects/manual_screenshot_services._store`: step shots get
  `attempt_id = manual_attempt_id(entry.get("attempt_key") or payload.key)`.
- Nothing else changes on the server. The case card and TestRail already
  group several shots per step.

### Pages and forms

There is no Django page, URL or form change.

Window (Tk), Manual run tab:

| Surface | Trigger | Behaviour | Errors |
| --- | --- | --- | --- |
| Shortcut Pass / Fail | Backend hotkey; or `bind_all` when the status is not `active` | Acts only when all hold: a case is claimed, not paused, the shown step exists, no capture is in flight, no sheet is open, the spool is not full, and the window is not hidden by the runner. Then a staged capture of the shown step runs and the sheet opens. | Capture refused or hide not confirmed: no sheet; message in `_capture_status` |
| Mark sheet (new `person_window_mark_sheet.py`, `MarkSheet`) | First staged capture succeeds; or "Edit…" | An inline overlay frame placed over the root, never a `Toplevel`. Contents: <br>• Pass/Fail switch and the step title <br>• thumbnails of this sheet's shots, each with Retake and Remove <br>• "Add screenshot (n of max)" <br>• the note: prefilled, required for Fail <br>• last step (D1): "Hand in as" with Passed/Failed/Blocked prefilled, an optional case note and "Hand in and start the next case" (ticked) <br>• Fail (D5): "Stop here and hand the case in as Failed" (unticked) <br>• Save (Ctrl/Cmd+Enter) and Cancel (Esc) | Save is disabled for a Fail with no note, or while a capture runs. Hand-in rules come from `_validation_error`, with an inline confirmation when the verdict contradicts the steps |
| Add screenshot / Retake | Button in the sheet | A staged capture. The hide covers the whole window and the sheet. Retake discards the replaced staged key once the new shot succeeds. | As for capture |
| Pass / Fail buttons (D6) | Click | Same as the shortcut; labels show the active triggers | — |
| Shortcut status line | Render | Shows one of: <br>• "Shortcuts: ⌘⇧P pass · ⌘⇧F fail" (or the portal's trigger descriptions) <br>• "Approve the shortcuts in the desktop's dialog…" <br>• "Shortcuts work only while this window is focused: <reason>" <br>• nothing when off | — |
| Auto-claim (D1) | After a sheet hand-in | "Handed in. Starting the next case…". Waits until the snapshot no longer shows the handed-in case: it is not in `submitted_pivots` and its status is not `in_progress`. Then `_start()` claims the preselected To-do case. With no To-do case left: "No case left to do in this run." Cancelled by any action in the case list, or after `_AUTO_CLAIM_WAIT_S` (120 s), with a hint to press Start. | Claim refused: the server's message in `_list_status`; no retry |

**Focus:**

- Opening the sheet activates the window. This is the one exception to "never
  takes focus", and it is always triggered by the tester's own key or click.
  - Windows: `SetForegroundWindow` (the receiver of `WM_HOTKEY` may take the
    foreground; verify).
  - macOS: the map activates the window.
  - Linux: `focus_force()` after the `Activated` signal or the X11 key event.
    The portal's `activation_token` is not usable from Tk; GNOME/KDE focus
    stealing prevention may only flash the window, and the tester then clicks
    it.
  - In every case the note gets `focus_set()`.
- The window records the previous foreground window or app when the shortcut
  fires, and gives the focus back after Save or Cancel.
  - Windows: `SetForegroundWindow(previous)`.
  - macOS: new `FrontAppKeeper.restore_now()`.
  - Linux: best effort. With X11, `XSetInputFocus` on the recorded
    `_NET_ACTIVE_WINDOW` is skipped in v1; the tester clicks back.

### Background work and WebSockets

- No Celery task, consumer, socket event or controller action is added.
- Wire changes are only `step_max_shots` and the relaxed shot count (D2).
- Window IPC (`controller_client/person_window_ipc.py`):
  - `ManualCaptureEvent.staged: bool` (default false).
  - `ManualMarkCommitEvent(pivot_id, claim_token, step_num, outcome, keys)`,
    answered with `ManualResultCommand(kind=ManualResultKind.MARK)`.
  - `ManualMarkDiscardEvent(pivot_id, claim_token, keys)`, with no reply.
  - `ManualMarkView.key` → `keys: tuple[str, ...]`.
  - The window and the runner ship together, so no IPC versioning is needed.

### Settings / infrastructure

- Runner env `CONTROLLER_MANUAL_SHORTCUTS` (default `true`), read in
  `controller_client/config.py` and added to `controller_client/example.env`.
  There is no server setting.
- No dependency change:
  - Windows uses user32 through ctypes.
  - macOS uses Carbon through ctypes.
  - Linux uses the existing `dbus-fast` (typed; already imported without
    ignores in `gnome_remote_session.py`) and libX11 through ctypes.
- No stubs are needed. mypy strict: platform modules are guarded with
  `sys.platform` checks (as `person_window_app` does for `ctypes.windll`), and
  the ctypes prototypes declare `argtypes` / `restype`.
- The portal writes one user file:
  `~/.local/share/applications/com.punkhazard.TestRunner.desktop`. It is
  idempotent, written only on Wayland when shortcuts are on, and never
  deleted.
- **Diagnostics:**
  - `python -m controller_client.person_window_app --probe-shortcuts
    [--interactive]` opens a withdrawn Tk, builds `ManualHotkeys`, enables,
    reports the status and disables.
    - Windows, macOS and X11: register and unregister only.
    - Portal without `--interactive`: checks that the GlobalShortcuts and
      Registry interfaces and their versions are present, and calls
      `ListShortcuts` on a fresh session (no dialog). It reports earlier
      bindings.
    - Portal with `--interactive`: also binds, which shows the dialog, and
      waits up to 20 s for the tester to press Ctrl+Shift+P.
  - `person_window_diagnostics.build_window_steps` adds a "Manual shortcuts"
    step. `diagnose --window` passes `--interactive` unless `--no-interactive`
    is given.
  - Results: `active` = pass; focus_only or off = warning with the reason
    (not a failure).
  - `--self-check` also renders the sheet on the sample manual state.

### Data flow

Pass on a middle step:

1. The backend puts `PASS` on the queue: Windows via the hotkey thread, macOS
   via the Carbon handler, Linux via the portal thread or the X11 thread.
2. `poll()` calls `ManualView.on_shortcut(PASS)`. The gate holds, so the view
   records the previous foreground and sends
   `ManualCaptureEvent(staged=True, PASS, shown step)`.
3. The runner's coordinator runs `ManualCapturer.capture()`: confirmed hide,
   grab, show. The shot is staged in the spool and returned with
   `show_capture_result(key, thumbnail)`.
4. The window opens `MarkSheet` and focuses the note. The tester may add or
   retake screenshots, which are more staged captures.
5. Save:
   - The note is written to `_notes[step]` and the draft is flushed.
   - `ManualMarkCommitEvent` → the coordinator commits.
   - The window closes the sheet, calls `_go_to(next unmarked)` and gives the
     focus back.
6. Last unmarked step (or "Stop here" on a Fail): after the commit result, the
   window builds `_texts()` with the sheet's verdict and case note.
   `_validation_error` runs, then `_submit` (the spooled hand-in), then
   auto-claim. The next case's steps arrive in the next snapshot.
7. Cancel: `ManualMarkDiscardEvent`, the note text is restored, the sheet
   closes and the focus goes back. The step is unchanged.

**Registration:** after every manual render, the app calls `enable()` when
`manual.shortcuts_wanted()` (a claimed case, not paused) holds, else
`disable()`.

- Transient states (capture in flight, sheet open, window hidden by the
  runner) keep the hotkeys registered but ignore them, so the combinations
  never reach the app under test mid-capture.
- Agent actions only happen with no claim or a paused case, where the hotkeys
  are released. The runner also hides the window for every agent action, and
  a hidden window ignores hotkeys. So injected keys never mark a step.

## Alternatives considered

- **pynput** (1.8.2): rejected.
  - It is a new dependency.
  - On macOS it needs Input Monitoring or Accessibility.
  - There are reported SIGTRAP crashes when its listener shares a process with
    Tk (pynput #596).
  - On macOS 26, HIToolbox input-source calls off the main thread trap, and
    pynput's listener thread makes them.
  - On Windows, UIPI hides keys from its low-level hook while an elevated
    window is in front.
  - It does not work on Wayland, and its uinput backend needs root.
- **Hotkeys in the runner process** (it already has an asyncio loop and
  dbus-fast): rejected.
  - On macOS a hotkey needs a Cocoa run loop, which the window process has.
  - The window owns the shown step, the sheet and the capture trigger.
  - The portal's `parent_window` is the Tk window.
  - Keeping every backend in one process gives a single queue and a single
    gate.
- **jeepney / dbus-next for the portal**: rejected; `dbus-fast` is already
  pinned on Linux and typed.
- **python-xlib for X11**: rejected; it is a new dependency, and ctypes on
  libX11 covers the six calls needed.
- **A GNOME custom keybinding via gsettings that runs a command**: rejected.
  It changes the user's desktop settings, it is GNOME-only and it needs IPC
  back to the window.
- **evdev / uinput**: rejected; it needs the `input` group or root, and it is
  effectively a keylogger.
- **CGEventTap on macOS**: kept as the fallback if Carbon under Tk fails the
  step-A spike. It needs Accessibility, which the runner already needs for
  pyautogui. It matches key codes and flags only, with no layout APIs.
- **A `Toplevel` dialog**: rejected; it breaks the "hiding the root hides
  everything" rule the capture relies on.
- **Mark immediately, and let Cancel only close the sheet**: rejected by D7.

## Risks and mitigations

- **Carbon under Tk's Cocoa loop is unproven.** Step A begins with a spike
  behind `--probe-shortcuts`. If it fails, fall back to CGEventTap, else
  focus-only on macOS.
- **Portal behaviour differs by compositor.**
  - Re-prompt per session: mitigated by keep-open mode, at the cost of the
    combinations being swallowed while paused (D4 caveat, stated in the tab).
  - App id attribution without the Registry interface: mitigated by the
    `.desktop` file and by Register run first.
  - The user rebinding or denying in the dialog: the tab shows the reported
    triggers or the reason, and the focus-only bindings still work.
  - Verify on Ubuntu 26.04 (GNOME 50) and on KDE Plasma 6.
- **The X11 grab is taken by another client:** BadAccess → focus-only with the
  reason.
- **Focus on Linux Wayland:** the window may not come to the front after a
  global shortcut. The sheet still opens and the tester clicks into it.
- **Foreground rules on Windows:** expected to be allowed after `WM_HOTKEY`.
  Verify, also with an elevated app in front.
- **Hide not confirmed:** no screenshot and no sheet, as today.
- **Protocol relax:**
  - The runner only sends more than one shot per step when the snapshot
    allows it.
  - Deploy order: migrate nothing, then deploy the server, then update the
    runners.
  - The manifest `attempt_key` fallback keeps older hand-ins linked.
- **Auto hand-in surprise:** the last-step block in the sheet is explicit and
  can be unticked. Validation and the inline confirmation still apply.
- **Auto-claim race with `_OTHER_CLAIMED`:** the window waits for the
  snapshot, gives up after 120 s and cancels on any tester action.
- **Staged shots** count against `CONTROLLER_MANUAL_SPOOL_MAX_MB`. They are
  dropped on Cancel, on window or draft reload and at hand-in.
- **Key repeat:** `MOD_NOREPEAT`. The X11 and portal paths act on press only.
  The view ignores shortcuts while busy.
- **Privacy:** every backend delivers only its own two combinations. No
  key-stream access.

## Implementation steps

Step 0 is written first by the orchestrator: it is shared code that every part
builds on. Steps A–F then run in parallel, each with its own files, and no two
steps edit the same file. Step G integrates the window process after A–C and
E. Step H documents.

0. **Shared contracts** (orchestrator, sequential):
   - `controller_client/manual_hotkeys.py`: `HotkeyKind`, `HotkeyStatus`,
     `HotkeyBackend` Protocol, the `ManualHotkeys` facade with platform
     selection importing the three backend modules (stub classes to be filled
     by A/B/C), and `probe_shortcuts`.
   - `controller_client/person_window_ipc.py`: `ManualCaptureEvent.staged`,
     `ManualMarkCommitEvent`, `ManualMarkDiscardEvent`,
     `ManualResultKind.MARK`, `ManualMarkView.keys`, encode/decode.
   - `controller_client/protocol.py`: `MANUAL_STEP_MAX_SHOTS` 5,
     `step_max_shots` on `ManualActiveCasePayload` (wire + parse),
     `CLIENT_VERSION` 0.17.0, the size assert.
1. **A: Windows backend.** Owns `controller_client/manual_hotkeys_windows.py`.
2. **B: macOS backend.** Starts with the Carbon-under-Tk spike. Owns
   `controller_client/manual_hotkeys_macos.py`.
3. **C: Linux backend (portal + X11 + selection).** Owns
   `controller_client/manual_hotkeys_linux.py`,
   `controller_client/manual_hotkeys_portal.py` and
   `controller_client/manual_hotkeys_x11.py`.
4. **D: Server multi-shot.** Owns `projects/runner_manual_state_services.py`,
   `projects/manual_case_submit_services.py` and
   `projects/manual_screenshot_services.py`.
5. **E: Spool v2 + coordinator.** Owns `controller_client/manual_spool.py` and
   `controller_client/manual_coordinator.py`.
6. **F: Runner manager + config.** Owns `controller_client/person_window.py`,
   `controller_client/config.py` and `controller_client/example.env` (plus
   `controller_client/client.py` only if request routing needs it).
7. **G: Window UI + app wiring + diagnostics** (one agent, after step 0; best
   started after A–C so the probe can be exercised).
   - Owns `controller_client/person_window_mark_sheet.py` (new),
     `controller_client/person_window_manual.py`,
     `controller_client/person_window_app.py` (create `ManualHotkeys` with the
     Tk `wm_frame()` id, drain in `poll()`, enable/disable after the manual
     render, focus in/back, `--probe-shortcuts [--interactive]`, sheet in
     `--self-check`), `controller_client/person_window_front_app.py`
     (`restore_now`), `controller_client/person_window_diagnostics.py` and
     `controller_client/diagnose.py`.
   - The sheet UI and the app wiring live in the same window process and call
     each other line by line, so they are one part rather than two.
8. **Integration** (orchestrator): read the combined diff, wire the seams and
   run `pre-commit run --all-files`.
9. **H: Docs.** `docs/changed/2026-11-xx-manual-run-shortcuts.md`; `CLAUDE.md`
   controller paragraph (Manual shortcuts, backends per platform,
   `CONTROLLER_MANUAL_SHORTCUTS`, CLIENT_VERSION 0.17.0, `step_max_shots`).

## Verification

- `pre-commit run --all-files`: black, isort, server mypy, and
  `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg
  controller_client/`. Run the latter on macOS, Windows and Linux, since the
  platform guards differ.
- `python -m controller_client.diagnose --window` (and `--no-interactive`) on:
  macOS, Windows 11, Ubuntu 26.04 GNOME Wayland, KDE Plasma 6 Wayland and an
  X11 session (for example Xfce). Expected: the shortcut step reports
  active / pending → active / the focus-only reason, and the self-check
  renders the sheet.
- Manual smoke on each platform with a Manual run of a 3-step case and a
  1-step case:
  - The browser under test is focused when Cmd/Ctrl+Shift+P is pressed. The
    browser does not react, the screenshot has no runner window, and the sheet
    opens with the note focused (Linux Wayland: possibly after a click).
  - Add 2 screenshots and retake one, then Save. Step 2 shows, focus goes back
    (Windows/macOS) and the overview shows ✓.
  - Fail without a note: Save is disabled. With a note, Save works. "Stop
    here" hands the case in as Failed.
  - Last step: the prefilled verdict, Save, hand-in, and the next case is
    claimed and shown automatically.
  - Cancel on a fresh sheet: the step is unmarked and no staged files are left
    in `controller_client/manual_spool/drafts/<token>/`.
  - Pause releases the combinations (Ctrl+Shift+P reaches the browser), except
    in portal keep-open mode, where the tab says so. Continue registers them
    again.
  - A combination already held by another app: the tab names it and the
    buttons and focus-only keys still work.
  - Kill the window mid-sheet: the sheet is gone, staged shots are dropped and
    the marks are intact.
  - Windows with an elevated app in front: the hotkey is delivered and the
    foreground is taken.
- Server: hand in a step with 3 screenshots.
  - The case card shows all 3 under the step.
  - Every `TestRunScreenshot.attempt_id` equals the step attempt's id.
  - TestRail evidence includes them within `MANUAL_TESTRAIL_MAX_SHOTS`.
  - A 0.16.0 runner's one-shot hand-in still lands.
  - A 0.17.0 runner against a server without `step_max_shots` sends one shot
    per step.
- Then run the `reviewer` and `verifier` agents in parallel.

## Open questions

None. Decisions D1–D8 above record the answers.

## Changelog

- 2026-11-03: created (Draft; Q1–Q8 open).
- 2026-11-03: questions answered by the user. Status Ready. Q1, Q2 and Q4–Q8
  took the recommendations. Q3 changed from focus-only to system-wide Linux:
  GlobalShortcuts portal via the existing `dbus-fast` (Registry.Register
  `com.punkhazard.TestRunner` plus a user `.desktop` file, keep-open fallback
  if the compositor re-prompts) and X11 `XGrabKey` via ctypes, with
  focus-only as the stated fallback. Added the Linux diagnose probe and
  restructured the steps into step 0 shared contracts plus parallel parts
  A–F, then G, then docs.
- 2026-11-03: implementation notes. The window process takes
  `--no-manual-shortcuts` (not `--manual-shortcuts`). The coordinator allows
  `step_max_shots + 1` staged shots for one step, so a Retake on a full sheet
  works (the replaced shot is discarded once the new one succeeds); Save still
  keeps at most `step_max_shots`. `commit_mark` also accepts keys already in
  the step's mark ("Edit…" resends them). A restarted window process
  (`ReadyEvent` on the manual request queue) drops the claimed draft's staged
  shots. Portal keep-open mode is a constant (`KEEP_SESSION_OPEN = False`),
  not auto-detected, until re-prompting is observed on a real desktop.
- 2026-11-03: review pass 1. Rejected: keeping a running byte total in
  `ManualSpool` instead of `uploader.refresh()` after Save / Cancel (reviewer,
  Low). Save and Cancel happen at most as often as captures, which already
  walk the spool, and a commit or discard frees space that `spool_full` must
  reflect; a running total is a separate optimization of the existing spool.
- 2026-11-03: review passes 2–4. A Save confirmed after its 30 s timeout
  records the step but never hands in or advances (the sheet was editable
  again); a late hand-in shows "Handed in." Left as is (verifier, Low): shots
  staged after a late commit stay in the spool until the window restarts or the
  claim ends (bounded, never uploaded). Status Done.
