# feat: Run speed, round 1

Status: Done
Created: 2026-10-28
Request: Make test runs faster and cause fewer help pauses. Six items come from
the findings in `docs/analysis/run-speed-findings.md`, and the user approved all
six. (1) Quote `cwd` in `execute_command` so paths with spaces work (F1).
(2) When a sub-agent's time budget runs out, give the model one last turn with no
tools so it can answer in its RESULT format. Today a step that already reached its
expected state comes back as FAIL (F11, Q3). (3) Add the desktop tools `scroll`
and `click_at`, both performed through the runner's normal input path (F8, F9).
(4) Release stuck modifier keys after key combinations and before typing (F10,
Q4). (5) Reword the planner and executor prompts: shell work is a command, merge
read-only steps, take the first screenshot only when the step needs the GUI,
chain predictable actions, and mention the new tools (F2, F3, F4). (6) Strip ANSI
codes from command output and collapse repeated lines before the model sees it.
Constraint: one LLM model for every role. No model tiering and no change to
reasoning effort.

## Context

Read for this plan: `CLAUDE.md`, `agents/CLAUDE.md`, the analysis above,
`docs/changed/2026-10-28-wayland-controller.md`,
`docs/changed/2026-10-28-windows-runner-commands-and-apps.md` (commit 6bfb80f),
`docs/changed/2026-10-25-verified-action-unconfirmed-outcome.md`, and
`docs/plan/feat-agent-double-click-iterations-close-app.md`, which is the pattern
for adding an optional capability.

**Shell (items 1, 6)**
- `agents/services/tools_controller.py::execute_command` does three things in
  order: `runner_os_name`, then `ensure_runner_supports_windows_shell`, then
  `agents/services/shell_command.py::build_shell_command(command, cwd, os_name)`.
  - Windows already quotes (`cd /d "<cwd>" && ...`).
  - The POSIX branch is still `f"cd {cwd} && {command}"`, which is the F1 bug.
- Runner shells:
  - POSIX: `pexpect` runs `/bin/bash -c` in a PTY
    (`controller_client/interactive_process_posix.py`), so output lines end in
    `\r\n`.
  - Windows: `cmd.exe /d /s /c` with pipes
    (`controller_client/interactive_process_windows.py`). Output is decoded as
    OEM with `replace`, lines end in `\r\n`, and there is no PTY. Tools such as
    npm may still print VT sequences.
  - So bash is the only POSIX shell, and the quoting belongs on the server, in
    `build_shell_command`.
- Shell output reaches the model through `execute_command`, `send_command_input`
  and `wait_for_command` in `tools_controller.py`.
  `agent_loop._cap_tool_result` then keeps the head and tail, up to
  `AGENT_TOOL_RESULT_MAX_CHARS`.

**Agent loop (item 2)**
- `agents/services/agent_loop.py::run_tool_loop` checks the elapsed time at the
  top of each iteration and returns `AgentStopReason.TIMEOUT` with
  `error="Timed out after N seconds"`, with no final LLM call.
- It has three callers:
  - `_run_agent_loop`, which is called by `sub_agent.run_sub_agent`. That covers
    the step, recovery and restore sub-agents (config from
    `orchestrator._build_sub_agent_execution_config`, with
    `_config_for_attempt` using `dataclasses.replace` per attempt) and the case
    teardown, which builds its own `AgentConfig` in `case_teardown._run`.
  - `machine_setup/services/run_turn.py`, the setup chat turn.
  - `machine_setup/services/run_setup_task.py`, the setup helper sub-task.
- How a timeout is handled downstream:
  - `machine_setup.subtask_report.parse_subtask_report` forces FAILED on
    `TIMEOUT`.
  - `turn_outcome.classify_turn_end` maps it to `TIME_LIMIT`.
  - `sub_agent._parse_sub_task_result` already prefers an explicit `RESULT:` in
    the last assistant text over the stop reason, and builds the
    `actions_digest` on every fail.
- `agents/services/llm_client.py::send_chat_completion` always sends
  `tool_choice: "auto"` when tools are given. Its httpx timeout is
  `OPENAI_REQUEST_TIMEOUT` (900 s). A timeout raises
  `AIServiceError(TIMEOUT)`, which pauses the case as `ai_service`.

**Desktop input (items 3, 4)**
- The click path:
  1. `tools_controller.click`
  2. `_verified_click`
  3. `projects.services.controller_click(project_id, x, y)`, which sends
     `controller.click {x, y, button}` (`projects/controller_protocol.py`,
     `controller_consumer.py`)
  4. on the runner: `client._handle_click`, then `executor.execute_click`
     (`ensure_input_not_blocked()`, then `_DESKTOP.click`)
- **Coordinate space.** Nothing maps screenshot pixels to input coordinates
  today. OmniParser centres are in screenshot pixels and the runner sends them
  straight to the backend. Each backend works like this (read in the installed
  packages and in mutter's source):
  - **macOS (pyautogui)**
    - Capture: `screencapture -x` (`screen_capture_macos.py`) writes the
      **main display only**, in backing pixels.
    - Input: `pyautogui.size()` is
      `Quartz.CGDisplayPixelsWide/High(CGMainDisplayID())`
      (`_pyautogui_osx._size`), i.e. the main display in points. Quartz mouse
      events (pyautogui and our double-click) also take points.
    - On a Retina display the screenshot is 2880x1800 while input is
      1440x900, so every click lands at 2x. This was flagged as suspected, item
      (c) in `feat-agent-double-click-iterations-close-app.md`. The analysed run
      used a 1280x960 screenshot (scale 1), so it did not show up there.
  - **Windows (pyautogui)**
    - Input: pyautogui calls `SetProcessDPIAware()` on import
      (`_pyautogui_win.py:17`), and `pyautogui.size()` is
      `GetSystemMetrics(0/1)`, the primary monitor in the process's (system)
      DPI space.
    - Capture: `pyautogui.screenshot()` → `pyscreeze` →
      `PIL.ImageGrab.grab()`. Pillow 11.2.1's `grabscreen_win32` switches the
      thread to `DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE` for the grab and reads
      `HORZRES/VERTRES` (`src/display.c`), so the capture is the **primary
      monitor in physical pixels**.
    - Usually both are physical and match. They differ when the awareness call
      failed (the process is then DPI-unaware: input is logical, e.g. 1536x864
      at 125%, while the capture is still 1920x1080), or when the primary
      monitor's scaling was changed without signing out (system DPI ≠ monitor
      DPI).
  - **GNOME Wayland**
    - Input and capture are both the ScreenCast stream of the **primary
      monitor** (`gnome_remote_session.py`, `RecordMonitor`).
    - Mutter's `meta_stream_source_monitor_get_specs` sizes the stream as
      `round(logical_rect × scale)`, where scale is the logical monitor scale
      when stage views are scaled, otherwise 1. Its
      `meta_stream_monitor_transform_position` maps input as
      `layout.x + stream_x / scale` with the same scale. Checked in
      `GNOME/mutter` `src/backends/meta-stream-source-monitor.c` and
      `meta-stream-monitor.c`.
    - `pipewire_frame.py` does not rescale (`pipewiresrc ! videoconvert !
      pngenc`).
    - So the input coordinate space *is* the stream's pixel space, which is the
      frame size. That holds with integer and fractional scaling alike.
      `desktop_gnome._check_bounds` already checks against that frame size.
- **pyautogui 0.9.54 scrolling** (read in the installed package):
  - Windows `_scroll` sends `dwData=clicks` without multiplying by
    `WHEEL_DELTA` (120), so `scroll(1)` moves 1/120 of a notch. `_hscroll`
    just calls `_scroll`, so horizontal becomes vertical.
  - macOS `_hscroll` computes the negative remainder as
    `(-1 * clicks % 10)`, which is positive, so a scroll left partly scrolls
    right.
  - So the backends must not call `pyautogui.scroll` / `hscroll`.
- GNOME input goes through `gnome_remote_session.GnomeRemoteSession`
  (`NotifyPointerMotionAbsolute`, `NotifyPointerButton`,
  `NotifyKeyboardKeysym`). Mutter's `org.gnome.Mutter.RemoteDesktop.Session`
  also has `NotifyPointerAxisDiscrete(u axis, i steps)` (checked against
  mutter's `data/dbus-interfaces/org.gnome.Mutter.RemoteDesktop.xml`; the axis
  numbering and sign are not documented there).
- **Modifier keys.**
  - pyautogui `hotkey` presses keys down in order and releases them in reverse.
  - On macOS, `typewrite` posts key events whose flags come from the session's
    modifier state.
  - Typing `" E1 typed"` while Ctrl+Cmd is still down is Ctrl+Cmd+Space, the
    macOS emoji picker shortcut. That matches F10 exactly, but it is not
    proven (Q4).
  - The GNOME backend releases every key it pressed in `_then_release`, even on
    errors.
- Optional capabilities follow one of two patterns:
  - `double_click`: `ensure_runner_supports_double_click` raises
    `RunnerOutdatedError`, and the case pauses for help.
  - `close_app` / `close_tab`: `runner_supports` returns a tool error with a
    manual fallback.
  - `CLIENT_VERSION` is `0.6.2` and the newest capability is `windows_shell_v1`.
  - Message builder: `ControllerMessageBuilder.build_action_message` skips
    payload keys missing from the event.
- `vision_qa.answer_screenshot_question` does not tell the model the image size.
  `controller_screenshot` returns `width` / `height`.

**Prompts (item 5)**
- Planner: `orchestrator_prompts.build_plan_system_prompt`.
  - Its "CRITICAL" rule says "always specify WHICH application, window, or
    terminal to use (e.g., 'In the VS Code integrated terminal, run...')", which
    produces "In macOS Terminal" (F2).
  - `PLAN_SCOPE_RULES` is shared with both chat redesign prompts
    (`machine_setup/services/setup_prompt.py`).
- Executor parts (`prompt_parts.py`): `build_tool_taxonomy`,
  `build_desktop_tool_examples`, `build_tool_selection_rules`, and
  `build_shell_rules(system_info=)`, which is OS-aware since 6bfb80f.
  `sub_agent_prompts._build_state_section` says "Take a screenshot first"
  (F3), and `_build_time_budget_section` says the step "fails automatically".
- `machine_setup/services/setup_task_prompt.py` reuses the taxonomy, desktop
  examples, tool selection and shell rules. Setup helpers therefore inherit the
  wording and the new tools with no edit there.

Versions:
- Server: Django ~=5.2.11, channels ~=4.3.2, celery ~=5.6.2, httpx ~=0.28.1
  (`requirements.txt`).
- Runner: PyAutoGUI ==0.9.54, pexpect ~=4.9.0, dbus-fast ~=5.2.0, Pillow ~=11.2.1
  (`controller_client/requirements.txt`). Quartz/AppKit come from pyobjc
  through pyautogui, with local stubs in `controller_client/stubs/`.

## Goals
- Paths with spaces work as `cwd` on every runner OS.
- A sub-agent whose budget ran out still gives a verdict on what it already
  observed. A step that was done comes back PASS instead of "Timed out".
- The agent can scroll and click at screen coordinates with real input events
  that move keyboard focus.
- Typing never runs with a modifier still held down from an earlier combination.
- Fewer, cheaper steps: shell work goes through `execute_command`, read-only
  steps merge, and there are no opening screenshots for shell-only steps.
- Command output reaches the model without ANSI noise or long runs of identical
  lines.
- The server part ships before Test Runners update, and old runners keep working.

## Non-goals
- No model tiering, reasoning-effort, or `OPENAI_REASONING_SUMMARY` change (user
  constraint). F5 stays open.
- No deterministic state restore (F6), system-dialog keyboard rules (F7), budget
  sizing by verified actions (F11, beyond the final turn), narrower vision
  questions (F12), per-step timestamps (Q2), or a verdict rule for `continue`
  decisions (Q3). Each needs its own plan.
- No final turn for the machine setup chat or the setup helpers: their
  report parser fails a timed-out helper whatever it says (decision below).
- No right/middle-button or double `click_at`, and no "end of element" text-offset
  target. `click_at` plus `key_press End` covers the F9 case.
- No change to `TEARDOWN_TOOL_NAMES`.
- No support for monitors other than the primary/main one. Capture already
  covers only that monitor on every OS (see Multi-monitor limits).

## Design

### Data model
None. No migration.

### Shared types (written first, see step 0)
- `agents/types.py`
  - `@dataclass(frozen=True) class FinalAnswerPolicy: prompt: str;
    timeout_seconds: int`. This is the user message sent when the budget is
    used up, plus the cap for that one LLM call.
  - `AgentConfig.final_answer: FinalAnswerPolicy | None = None`. The default is
    None, which means off, so the setup chat, the setup helpers and the
    teardown keep their behaviour without any edit.
  - `AgentResult.answered_after_timeout: bool = False`.
- `controller_client/protocol.py`
  - `MessageType.SCROLL = "scroll"`.
  - `ClientCapability.SCROLL_V1 = "scroll_v1"`, optional, **not** in
    `REQUIRED_CLIENT_CAPABILITIES`. Update the comment that lists optional
    capabilities.
  - `class ScrollDirection(StrEnum): UP, DOWN, LEFT, RIGHT`.
  - `MAX_SCROLL_AMOUNT: Final = 25`.
  - `@dataclass(frozen=True) class ScrollPayload: direction: ScrollDirection;
    amount: int; x: int | None; y: int | None`.
  - `parse_scroll_payload(data)`:
    - `direction` must be a known value.
    - `amount` must be an int (not bool) with 1 <= amount <=
      `MAX_SCROLL_AMOUNT`.
    - `x`/`y` are both present or both absent/null. Anything else raises
      `ProtocolError`.
  - `CLIENT_VERSION = "0.7.0"`, a new message type.
- `controller_client/desktop_backend.py`, `DesktopBackend` Protocol:
  - Add `def scroll(self, direction: ScrollDirection, amount: int, x: int |
    None, y: int | None) -> None`. The amount is in wheel notches and None
    means the current pointer position.
  - Add `def capture_size(self) -> tuple[int, int]`: the size of the latest
    screenshot this backend captured, capturing one when there is none yet.
  - Add `def input_size(self) -> tuple[int, int]`: the size of the coordinate
    space its pointer input takes, read at call time.
  - Document that every pointer method (`click`, `double_click`, `move_to`,
    `drag`, `scroll`) now receives **input-space** coordinates; the executor
    maps them (below).
- New `controller_client/pointer_mapping.py`. It is pure: no backend, no OS
  import.
  - `@dataclass(frozen=True) class ScreenSpaces: capture: tuple[int, int];
    input: tuple[int, int]`.
  - `map_to_input(x: int, y: int, spaces: ScreenSpaces) -> tuple[int, int]`:
    1. Check `0 <= x < capture_w` and `0 <= y < capture_h`. Otherwise raise
       `ExecutionError` with the message GNOME uses today: "Coordinates (x, y)
       are outside the screen, which is WxH pixels as in the latest screenshot.
       Use coordinates from a current screenshot."
    2. Scale each axis on its own:
       `min(input_w - 1, floor((x + 0.5) * input_w / capture_w))`, and the same
       for y.
    3. Equal sizes return the point unchanged.
    4. A zero-sized space raises `ExecutionError` ("screen size unknown").

`click_at` needs **no protocol change**. It sends the existing `click` message, so
it works on every runner. A runner from this plan maps it like every other
pointer action.

### Services and tools (server)

**Item 1: cwd quoting** (`agents/services/shell_command.py`)
- POSIX branch: `cd -- <quoted> && <command>`. `<quoted>` is
  `shlex.quote(cwd)`, except that a leading home prefix is kept unquoted so
  that it still expands:
  - `~` or `~user` up to the first `/`
  - `$HOME` or `${HOME}` up to the first `/`

  The rest after the slash is quoted. `~` alone or `$HOME` alone becomes
  `cd -- ~` / `cd -- "$HOME"`.
- Windows is unchanged.
- The command itself is not touched. The `cwd` parameter description in
  `tool_definitions.py` gains "absolute path, or ~/... on macOS and Linux".
- Why on the server: the cd is already built there for old runners, which ignore
  unknown fields (see the module docstring), and bash is the only POSIX shell.

**Item 6: output cleaning** (new pure module `agents/command_output.py`)
- Pure, like `agents/attempt_digest.py`: no ORM, no settings, no
  `agents.services` import.
- `clean_command_output(text: str) -> str`, in this order:
  1. Normalise `\r\n` to `\n`.
  2. Strip ANSI/VT sequences: CSI `ESC [ ... final`, OSC `ESC ] ... (BEL | ESC \)`,
     and other two-character `ESC x`. Also drop stray C0 controls except `\n`
     and `\t`. A backspace drops itself and the character before it.
  3. Inside each line keep only the text after the last remaining `\r`. That is
     what a terminal shows for progress bars (`npm`, `curl`, `pip`).
  4. Collapse a run of 3 or more identical consecutive lines (blank lines
     included) to the first line plus `[previous line repeated N more times]`.
- Applied in `tools_controller.execute_command`, `wait_for_command` and
  `send_command_input` to `result["output"]`, before formatting and before the
  `on_log` line, so the case log is clean as well. The head/tail cap in
  `agent_loop` stays where it is and runs afterwards.
- A utility, not domain logic. It lives beside `attempt_digest.py` because
  `agents/services/__init__.py` imports `projects.services`, so a pure module
  must stay outside that package.

**Item 2: final turn on timeout** (`agent_loop.run_tool_loop`)
- When `elapsed > config.timeout_seconds` and `config.final_answer` is set:
  1. Check cancellation.
  2. Log `[Agent] Time budget used up; asking for a final answer`.
  3. Append `ChatMessage(role="user", content=policy.prompt)` and call
     `send_chat_completion(config.llm, messages, tool_definitions,
     tool_choice="none", timeout_seconds=policy.timeout_seconds)`.
     - Sending the same tool list keeps the request prefix identical (prompt
       cache), and `tool_choice="none"` forbids calls.
     - Every earlier `function_call` already has its output at the top of the
       loop, so the input is well formed.
  4. Text and no tool calls: append the assistant message and return
     `AgentResult(stop_reason=TIMEOUT, error="Timed out after N seconds",
     answered_after_timeout=True, ...)`.
  5. Tool calls anyway, or no text: drop the calls and keep any text. With no
     text, fall back.
  6. Fallback: on `AIServiceError` (including the capped timeout) or with no
     text, log a warning and return exactly what today's code returns, without
     the extra user message. A provider fault in the grace turn must not turn a
     timed-out step into an `ai_service` pause.
- `MAX_ITERATIONS` keeps today's behaviour, because 300 turns is not the
  observed problem.
- `llm_client.send_chat_completion(config, messages, tools=(), *,
  tool_choice: Literal["auto", "none"] = "auto", timeout_seconds: float | None
  = None)`. None means `OPENAI_REQUEST_TIMEOUT`. `_build_payload` sends
  `tool_choice` only when tools are given (as today).
- Opt-in: `orchestrator._build_sub_agent_execution_config` sets
  `final_answer=FinalAnswerPolicy(prompt=build_final_answer_prompt(),
  timeout_seconds=settings.AGENT_FINAL_ANSWER_TIMEOUT_SECONDS)` when the
  setting is > 0. Steps, recoveries and restores get it through
  `_config_for_attempt`'s `replace`. The teardown and both setup callers build
  their own configs and stay off.
- `sub_agent.run_sub_agent` passes `final_turn_on_timeout=config.final_answer
  is not None` to `build_sub_agent_system_prompt`, so the TIME BUDGET section
  tells the truth.
- `sub_agent._parse_sub_task_result`:
  - Explicit `RESULT: PASS` with `answered_after_timeout`: `status="pass"`,
    `error=None`, and the summary gets the suffix
    ` (answered when the step's time budget ran out)`.
  - Explicit `RESULT: FAIL`: as today. The error keeps "Timed out after N
    seconds" and the digest is built. Its last observation is the final
    answer's text.
  - No RESULT: as today (fail).
- Final prompt (`sub_agent_prompts.build_final_answer_prompt()`): "TIME IS UP.
  Your time budget for this step has run out and no more tools can be called.
  Answer now in the RESULT FORMAT. Answer RESULT: PASS only if a tool result or
  screenshot answer earlier in this conversation already showed every check of
  the expected result; otherwise RESULT: FAIL, and say what was done and what is
  still unverified. Do not guess."

**Item 3: new tools** (`tool_definitions.py`, `tool_registry.py`,
`tools_controller.py`)
- `scroll(direction, amount?, x?, y?)`, category CONTROLLER.
  - `direction` is an enum of the four values. `amount` is wheel notches
    (integer, default 5, 1-25). `x`/`y` are screenshot pixel coordinates of a
    point over the area to scroll, and must be given together. When they are
    missing the scroll happens at the current pointer position.
  - Handler: strict int parsing (int, or a float with no fraction; never bool or
    str), with range and pairing checks. A bad value returns a tool error naming
    the rule.
  - `tools_controller.scroll`:
    - If `runner_supports(project_id, ClientCapability.SCROLL_V1)` is false,
      return a **tool error**: "This Test Runner is too old for scroll. Click
      inside the area first, then use key_press (Page_Down, Page_Up, space, or
      arrow keys); ask a person to update the Test Runner from the Environment
      page."
    - Otherwise call `controller_scroll`, then `_ensure_action_succeeded`, and
      return "Scrolled <direction> <amount> notches at (x, y) / at the pointer.
      Take a screenshot to see the result."
    - No built-in verification: the agent needs a screenshot anyway to read the
      revealed content.
- `click_at(x, y, expected_result?)`.
  - The description says it is for targets OmniParser cannot name: window
    chrome, a blank area, the end of a text line, a scrollbar. Coordinates come
    from take_screenshot, which reports pixel coordinates. A real left click
    moves keyboard focus. Never use it for web pages opened with
    browser_navigate.
  - `tools_controller.click_at` does **not** call the OmniParser
    resolver/candidate verifier, because there is no element to resolve.
  - Without `expected_result`: `controller_click(x, y)`, then
    `_ensure_action_succeeded`, then "Clicked at (x, y)."
  - With `expected_result`: `take_before_screenshot`, click, then
    `confirm_action(..., acted_on=(), action_summary="clicked at (x, y)")` with a
    `VerificationBudget(deadline, cancellation_check)`. There is one try and
    never a retry:
    - `confirmed`: "Clicked at (x, y); expected result confirmed."
    - `unexpected`: `format_unconfirmed_message`, not an error.
    - `missed`: `is_error=True`, "Clicked at (x, y), but the expected result is
      NOT shown: <reason>. Take a screenshot and ask for the target's
      coordinates again."
  - This reuses the existing outcome verifier at the cost of 2 screenshots and
    1 vision call, and only when asked. Without a default expectation there is
    no vision call when none was asked for. Focusing a text field rarely
    changes the screen enough for a default expectation to say YES.
- Coordinates the model can use:
  - `vision_qa.answer_screenshot_question(..., *, image_size: tuple[int, int] |
    None = None)`. When it is given, the system prompt adds: "The screenshot is W
    x H pixels. Give any screen position as pixel coordinates (x, y) from its
    top-left corner."
  - `tools_controller.take_screenshot` passes `(result["width"],
    result["height"])` and appends `(Screenshot: W x H pixels; click_at and
    scroll use these coordinates.)` to its result.
  - `result_chat_tools` keeps calling it without a size.
- `projects/services.py`: `controller_scroll(project_id, direction:
  ScrollDirection, amount: int, x: int | None, y: int | None, timeout: float =
  30.0) -> ActionResult`.
  - It dispatches `controller.scroll` with `runner_local=True` and passes `x`/`y`
    only when set, because the builder skips missing keys.
  - `projects/controller_protocol.py`: `ScrollActionEvent(BaseActionEvent)` with
    `direction: str`, `amount: int`, and `NotRequired` `x`/`y`. Also add the
    `"controller.scroll": MessageType.SCROLL` map entry and the
    `("direction", "amount", "x", "y")` payload keys.
  - `projects/controller_consumer.py`: `controller_scroll` handler
    (`_forward_action`).
- `projects/runner_capability_services.py` needs no new function. The tool uses
  `runner_supports`, as `close_app` does.
- `machine_setup`: setup helpers get both tools through `get_all_tool_definitions`
  and `dispatch_tool_call`. `build_machine_dispatcher` needs no change, because
  the old-runner case is a tool error, not `RunnerOutdatedError`.

**Item 5: prompt wording**
- `orchestrator_prompts.build_plan_system_prompt`, the "CRITICAL" rule. Keep the
  "self-contained" requirement, but change the example to name the app for GUI
  work only. Add: "Shell work is a command: write the command and what its output
  must show (e.g. 'Run `sencha which` with execute_command and confirm it prints
  8.1.0.42'). Never write 'in Terminal', 'in a Terminal window' or 'in a login
  shell' unless the case tests the terminal application itself."
- `PLAN_SCOPE_RULES`: add one bullet. "Merge consecutive read-only steps (shell
  commands that only read state, and look-only checks of the same screen) into
  one step that lists every check in its expected_result, also when the case
  lists them as separate steps. Never merge an action that creates, submits,
  sends or changes data." This is consistent with the existing "safe to repeat"
  rule and is inherited by both chat redesign prompts on purpose.
- `prompt_parts.build_shell_rules` (both OS branches, in the shared
  `background` text):
  - "A shell command always goes through execute_command, also when the step
    says 'in Terminal', 'in Command Prompt' or 'in a shell'. Open a terminal
    window only when the step tests the terminal application itself."
  - Shell-only steps need no screenshot (already partly said; keep one
    sentence).
- `prompt_parts.build_tool_taxonomy`: add `scroll` and `click_at` to DESKTOP
  TOOLS and renumber.
- `prompt_parts.build_desktop_tool_examples`:
  - Examples `scroll(direction="down", amount=5, x=640, y=500)` and
    `click_at(x=812, y=233, expected_result="the caret is at the end of the
    Editor 3 text")`.
  - Bullets:
    - When content is outside the visible area, use scroll at a point over that
      area, not PageDown or zoom.
    - When click cannot find a target (window chrome, blank area, end of a text
      line), ask take_screenshot for its pixel coordinates and use click_at.
      Never fake a click with AppleScript, osascript, ctypes or other scripts:
      they do not move keyboard focus.
- `prompt_parts.build_tool_guidelines` gains `build_action_batching_rules()`
  (new function in the same module): "When the next actions are predictable and
  need no look in between (e.g. key_press cmd+l, type_text the URL, key_press
  Return), call them together in one response; they run in order. Observe once
  afterwards." `setup_task_prompt` does not call `build_tool_guidelines`;
  leaving the batching rule out of it is acceptable (helpers are not on the run
  hot path).
- `sub_agent_prompts._build_state_section`: replace "Take a screenshot first to
  see the current screen before acting" with "Before your first GUI action, take
  a screenshot to see the current screen. If this step only runs shell commands,
  do not take one."
- `sub_agent_prompts._build_time_budget_section(timeout_seconds, *,
  final_turn_on_timeout: bool)`:
  - When on, replace "fails automatically" with: "When the budget runs out you
    get one last turn without tools to give your RESULT. PASS then only counts
    if you had already verified the expected result, so verify as soon as the
    action is done."
  - When off (teardown), keep today's text.

### Runner (items 3, 4)

**Scroll per backend** (`desktop_pyautogui.py`, `desktop_gnome.py`)
- A notch is one wheel click.
- macOS: move to the point (already mapped by the executor). Then post `amount`
  `CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, dy, dx)`
  events of 3 lines each: up is dy=+3, down dy=-3, left dx=+3, right dx=-3.
  The signs are to be confirmed on a Mac (Verification). Fix them in one
  table, `_MAC_SCROLL_DELTAS`.
- Windows (`sys.platform == "win32"` guard):
  - `pyautogui.moveTo`, then `ctypes.windll.user32.mouse_event` with
    `MOUSEEVENTF_WHEEL` (0x0800) or `MOUSEEVENTF_HWHEEL` (0x01000), and
    `dwData = ±amount * 120`. Up and right are positive.
  - Declare `argtypes`, with a signed `c_long` for `dwData`.
  - It passes the win32 mypy hook.
- GNOME:
  - `GnomeRemoteSession.pointer_axis_discrete(axis: int, steps: int, *,
    stream_path: str)`, alongside `pointer_button`.
  - `GnomeDesktopBackend.scroll` checks bounds when x/y are given, moves the
    pointer, then sends `NotifyPointerAxisDiscrete` with axis 0 for vertical and
    1 for horizontal, and steps ±amount. Positive is down/right, the libinput
    convention.
  - Confirm on the 26.04 VM together with the open Wayland checks.
- `executor.execute_scroll(payload) -> ActionResultPayload`: first
  `ensure_input_not_blocked()` (Windows UIPI drops wheel events too), then the
  point mapping when x/y are given, then `_DESKTOP.scroll`, with the same error
  mapping as `execute_click`.
  `client._handle_scroll` follows `_handle_double_click`, registered under
  `MessageType.SCROLL`.
- `input_guard.py` is unchanged. The guard is already called first by every
  input action, scroll included.

**Screenshot-pixel to input mapping, on every OS and every pointer action**
(decided with the user; see Resolved questions, Q3)
- **One place.** `executor.py` gets `_to_input(x, y) -> tuple[int, int]`, which
  returns `map_to_input(x, y, ScreenSpaces(_DESKTOP.capture_size(),
  _DESKTOP.input_size()))`.
  - `execute_click` (also used by `click_at`), `execute_double_click`,
    `execute_hover`, both ends of `execute_drag`, and `execute_scroll` with a
    point call it after `ensure_input_not_blocked()` and before the backend.
  - The server keeps sending screenshot-pixel coordinates. The protocol and
    the payloads are unchanged.
  - Result messages name both: "Clicked (x, y) [input (ix, iy)] ...", with the
    input part only when the two differ, so logs show any scaling.
- **Reference capture.** `capture_size()` is the size of the backend's latest
  screenshot, which is what the agent's coordinates came from.
  - Every desktop screenshot passes through `backend.screenshot()`:
    `take_screenshot`, the verifier's before/after, and the OmniParser parse
    via `controller_screenshot`.
  - Playwright screenshots do not, so they never change it.
  - With none yet (a runner restarted between the screenshot and the click),
    the backend captures once to learn the size: one `screencapture` or
    `grab`, about 0.3 s, once per runner process.
- **`input_size()` per backend**
  - pyautogui (macOS and Windows): `pyautogui.size()`, read on every call so
    that a resolution change is seen.
    - macOS: main display points.
    - Windows: primary monitor in the process's DPI space. The mapping
      therefore covers DPI-aware (match, no-op), DPI-unaware (scales) and
      system-DPI ≠ monitor-DPI (scales) alike.
    - At startup the runner logs whether the process ended up DPI-aware
      (`user32.IsProcessDPIAware()`, `win32` guard) next to both sizes, so a
      mismatch is visible in the runner log.
  - GNOME: the current stream's frame size (`_known_frame_size()`). Per
    mutter's source this is the stream coordinate space, so the mapping is
    identity by construction, under integer and fractional scaling.
    `_check_bounds` stays as the backend's own guard.
  - `capture_size()`: pyautogui records `image.size` in `screenshot()` (a
    lock-guarded field, as GNOME does). GNOME returns its existing
    `_frame_size` for the current stream, capturing when stale.
- **Effect**
  - Retina Macs: scale 0.5 on both axes. This fixes the OmniParser `click`,
    `double_click`, `hover` and `drag` offset as well as the new tools.
  - Scale-1 Macs, DPI-aware Windows and GNOME: no change.
- **Multi-monitor limits** (unchanged by this plan, now written down)
  - Every backend captures and drives only one monitor:
    - macOS: the main display, the one with the menu bar.
    - Windows: the primary monitor (`ImageGrab.grab()` without
      `all_screens`; `GetSystemMetrics(0/1)`).
    - GNOME: the primary monitor's stream.
  - Content on other monitors is never seen and cannot be clicked.
  - The origin is that monitor's top-left on every OS: global (0, 0) on macOS
    and Windows, and the stream origin on GNOME.
  - On Windows with mixed-DPI monitors only the primary's scale matters,
    because the capture is primary-only.
- **Quartz stub** (`controller_client/stubs/Quartz/__init__.pyi`) gets only
  what scroll and modifier release use: `CGEventCreateScrollWheelEvent`,
  `kCGScrollEventUnitLine`, `CGEventSourceFlagsState`,
  `kCGEventSourceStateCombinedSessionState`, and the four
  `kCGEventFlagMask{Shift,Control,Alternate,Command}` constants. The mapping
  needs no Quartz call: `pyautogui.size()` already reads the display in
  points.

**Item 4: release modifiers** (`desktop_pyautogui.py`)
- `_release_modifiers()` is called at the end of `press_keys` when `len(keys) >
  1`, and at the start of `type_text`.
  - macOS:
    1. Read `CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)`.
       When any Shift/Control/Option/Command mask is set, log WARNING
       `Modifier flags still set before typing: 0x...`. This is the evidence for
       Q4.
    2. `pyautogui.keyUp` for `shift`, `shiftright`, `ctrl`, `ctrlright`,
       `option`, `optionright` and `command`. A lone modifier key-up is only a
       flagsChanged event on macOS and has no side effect.
    3. Wait 20 ms.
  - Windows (`win32` guard): release only the keys that
    `user32.GetAsyncKeyState(vk) & 0x8000` reports down, among VK_LSHIFT,
    VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN and
    VK_RWIN. Release them with `pyautogui.keyUp` on the matching name. A lone
    Win or Alt key-up could open Start or the menu bar if sent blindly.
- GNOME: no change. `press_keys` / `type_text` already release every keysym they
  pressed in `_then_release`. Mutter's virtual keyboard holds no other state, and
  releasing keys it never pressed could log clutter warnings. Record this
  decision in a comment.
- How to reproduce F10/Q4 on the Mac runner: open TextEdit, `key_press
  "ctrl+cmd+f"` (full screen), `key_press "ctrl+cmd+f"` again, then `type_text
  " E1 typed"`. Before this change the emoji picker opens and characters go
  missing. After it, the runner log shows whether flags were still set, and the
  text arrives intact.

### Pages and forms
None.

### Background work and WebSockets
- New controller action `controller.scroll` → runner message `scroll {direction,
  amount, x?, y?}` → `action_result` or `error`. It is on the same `execution`
  queue path as every `controller_*` call. There are no new Celery tasks.
- `click_at` reuses `controller.click`.

### Settings / infrastructure
- `AGENT_FINAL_ANSWER_TIMEOUT_SECONDS` (int, default 90; 0 disables the final
  turn). Read in `auto_tester/settings.py` beside the `SUB_AGENT_*` settings and
  added to `example.env` with a comment.
- Runner: `CLIENT_VERSION` 0.7.0. There is no new dependency, because pyobjc
  Quartz is already present through pyautogui on macOS.
- Deploy: restart the execution workers. Runners update by downloading the new
  ZIP.

### Data flow
- Timed-out step:
  1. `run_tool_loop` hits the deadline and sends one capped LLM call with
     `tool_choice=none`.
  2. The model answers `RESULT: PASS|FAIL`, then `_parse_sub_task_result`
     builds the `SubTaskResult`.
  3. The orchestrator records the attempt as usual. A PASS skips
     evaluate/recover/help.
- click_at:
  1. `take_screenshot("where is the end of the Editor 3 text?")` answers with
     pixel coordinates. It now knows the image size.
  2. `click_at(x, y)` → `controller_click` → runner `execute_click` →
     `_to_input` (screenshot pixels to input space, per backend) → backend →
     real mouse event.
- scroll:
  1. Capability check. An old runner gets a tool error with a key_press
     fallback.
  2. `controller_scroll` → runner `execute_scroll` → input guard →
     `_to_input` when a point is given → backend wheel events.

## Alternatives considered
- **cwd as a protocol field so the runner does the `cd`.** Old runners ignore
  unknown fields and would run the command in the wrong directory. Server-side
  quoting works with every runner.
- **Final turn always on, inside `run_tool_loop`.** The setup report parser
  forces FAILED on TIMEOUT, and the setup chat already has its own
  continue-after-time-limit UX. A per-caller policy keeps both unchanged.
- **Final turn with tools refused by the dispatcher.** That would cost one more
  round trip whenever the model calls a tool anyway. `tool_choice="none"`
  forbids calls in the API and keeps the request prefix identical.
- **A new `AgentStopReason.ANSWERED_AFTER_TIMEOUT`.** It would ripple into
  `classify_turn_end` and `parse_subtask_report`. A boolean on `AgentResult` is
  read only by the one parser that cares.
- **`click_at` as a new message type with its own capability.** It is
  byte-for-byte the existing `click`. A new type would only make old runners
  unable to use it.
- **Default verification for `click_at`.** A focus click often changes nothing
  visible, so a default "UI reacted" check would report misses and cost a
  vision call on every click. Verification is opt-in through
  `expected_result`.
- **`pyautogui.scroll`/`hscroll`.** They are broken on Windows (no
  `WHEEL_DELTA`, horizontal becomes vertical) and in macOS horizontal (sign
  bug), so the backends post wheel events directly.
- **Scroll on an old runner pausing for help, like `double_click`.** That would
  pause every scrolling step until someone updates the runner, while key_press
  fallbacks sometimes work. A tool error lets the agent try, and the text tells
  a person to update. The user confirmed this (Q2).
- **A macOS-only Retina fix using the Quartz display mode
  (`CGDisplayModeGetPixelWidth / GetWidth`).** This was the first draft.
  - The user asked for one general mapping on every OS.
  - Comparing the capture size with the input backend's own size also covers
    Windows DPI-unaware or system-DPI cases, and any future backend.
  - It needs no OS-specific call, because `pyautogui.size()` already reports
    the input space.
- **Mapping on the server.** The server does not know the runner's input space,
  and old runners would then get pre-scaled coordinates. Mapping in the runner
  executor keeps the wire contract "screenshot pixels" unchanged.
- **Mapping inside each backend method.** That would repeat the code five
  times per backend. The executor is the one place every pointer action passes
  through.
- **Releasing modifiers on GNOME too.** The backend already guarantees releases,
  and extra key-ups to Mutter are not free of side effects.

## Risks and mitigations
- **A false PASS in the final turn.** The model could claim success it never
  saw.
  - The prompt demands earlier evidence and FAIL when unsure.
  - The summary is marked "answered when the step's time budget ran out".
  - The verdict LLM and a person reviewing see that marker.
  - The user chose to count it as a normal pass (Q1).
- **Case wall clock.** The grace turn adds up to 90 s per timed-out sub-agent,
  within the per-case Celery soft limit (1800 s + outage allowance). It is
  disabled with 0.
- **Prompt cache / API acceptance of `tool_choice: "none"` with prior function
  items.** Third-party Responses docs confirm the value. OpenAI's own page was
  not reachable in research, so the Verification run below checks it. On a 400
  error the fallback returns today's TIMEOUT, so the worst case is today's
  behaviour plus one rejected request.
- **The mapping changes existing pointer actions wherever the capture and
  input sizes differ.** This affects Retina Macs and DPI-unaware or
  system-DPI-mismatched Windows machines.
  - Equal sizes are a no-op.
  - Where they differ, today's clicks are already wrong by that factor (by
    reading the code; unverified on a real Retina or scaled Windows machine).
  - Verify on one Retina and one non-Retina Mac and on a Windows machine at
    125 % or 150 % scaling before release.
- **Stale reference size.** If the resolution changes between a screenshot and
  a click, the click is scaled from the old screenshot's size into the new input
  space, which is proportionally right. Coordinates from a screenshot of a
  different layout are wrong anyway.
- **First pointer action after a runner restart** costs one extra capture to
  learn the capture size.
- **Scroll direction and sign** on macOS natural scrolling and GNOME. These are
  manual checks, and the signs live in one table per backend.
- **Output cleaning could hide meaningful repeats** (e.g. test output with
  identical lines). The marker states the count, so no information about
  quantity is lost.
- **`$HOME`/`~` expansion.** Only a leading prefix stays unquoted. Anything else
  is literal, which is the safe direction.
- **Windows modifier release.** It acts only on keys the OS reports as down, so
  an unpressed Win or Alt is never sent.
- **Security.** No new user input reaches a shell unquoted; quoting removes an
  injection vector in `cwd`. There is no template change, so no XSS. The runner
  validates every new payload field.

## Implementation steps

Work that every part builds on is written first, then the parts run in parallel.
No file is owned by two parts.

0. **Shared (orchestrator, before sub-agents)**
   - `agents/types.py`: `FinalAnswerPolicy`, `AgentConfig.final_answer`,
     `AgentResult.answered_after_timeout`.
   - `controller_client/protocol.py`: `MessageType.SCROLL`,
     `ClientCapability.SCROLL_V1`, `ScrollDirection`, `MAX_SCROLL_AMOUNT`,
     `ScrollPayload`, `parse_scroll_payload`, `CLIENT_VERSION = "0.7.0"`, and the
     optional-capability comment.
   - `controller_client/desktop_backend.py`: `scroll`, `capture_size` and
     `input_size` on the Protocol, plus the input-space note on pointer methods.
   - New `controller_client/pointer_mapping.py`: `ScreenSpaces` and
     `map_to_input`.

**Server parts (ship first; no runner update needed)**

1. **Part A: final turn.**
   - Files: `agents/services/llm_client.py`, `agents/services/agent_loop.py`,
     `agents/services/sub_agent.py`, `agents/services/orchestrator.py` (only
     `_build_sub_agent_execution_config`), `auto_tester/settings.py`,
     `example.env`.
   - Consumes from Part C: `build_final_answer_prompt() -> str`, and
     `build_sub_agent_system_prompt(..., final_turn_on_timeout: bool = False)`.
2. **Part B: shell pure functions.**
   - Files: `agents/services/shell_command.py` (POSIX quoting; signature
     unchanged) and new `agents/command_output.py`
     (`clean_command_output(text: str) -> str`).
3. **Part C: prompts.**
   - Files: `agents/services/orchestrator_prompts.py`,
     `agents/services/prompt_parts.py`, `agents/services/sub_agent_prompts.py`.
   - Exposes `build_final_answer_prompt()`, the `final_turn_on_timeout` keyword
     on `build_sub_agent_system_prompt`, and `build_action_batching_rules()`.
   - Uses tool names `scroll` and `click_at` with the parameters named in
     Design.
4. **Part D: server side of scroll/click_at, plus output cleaning wiring.**
   - Files: `agents/services/tools_controller.py` (`scroll`, `click_at`,
     `clean_command_output` calls, take_screenshot size line),
     `agents/services/tool_registry.py` (`_handle_scroll`, `_handle_click_at`),
     `agents/services/tool_definitions.py` (two definitions and the `cwd`
     description), `agents/services/vision_qa.py` (`image_size`),
     `projects/services.py` (`controller_scroll`),
     `projects/controller_protocol.py` (`ScrollActionEvent`, map, keys) and
     `projects/controller_consumer.py` (`controller_scroll`).
   - Consumes from Part B: `clean_command_output`.

**Runner parts (ship with the next Test Runner ZIP)**

5. **Part R1: pyautogui backend, executor, client.**
   - Files: `controller_client/desktop_pyautogui.py` (scroll per OS,
     `capture_size`, `input_size`, the Windows DPI-awareness startup log,
     `_release_modifiers`), `controller_client/executor.py` (`execute_scroll`,
     `_to_input` applied in every pointer action, result messages),
     `controller_client/client.py` (`_handle_scroll` and its registration) and
     `controller_client/stubs/Quartz/__init__.pyi`.
6. **Part R2: GNOME backend.**
   - Files: `controller_client/desktop_gnome.py` (`scroll`, `capture_size`,
     `input_size`, plus a comment on modifier handling) and `controller_client/gnome_remote_session.py`
     (`pointer_axis_discrete`).
7. **Integration (orchestrator).**
   - Check the seams: imports, the consumer handler name `controller_scroll`
     matching the `controller.scroll` event type, and the `client.py` handler
     map.
   - Run `pre-commit run --all-files`. This covers isort, black, mypy for the
     server, `mypy-controller-client` and `mypy-controller-client-win32`.
   - Update `agents/CLAUDE.md` (final turn, `click_at`, `scroll`) and the root
     `CLAUDE.md` optional-capability list (`SCROLL_V1`).
   - Then run reviewer and verifier, write
     `docs/changed/2026-10-28-run-speed-round-1.md`, and mark F1, F2–F4, F8–F10
     as addressed in `docs/analysis/run-speed-findings.md`.

## Verification
- Static: `pre-commit run --all-files`, plus `python manage.py check`.
- Item 1:
  - On a macOS or Linux runner, `execute_command(command="pwd",
    cwd="~/Documents/Sencha Cmd")` prints the path.
  - `cwd="/tmp/a b"` after `mkdir -p "/tmp/a b"` works.
  - `cwd="$HOME"` works.
  - On Windows, `cwd="C:\Program Files"` still works.
- Item 6: run `npm install` or `ls --color=always`, and a command printing 50
  identical lines (`yes x | head -50`). The tool result has no `\x1b[` and shows
  the repeat marker. Repeat with `cmd /c dir` on Windows.
- Item 2:
  - Run a case with a step given the minimum budget that has to verify a slow
    page. The log shows `[Agent] Time budget used up; asking for a final
    answer`, and the step comes back PASS with the suffix, or FAIL with "Timed
    out".
  - The first live run confirms the API accepts `tool_choice: "none"` after
    function items (no 400).
  - Set the setting to 0 and confirm the old behaviour.
  - Confirm a machine setup chat turn that hits its time limit still ends with
    `TIME_LIMIT`.
- Item 3 (Mac runner, then Windows, then the GNOME VM):
  - `scroll(direction="down", amount=5, x=..., y=...)` over a long page in a
    native app moves the content down. Check `up`, `left` and `right` too.
  - `click_at` at the end of a text field's line, then `type_text`: the text
    lands in that field (keyboard focus moved).
  - An out-of-screen `click_at` fails with the bounds message on every OS.
  - Mapping:
    - On a Retina Mac, `click` on an OmniParser element hits it, and the
      result message shows `[input (x/2, y/2)]`.
    - A non-Retina Mac shows no input part (no-op).
    - Windows at 125 % scaling: the startup log shows DPI-aware with equal
      sizes, and clicks hit.
    - Optionally run once with the process forced DPI-unaware (compatibility
      setting "Override high DPI scaling: System") to see the scaled path.
    - GNOME VM with fractional scaling (125 %) enabled: clicks hit with no
      input part.
  - An old (0.6.2) runner: `scroll` returns the tool error and `click_at`
    works.
  - Elevated window on Windows: scroll raises `InputBlockedError`.
- Item 4: the reproduction in Design on the Mac runner. Record in the change
  note whether the WARNING fired, which answers Q4.
- Item 5:
  - Plan EXTJS-30117 and C1514863 again (planning only, or a dry run). The plans
    contain no "in Terminal" wording, the read-only steps are merged, and shell
    steps take no opening screenshot in the log.
  - The step-5 style flow uses `click_at` + `type_text`.
- Run the `reviewer` and `verifier` agents in parallel on the full diff.

## Resolved questions
- Q1 (resolved 2026-10-28): a PASS given in the final turn after a timeout
  counts as a normal pass, with the suffix "(answered when the step's time
  budget ran out)" in the summary.
- Q2 (resolved 2026-10-28): on a Test Runner without `scroll_v1`, `scroll`
  returns a tool error with the key_press fallback (the `close_app` pattern), not
  a Runner outdated help pause.
- Q3 (resolved 2026-10-28): a general mapping from screenshot pixels to the input
  backend's coordinate space, in the runner, for every pointer action (click,
  double_click, hover, drag, click_at, scroll at a point) and on every OS. Each
  axis is scaled by input size / capture size, so equal sizes do nothing. See
  "Screenshot-pixel to input mapping". Nothing in it was left undecidable from
  the code. The remaining uncertainty is live confirmation, listed under
  Verification.

## Open questions
None.

## Changelog
- 2026-10-28: created. Revised the same day after 6bfb80f (Windows runner shell
  and OS-aware prompts): cwd quoting moved into `build_shell_command`'s POSIX
  branch, output cleaning covers cmd.exe output, the version goes from 0.6.2 to
  0.7.0, and Windows-only runner code is under `sys.platform == "win32"` for the
  win32 mypy hook.
- 2026-10-28: user answers recorded and Status set to Ready.
  - Q1: a late PASS counts as a pass, with the suffix.
  - Q2: an old runner gets a scroll tool error with the key_press fallback.
  - Q3: replaced the macOS-only Quartz display-mode fix with a general
    capture-size to input-size mapping. It lives in the runner executor
    (`_to_input`), with the pure `controller_client/pointer_mapping.py` and new
    backend methods `capture_size` / `input_size`, and applies to every
    pointer action on every OS.
  - Documented the Windows capture and input DPI paths (pyautogui
    `SetProcessDPIAware`, Pillow 11.2.1 per-monitor-aware grab), GNOME's stream
    space (mutter `meta-stream-source-monitor.c` / `meta-stream-monitor.c`) and
    the multi-monitor limits.
  - Dropped the display-mode functions from the Quartz stub list.
- 2026-10-28: review round 1 fixes.
  - `build_action_batching_rules`: the chaining example no longer uses
    `cmd+l`, which runners outside macOS map to Win+L / Super+L (lock screen).
  - macOS `_release_modifiers` returns early when no modifier flag is set and
    releases only the keys whose flags are set (each key event costs pyautogui's
    10 ms catch-up sleep).
  - `command_output`: a lone ESC no longer swallows a line break; trailing blank
    lines are dropped instead of collapsed into a repeat note.
  - `parse_scroll_payload` x/y reject JSON booleans; `click_at` needs a vision
    config only with `expected_result`; `DEFAULT_SCROLL_AMOUNT` moved next to
    `MAX_SCROLL_AMOUNT` in `controller_client/protocol.py`; CLAUDE.md wording.
  - Rejected: `dict[str, Any]` in `projects.services.controller_scroll` stays
    (a `dict[str, int]` splat fails mypy against the `runner_local: bool`
    keyword); documented inline.
  - Rejected: an escape sequence split across two `wait_for_command` chunks can
    leave a few stray characters. Rare and cosmetic; carrying state between
    chunks is not worth it.
  - Rejected: "Leave time for a final check" (was "final screenshot") also in the
    teardown's budget text. It is true there too and keeps shell-only steps from
    being told to take a screenshot.
