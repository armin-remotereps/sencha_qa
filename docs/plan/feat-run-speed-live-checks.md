# feat: Live input checks for run speed round 1 (`diagnose --input`)

Status: Done
Created: 2026-10-28
Request: Round 1 (`docs/plan/feat-run-speed-round-1.md`,
`docs/changed/2026-10-28-run-speed-round-1.md`) shipped runner behaviour that
was never run on a real machine. The user will run a new diagnose mode on each
test OS (macOS, Windows, GNOME/Wayland Linux) and paste the output back, so the
remaining assumptions can be confirmed or fixed in one final touch. The mode
checks three things:
1. the screenshot-pixel to input mapping, against the display scale the OS
   reports, plus a pointer check where a person confirms the position;
2. the scroll direction per OS;
3. whether modifiers are still held after a key combination (findings Q4).

It is a new runner flag, `python -m controller_client.diagnose --input`. With
`--no-interactive` it runs only the automatic checks. The output ends with a
compact copy-paste summary. Type: `feat` (runner diagnostics only).
Scope change (2026-10-28): the server check of `tool_choice="none"` after
function items is dropped. The user tests it directly in a Django shell on the
production api container, so this plan touches no server files.

## Context
- `controller_client/diagnose.py`: argparse CLI.
  - With no flag it runs `backend.diagnostic_steps()`, which is a screen
    capture check.
  - `--browsers` runs `browser_diagnostics.build_browser_steps`.
  - Both go through `diagnostics.run_diagnostics`: `DiagnosticStep(name,
    run -> str)` steps that stop at the first failure. That shape is
    pass/fail and stop-on-first-failure. It does not fit measured numbers or
    interactive yes/no answers, so the new mode gets its own runner and leaves
    `run_diagnostics` alone.
- `controller_client/executor.py`:
  - `_to_input(x, y)` maps with `pointer_mapping.map_to_input(x, y,
    ScreenSpaces(capture_size(), input_size()))`.
  - `execute_hover(HoverPayload)` is the mapped pointer path with no click.
    It runs `ensure_input_not_blocked()` → `_to_input` → `_DESKTOP.move_to`.
  - `execute_scroll(ScrollPayload)` and `execute_key_press(KeyPressPayload)`
    are the real action entry points.
- `controller_client/desktop_pyautogui.py` (macOS and Windows):
  - `capture_size()` is the latest `screenshot()` image size.
  - `input_size()` is `pyautogui.size()`.
  - Scroll tables: `_MAC_SCROLL_DELTAS` (Quartz `CGEventCreateScrollWheelEvent`,
    lines) and `_WIN_SCROLL_EVENTS` (`mouse_event` with signed `dwData`).
  - `_release_modifiers(context)` runs in `press_keys`' `finally` after
    `pyautogui.hotkey`, and at the start of `type_text`. It returns `None`
    today. macOS reads `CGEventSourceFlagsState(CombinedSessionState)`;
    Windows reads `GetAsyncKeyState` for L/R Shift, Ctrl, Alt and Win.
  - `_dpi_awareness_note()` is Windows only (`IsProcessDPIAware`).
  - Windows-only code sits under `sys.platform == "win32"`. pre-commit runs a
    second mypy pass with `--platform win32` (`mypy-controller-client-win32`).
- `controller_client/desktop_gnome.py`:
  - `capture_size()` and `input_size()` are both the current stream's frame
    size, so the mapping is identity by construction.
  - `_SCROLL_AXES` holds the axis and sign for `NotifyPointerAxisDiscrete`.
  - Modifiers are released by `_then_release`; there is no `_release_modifiers`.
  - Mutter's RemoteDesktop API cannot read the pointer position back.
- `controller_client/stubs/Quartz/__init__.pyi`: a narrow local stub for
  pyobjc Quartz. Display-mode functions are not in it yet.
- Verified on this Mac with the main checkout's controller venv (pyobjc-core
  and pyobjc-framework-Quartz 12.2.1, PyAutoGUI 0.9.54):
  - `CGMainDisplayID`, `CGDisplayCopyDisplayMode`,
    `CGDisplayModeGetPixelWidth/Height` and `CGDisplayModeGetWidth/Height` exist
    and return 5120x2880 px / 2560x1440 pt. `pyautogui.size()` returns
    2560x1440, so the backing scale is 2.0, matching
    `NSScreen.backingScaleFactor`.
  - `pyautogui.position()` works.
  - pyautogui's macOS key map has `f13`..`f20`. Its Windows map has `f13`..`f24`.
- `CLIENT_VERSION` is `"0.7.0"` (`controller_client/protocol.py`).

## Goals
- Run one command per OS and paste one block back. That block answers:
  - mapping: do the capture and input sizes match what the OS reports, and is
    the pointer where the screenshot pixel says?
  - scroll: does each direction move the content the right way?
  - modifiers: are any still held after a combination, before and after the
    round-1 release?
- An automatic-only mode (`--no-interactive`) for a quick or remote run.
- No change to runtime behaviour. The only runtime-module change is a
  behaviour-preserving split in `desktop_pyautogui.py` so the check runs the
  real `press_keys` code.

## Non-goals
- The server check of `tool_choice="none"`, which the user tests in a Django
  shell on production.
- Fixing anything the checks find. That is the follow-up "final touch".
- Multi-monitor checks. Every backend drives only the primary or main display
  (documented in round 1).
- Reading the GNOME logical monitor scale. On GNOME both sizes are the stream
  frame by design, so an OS scale has nothing to cross-check. The pointer
  check, confirmed by a person, is what tests mutter's coordinate space there,
  including fractional scaling.
- A `CLIENT_VERSION` bump. Diagnose is not protocol, and the follow-up fix
  bumps the version if it changes runner behaviour.
- Tests or test tooling. This project has none.

## Design

### Data model
None.

### Services
None. This is runner-only and needs no Django.

### Pages and forms
None.

### Background work and WebSockets
None. The command runs locally on the test machine without connecting to the
server.

### Runner: modules and symbols

**New `controller_client/display_scale.py`** (OS display facts; imported only
by the input diagnostics, never by the runtime path)
- `@dataclass(frozen=True) DisplayScale` has these fields:
  - `source: str`, e.g. "Quartz display mode", "Win32 display settings",
    "not read".
  - `physical_size: tuple[int, int] | None`, the main/primary display in
    physical pixels.
  - `expected_input_size: tuple[int, int] | None`, the size pointer input
    should report given the OS scale and this process's DPI awareness.
  - `scale: float | None`, the OS scale factor (macOS backing scale; Windows
    monitor effective DPI / 96).
  - `detail: str`, the raw numbers for the summary.
- `read_display_scale() -> DisplayScale` dispatches by platform. It never
  raises: an API failure gives `source="unavailable: <reason>"` with `None`
  fields.
  - `darwin`:
    1. `mode = CGDisplayCopyDisplayMode(CGMainDisplayID())`.
    2. Physical size is `CGDisplayModeGetPixelWidth/Height(mode)`.
    3. Expected input size is `CGDisplayModeGetWidth/Height(mode)`, in points.
    4. Scale is pixel width / point width.
  - `win32` (all code under `sys.platform == "win32"`, ctypes with explicit
    `argtypes`/`restype` as in `desktop_pyautogui.py`):
    1. Physical size comes from
       `user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS=-1, DEVMODEW)`:
       `dmPelsWidth`/`dmPelsHeight` (set `dmSize`). This is the current mode in
       physical pixels whatever the DPI awareness. `GetDeviceCaps(DESKTOPHORZRES)`
       was rejected: forum reports show it varies with awareness.
    2. Monitor DPI is `shcore.GetDpiForMonitor(MonitorFromPoint((0, 0),
       MONITOR_DEFAULTTOPRIMARY=1), MDT_EFFECTIVE_DPI=0, ...)`.
    3. Process awareness is
       `GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext())`:
       0 unaware, 1 system, 2 per-monitor. When that API is missing
       (pre-1607), fall back to `IsProcessDPIAware()` (aware → system).
    4. System DPI is `GetDpiForSystem()`. When missing, use
       `GetDeviceCaps(GetDC(None), LOGPIXELSX=88)`, then release the DC.
    5. Process DPI is: the monitor DPI when per-monitor aware, the system DPI
       when system aware, 96 when unaware.
    6. Expected input size is physical × process DPI / monitor DPI, rounded.
    7. Detail lists awareness, system DPI, monitor DPI and the physical size.
  - Other (Linux/GNOME): `source="not read (GNOME: input space is the stream
    frame by design)"` with all `None` fields.
- Pure helper `compare_spaces(capture, input, scale) -> tuple[str, str]`
  returns a verdict and an explanation. The verdict is one of these:
  - `OK`: capture equals physical when known, and input equals expected input
    when known, each within ±1 px.
  - `MISMATCH`: either comparison fails. The explanation names which one.
  - `NO REFERENCE`: there is no OS reference.

  It lives here because it encodes what the OS facts mean. It is not a
  generic util.

**`controller_client/stubs/Quartz/__init__.pyi`**
- Add only these:
  - `CGDirectDisplayID = int` (or a NewType)
  - `CGDisplayModeRef = NewType(...)`
  - `CGMainDisplayID() -> int`
  - `CGDisplayCopyDisplayMode(display: int) -> CGDisplayModeRef | None`
  - `CGDisplayModeGetPixelWidth`, `CGDisplayModeGetPixelHeight`,
    `CGDisplayModeGetWidth`, `CGDisplayModeGetHeight`, each
    `(mode: CGDisplayModeRef) -> int`
- Extend the header comment ("display mode for diagnose --input").

**`controller_client/desktop_pyautogui.py`** (the only runtime module
touched; behaviour unchanged)
- `_release_modifiers(context: str) -> tuple[str, ...]` returns the modifier
  names it found still held before releasing them (empty when none). The macOS
  and Windows logging and release stay as they are.
  - macOS names come from the flag masks: `shift`, `ctrl`, `option`, `command`.
  - Windows names are the existing `_WIN_MODIFIER_KEYS` values.
  - The other-platform stub returns `()`.
- New public `held_modifiers() -> tuple[str, ...]` reads the same state with no
  release and no log. Per OS it uses the same read as `_release_modifiers`, so
  both share one reader (`_held_modifier_state()` or similar) and do not
  duplicate the mask/VK tables. Other platforms return `()`.
- New public `press_combination(keys: Sequence[str]) -> tuple[str, ...]` holds
  today's `press_keys` multi-key body: `pyautogui.hotkey(*keys)`, then in
  `finally` `_release_modifiers("after a key combination")`. It returns what
  the release found. `PyAutoGuiDesktopBackend.press_keys` calls it for
  `len(keys) > 1` and ignores the result. This way the diagnostic exercises
  the exact runtime path, and the runtime gets no hook or diagnose-only
  branch.
- Nothing else changes: no new imports at runtime, and the scroll tables are
  untouched.

**New `controller_client/input_diagnostics.py`** (the `--input` checks; pure
orchestration and printing)
- Signature: `run_input_checks(*, interactive: bool, ask: Callable[[str], str],
  report: Callable[[str], None]) -> InputCheckReport`. `ask` is `input`, and
  `report` is `print`, both injected from `diagnose.py`.
- `@dataclass(frozen=True) CheckResult` has `name: str`, `verdict: str` (one
  of `OK`, `MISMATCH`, `FAIL`, `YES`, `NO`, `SKIPPED`, `N/A`, `INFO`) and
  `detail: str`.
- `InputCheckReport` holds the environment line plus a
  `tuple[CheckResult, ...]`. `format_summary(report) -> str` builds the
  copy-paste block, and `report.failed -> bool` is true for any `MISMATCH`,
  `FAIL` or `NO`.
- It uses the public backend and executor only:
  - `get_desktop_backend()`: `name`, `screenshot()`, `capture_size()`,
    `input_size()`.
  - `executor.execute_hover`, `executor.execute_scroll`,
    `executor.execute_type_text` and `executor.to_input`. `_to_input` is
    renamed to public `to_input`, with its five call sites updated in
    `executor.py`; this is a mechanical rename with no behaviour change. It is
    used to compute the expected input point for the read-back.
- Platform imports at module top (no lazy imports):
  - `if sys.platform != "linux": import pyautogui` and `from
    controller_client.desktop_pyautogui import held_modifiers,
    press_combination`.
  - On Linux these names are not imported, and the checks that need them
    report `N/A`. pyautogui must not be imported on Wayland: it crashes
    without X.
- Checks, in order. Each is wrapped so an exception becomes `FAIL` with the
  message, and the run continues. Checks marked [auto] run in both modes;
  checks marked [interactive] run only without `--no-interactive`.
  1. **Environment** [auto, `INFO`]: four values.
     - `platform.platform()`, `platform.machine()`; on macOS
       `platform.mac_ver()[0]`; on Windows `platform.win32_ver()` and
       `win32_edition()`; on Linux `XDG_SESSION_TYPE`, `XDG_CURRENT_DESKTOP`
       and `gnome-shell --version` (subprocess, 5 s timeout, best effort).
     - Python version.
     - `CLIENT_VERSION` and the backend name.
     - Package versions via `importlib.metadata.version`, best effort:
       PyAutoGUI, Pillow, pyobjc-core (darwin), dbus-fast (linux).
  2. **Screen spaces** [auto]:
     1. Take one `backend.screenshot()` so `capture_size()` is fresh.
     2. Report capture, input and the per-axis ratio input/capture with 3
        decimals.
     3. Report whether the mapping is active (sizes differ) or a no-op.
  3. **OS scale cross-check** [auto]: `read_display_scale()` then
     `compare_spaces`, with an explanation of each verdict:
     - macOS: capture should equal mode pixels; input should equal mode
       points.
     - Windows: capture should equal the physical mode; input should equal
       physical × process DPI / monitor DPI.
     - GNOME: `NO REFERENCE` with the frame size, "identity by design".
  4. **Pointer read-back** [auto, macOS/Windows]:
     1. Targets in screenshot pixels: centre `(w//2, h//2)`, quarter
        `(w//4, h//4)`, and near bottom-right `(w-40, h-40)`.
     2. For each: `execute_hover(HoverPayload(x, y))` (the mapped executor
        path), then `pyautogui.position()`.
     3. `OK` when the read-back is within ±1 of `to_input(x, y)`.
     4. Restore the pointer to where it was afterwards.

     The quarter target is there because a factor-2 error is obvious at it.
     A corner target alone hides one: the OS clamps out-of-range moves to the
     corner. GNOME reports `N/A (mutter cannot report the pointer position)`.
  5. **Pointer seen by a person** [interactive, all OS]:
     1. For each of the three targets, print in plain words where the pointer
        should be, e.g. "in the middle of the screen", "in the middle of the
        top-left quarter", "about 40 px from the bottom-right corner".
     2. Move there with `execute_hover`, hold 3 s, and ask
        `Is the pointer there? [y/n/s]`.
     3. Record `YES`, `NO` or `SKIPPED`.

     The pointer moves first and the question comes after, because the person
     looks at the screen, not the terminal, while it moves.
  6. **Scroll direction** [interactive, all OS]:
     1. Ask the person to open a long scrollable window (a long web page, a
        long document), press Enter, then within 5 s put the pointer over it
        without clicking. The terminal keeps keyboard focus.
     2. Run `execute_scroll(ScrollPayload(DOWN, 5, None, None))` at the pointer.
     3. Ask "Did the content move to show later content, as when scrolling
        down? [y/n/s]".
     4. Then `UP` with the same amount: "did it go back?".
     5. Then ask whether to test left/right, which needs horizontally scrollable
        content such as a zoomed-in page or a wide spreadsheet. If yes, repeat
        with `RIGHT` ("later content to the right came into view?") and `LEFT`.
     6. The result line names only OS and direction (no table import and no
        accessor, so runtime modules stay untouched). The follow-up knows
        which table belongs to which OS: `_MAC_SCROLL_DELTAS`,
        `_WIN_SCROLL_EVENTS` or `_SCROLL_AXES`.
  7. **Modifiers after a combination** [auto, macOS/Windows]:
     1. For each harmless combo, run `press_combination(keys)`. Its return value
        is `held_before_release`, read right after `pyautogui.hotkey` returned
        and before the round-1 release.
     2. Then read `held_modifiers()` immediately (`after_release`) and again
        after 250 ms (`after_250ms`).
     3. Combos are named with backend key names and resolved through
        `backend.resolve_key` so aliases match runtime:
        - macOS: `ctrl+command+f19`, `shift+option+f19`, `command+shift+f19`.
          F19 has no default macOS binding.
        - Windows: `ctrl+shift+f24`, `ctrl+alt+f24`, `win+f24`. F24 is unbound,
          and a non-modifier key between Win down and up keeps Start closed.
     4. Each combo's verdict:
        - `OK` when all three readings are empty.
        - `INFO (released)` when only `held_before_release` is non-empty. The
          round-1 release was needed and worked; this is the Q4 evidence.
        - `FAIL` when `after_release` or `after_250ms` is non-empty.
     5. Before the combos, read `held_modifiers()` once. If anything is already
        held (the person is pressing a key), report `SKIPPED (a modifier was
        already down)`.

     GNOME: `N/A (GNOME releases every key it pressed; no OS modifier state
     to read)`.
  8. **Full-screen reproduction** [interactive, macOS only, opt-in, default
     No]. This is the original F10/Q4 sequence, the only one known to have
     opened the emoji picker; a harmless combo may not reproduce it.
     1. Ask: "Run the TextEdit reproduction? It toggles full screen twice in
        the window you pick. [y/N]".
     2. If yes, ask the person to open a TextEdit document, press Enter, and
        click into the document within 5 s.
     3. Run `press_combination(["ctrl", "command", "f"])` twice with 1.5 s
        between (full-screen animation), recording each return.
     4. Run `executor.execute_type_text(TypeTextPayload(" E1 typed", 0.0))`.
     5. Ask "Did ' E1 typed' arrive intact, with no emoji picker? [y/n]".
     6. The detail holds the two `held_before_release` readings.
- Interactive prompts read with `ask`. EOF or KeyboardInterrupt on a prompt
  marks the rest `SKIPPED` and still prints the summary.
- **Summary block** at the very end, ASCII only, at most ~25 lines, one check
  per line, measured numbers inline:
  ```
  ----- BEGIN PUNK HAZARD INPUT CHECK -----
  env: macOS 15.5 arm64 | py 3.13.1 | client 0.7.0 | backend pyautogui | PyAutoGUI 0.9.54 Pillow 11.2.1 pyobjc 12.2.1
  spaces: capture 5120x2880 input 2560x1440 ratio 0.500x0.500 mapping active
  os-scale: OK Quartz display mode 5120x2880 px / 2560x1440 pt scale 2.00
  pointer-readback: OK centre (2560,1440)->(1280,720) got (1280,720); quarter ...; corner ...
  pointer-seen: YES centre, YES quarter, YES corner
  scroll: down YES, up YES, right SKIPPED, left SKIPPED
  modifiers: ctrl+command+f19 OK before[] after[] 250ms[]; ...
  fullscreen-repro: SKIPPED
  result: PASS | ATTENTION (n checks)
  ----- END PUNK HAZARD INPUT CHECK -----
  ```
  Exact wording is the implementer's. The contract: the begin/end markers, one
  line per check named as above, the verdict first on each line, and all
  numbers present.

**`controller_client/diagnose.py`**
- `--input` (store_true) is mutually exclusive with `--browsers`, through an
  argparse mutually exclusive group.
- `--no-interactive` (store_true) is only meaningful with `--input`. Using it
  without `--input` is a `parser.error`.
- With `--input`: run `run_input_checks(interactive=not args.no_interactive,
  ask=input, report=print)`, then print `format_summary(report)`.
  `sys.exit(1)` when `report.failed`, otherwise exit 0.
- Update the description and help text. The existing default and `--browsers`
  paths are unchanged.
- Import note: `diagnose.py` now imports `executor` through
  `input_diagnostics`. `executor` already builds the same backend that
  `get_desktop_backend()` returns, so this adds no side effects beyond import
  cost.

### Settings / infrastructure
None. No env vars and no new dependencies: pyobjc Quartz arrives with PyAutoGUI
on macOS, Windows uses ctypes, and Linux uses only the stdlib for this.

### Data flow
1. The person runs `python -m controller_client.diagnose --input` (with
   `--no-interactive` for automatic checks only).
2. `diagnose.main` → `input_diagnostics.run_input_checks`.
3. The checks go through the backend for capture and sizes, `display_scale`
   for OS facts, `executor.execute_hover`/`execute_scroll`/`execute_type_text`
   for the real mapped action paths, and `desktop_pyautogui.press_combination`/
   `held_modifiers` for modifiers.
4. Each check prints a line as it runs, and the person answers the y/n
   prompts.
5. The summary block prints. The person pastes it back. The exit code is 1 when
   anything needs attention.

## Alternatives considered
- **Reuse `DiagnosticStep`/`run_diagnostics`:** rejected. It stops at the first
  failure and returns a string. The new checks must all run and yield verdicts
  and numbers. A second small runner is simpler than bending the first.
- **A hook in `press_keys` to read modifiers before release:** rejected. A
  callback parameter, or capturing the WARNING log record, puts diagnose
  concerns into the hot path or depends on log text. Moving the existing body
  into `press_combination` and returning what `_release_modifiers` already
  reads costs nothing at runtime and tests the identical code.
- **Raw `pyautogui.hotkey` with timed samples before any release** (to tell
  "stuck" from "flag state lags"): not added. The `before / after / +250 ms`
  readings answer Q4, which asks whether the release is needed and whether it
  works. A lag-versus-stuck split would not change the fix.
- **Windows physical size via `GetDeviceCaps(DESKTOPHORZRES)`:** rejected in
  favour of `EnumDisplaySettingsW(ENUM_CURRENT_SETTINGS)`. Reports conflict on
  whether the former follows DPI virtualization; the current display mode is
  physical by definition.
- **GNOME scale via `org.gnome.Mutter.DisplayConfig.GetCurrentState`:**
  rejected. There is a large D-Bus signature to parse, and nothing to compare
  it against, since both sizes come from the stream. The pointer check
  confirmed by a person tests what matters.
- **Automatic pointer verification by finding the cursor in a screenshot:**
  rejected. macOS `screencapture` omits the cursor by default, and image
  matching is out of proportion. The pointer read-back covers the
  input-space step, and the scale cross-check covers the units.
- **A scroll-table accessor in each backend:** rejected to keep runtime
  modules untouched. The summary names OS and direction, which identifies the
  table entry.

## Risks and mitigations
- **Key combos with side effects.**
  - Only F19 (macOS) and F24 (Windows) with modifiers are sent, and no letter
    keys.
  - The full-screen reproduction is opt-in and runs only after the person
    picks a TextEdit window.
  - Never `cmd+q`, `cmd+w`, `ctrl+cmd+q` (lock), `win+l`, a lone `win`, or
    `alt+f4`.
- **Pointer read-back clamping hides a factor-2 error at the corner.** The
  quarter and centre targets catch it; this is noted in the code comment.
- **Windows API availability.** `GetAwarenessFromDpiAwarenessContext` and
  `GetDpiForSystem` need Windows 10 1607 or later; `GetDpiForMonitor` needs 8.1
  or later. Missing functions show up as `AttributeError` on `ctypes.WinDLL`
  attribute lookup. Fall back as described or report `unavailable`, and never
  crash the run.
- **pyautogui `SetProcessDPIAware` at import** makes the process
  system-DPI-aware. The check reports the awareness the runner really runs
  with, which is the point.
- **UIPI.** An elevated window in front makes `execute_hover`/`execute_scroll`
  raise `InputBlockedError`. That becomes a `FAIL` line naming the cause, and
  the run continues.
- **Runtime regression in `press_keys`.** The refactor is a pure extraction,
  and `_release_modifiers`' return value is ignored at runtime. The reviewer
  checks that the `finally` placement is identical.
- **Linux import safety.** `input_diagnostics` must not import
  `desktop_pyautogui` or pyautogui on Linux. The import is guarded by
  `sys.platform != "linux"`, the same rule as `desktop.py`.
- **Secrets.** None involved. The summary holds no hostname or connection key.

## Implementation steps
The work is runner only. Two parts share no file. Part 2 consumes Part 1's
interfaces as named above, so write Part 1 first or give Part 2 the
signatures.

1. **Part 1: OS facts and backend primitives.**
   - Files: new `controller_client/display_scale.py` (`DisplayScale`,
     `read_display_scale`, `compare_spaces`),
     `controller_client/stubs/Quartz/__init__.pyi` (display-mode functions)
     and `controller_client/desktop_pyautogui.py` (`_release_modifiers` returns
     `tuple[str, ...]`, a shared modifier reader, `held_modifiers`,
     `press_combination`, `press_keys` delegating).
2. **Part 2: checks and CLI.**
   - Files: new `controller_client/input_diagnostics.py` (`CheckResult`,
     `InputCheckReport`, `run_input_checks`, `format_summary`),
     `controller_client/diagnose.py` (`--input`, `--no-interactive`) and
     `controller_client/executor.py` (rename `_to_input` → `to_input`; five
     call sites, no behaviour change).
3. **Integration (orchestrator).**
   - Check the imports and the Linux guard.
   - Run `pre-commit run --all-files`, which covers the controller mypy for
     both the host and `--platform win32`.
   - Run `python -m controller_client.diagnose --input --no-interactive` and
     then interactively on this Mac.
   - Update root `CLAUDE.md` Commands (one line:
     `python -m controller_client.diagnose --input [--no-interactive]`).
   - Optionally add the same line to the diagnose hint in
     `controller_client/scripts/setup.sh` / `setup.ps1` /
     `setup.bat`. Decision: skip this. Those scripts are for setting up a
     machine, not a one-off verification.

## Verification
- Static: `pre-commit run --all-files` (isort, black,
  `mypy-controller-client`, `mypy-controller-client-win32`).
- On this Mac, a Retina display at 2x:
  - `--input --no-interactive`: spaces 5120x2880 / 2560x1440, os-scale OK at
    2.00, pointer read-back OK at all three targets, modifiers lines present,
    and exit code 0.
  - Interactive: the pointer sits at the centre, at the middle of the top-left
    quarter and near the bottom-right corner. Scroll down shows later content.
    Answering `n` once gives exit code 1 and `result: ATTENTION`.
  - Ctrl+C at a prompt: the remaining checks show `SKIPPED` and the summary
    still prints.
  - The default `diagnose` and `--browsers` behave as before, and
    `--no-interactive` alone errors.
- The user then runs it on these machines and pastes each summary:
  - a non-Retina Mac if available;
  - Windows at 125 % or 150 %;
  - the GNOME 26.04 VM, with fractional scaling on.

  Get the runner from this branch's checkout, or from the runner ZIP of a
  server built from this branch.
- Run the `reviewer` and `verifier` agents in parallel on the diff.
- After the pasted results come back: fix any sign or mapping in the one table
  or function named, and record the answers in
  `docs/changed/2026-10-28-run-speed-round-1.md`'s follow-up note or a new
  change note. That work is out of this plan.

## Open questions
None that need the user. Every design choice above is decided in the plan.

## Changelog
- 2026-10-28: created. The server `tool_choice="none"` management command was
  dropped at the user's request before drafting: it is tested directly in a
  Django shell on the production api container.
- 2026-10-28: on GNOME the `os-scale` line reports `N/A no reference; ...`
  rather than a `NO REFERENCE` verdict: `N/A` is in the verdict set and the
  mapping there is identity by design. `NO REFERENCE` stays the internal
  `display_scale.compare_spaces` verdict (exported as `NO_REFERENCE`).
- 2026-10-28: review fixes. Windows reads the true monitor DPI under a
  temporary per-monitor thread DPI context (`GetDpiForMonitor` is virtualized
  for the system-aware runner) and passes `ENUM_CURRENT_SETTINGS` as
  `0xFFFFFFFF`; the TextEdit reproduction checks that the frontmost window is
  TextEdit before the shortcut and before typing (SKIPPED otherwise) and lets
  the terminal echo settle before flushing; the Windows scroll check reports
  the wheel routing setting and tells the person about "Scroll inactive
  windows when I hover over them"; the `os-scale` line is built from the
  explanation and detail only.
- 2026-10-28: review round 2: `SPI_GETMOUSEWHEELROUTING` is 0x201C (0x201A is
  `SPI_GETGESTUREVISUALIZATION`). Optional nit not taken: a TextEdit run skipped
  by the second guard drops its before-readings.
