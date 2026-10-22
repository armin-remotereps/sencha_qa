# feat: Agent double-click, 300 iterations, closing apps it no longer needs

Status: Done
Created: 2026-10-22
Request: Three agent changes in one plan, plus one check. (1) The agent can
double-click, on the desktop and inside web pages. (2) The agent's iteration
cap of 30 is too low and becomes 300. (3) The agent closes a desktop app once
no later step needs it, because a leftover window clutters every later screen
that OmniParser parses. (Extra) Confirm that the OmniParser annotated image
reaches gpt-5.6-sol with the same image processing as every other screenshot.

## Context

**Iteration caps.** One iteration is one LLM turn in `_run_agent_loop`, and a
turn may hold several tool calls (`agents/services/agent_loop.py:181`).
- `AgentConfig.max_iterations: int = 30` (`agents/types.py:134`). This is a
  dataclass default. Neither builder relies on it: both pass a value explicitly.
- `AGENT_MAX_ITERATIONS` (default 30, `auto_tester/settings.py:262`) is read
  only by `build_agent_config` -> `run_agent` (`agent_loop.py:107-145`).
  `run_agent` is exported from `agents/services/__init__.py`, but test runs
  never call it. `manage.py test_agent` calls `execute_test_run_test_case`.
- **The cap that governs test runs** is `SUB_AGENT_MAX_ITERATIONS` (default 15,
  `settings.py:333`). `orchestrator._build_sub_agent_execution_config`
  (`orchestrator.py:132`) applies it to every sub-agent and recovery agent.
- Related limits, not iteration counts:
  - `ORCHESTRATOR_MAX_SUBTASKS=30`: the plan is silently truncated to 30
    sub-tasks (`orchestrator.py:183`).
  - Sub-task wall-clock budgets of 150-900 s (`sub_task_budget.py`).
  - The per-case Celery soft limit of 1800 s plus the outage allowance
    (`projects/tasks.py:272`).
- `.env` and `example.env` set `AGENT_MAX_ITERATIONS=30` and
  `SUB_AGENT_MAX_ITERATIONS=15`, so changing the defaults alone does nothing
  where `.env` is in use.

**Desktop click, end to end.**
1. Tool definition `click` (`agents/services/tool_definitions.py`), then
   `tool_registry._handle_click`, then `tools_controller.click`.
2. `tools_controller.click` calls `verified_element_actions.resolve_verified_element`.
   That runs `controller_element_finder.parse_screen` (`controller.find_element`,
   OmniParser on the runner), then `match_element`, then
   `action_verifier.verify_candidate`.
3. `take_before_screenshot`, then `projects.services.controller_click`.
   This dispatches `controller.click` with `runner_local=True` and a 30 s reply
   timeout.
4. `projects/controller_protocol.py` (`ClickActionEvent`, `ActionTypeRegistry`)
   and `projects/controller_consumer.controller_click` send the WebSocket
   message `click {x, y, button}`.
5. On the runner, `controller_client/client._handle_click` calls
   `executor.execute_click`. That runs `input_guard.ensure_input_not_blocked()`
   (Windows UIPI), then `pyautogui.click`.
6. Back on the server, `_ensure_action_succeeded`, then `_confirm_or_finish`,
   which compares before/after screenshots with `default_click_expectation`.

**Browser click.** `browser_click`, then `tools_controller.browser_click`
(`browser_element_finder.find_element_index`, a text-only LLM match over the
stamped element list), then `controller_browser_click`. On the runner it is
`browser_executor.execute_browser_click`, which calls
`_locate_element(...).click(timeout=10 s)`.

**Old runners and unknown input.**
- An unknown message type gets an `UNKNOWN_COMMAND` error reply
  (`client._reject_undecodable_message`). The server turns that into
  `ControllerActionError` (`services._raise_for_error_reply`).
- An **extra field** on a known message is silently ignored by the payload
  parsers. So a `click` that carried `clicks: 2` would single-click on an old
  runner and still report success.
- `CLIENT_CAPABILITIES = tuple(ClientCapability)` advertises every enum member
  automatically.
- `REQUIRED_CLIENT_CAPABILITIES` is checked at handshake. Optional capabilities
  are checked at use, as `projects/browser_selection_services.py` does for
  `BROWSER_SELECTION_V1`. `CLIENT_VERSION` is `0.4.0`.

**Apps.**
- `launch_app` (`controller_client/app_launcher.py`) fuzzy-matches discovered
  apps (`app_discovery.compute_match_score`, `MATCH_THRESHOLD=0.7`).
- Process tracking only works on Linux:
  - On macOS it registers the PID of `open -a`, which exits at once.
  - On Windows it calls `os.startfile` and registers nothing.
- So `cleanup_environment` (`cleanup.py`, run before each case) does **not**
  close apps launched on macOS or Windows. They survive into later steps and
  later cases.
- There is no close-app tool. `key_press` is focus-dependent: `cmd+q` with the
  runner's Terminal in front quits the Test Runner itself.
- The planner prompt (`orchestrator_prompts.build_plan_system_prompt`) and the
  sub-agent prompt (`sub_agent_prompts`, `prompt_parts`) say nothing about
  closing apps. The sub-agent state section tells the next step that earlier
  apps "are still open".
- `prompt_parts.build_tool_taxonomy` already omits `launch_app`,
  `check_app_installed`, `wait_for_command` and `browser_list_downloads`. This
  is pre-existing drift.

**Images.**
- Every image the LLM sees is an `ImageContent(base64_data, media_type="image/png")`.
  `llm_serializer._serialize_multimodal_content` turns it into
  `{"type": "input_image", "image_url": "data:image/png;base64,..."}`, with no
  `detail`.
- The sources:
  - Desktop screenshots: `executor.execute_screenshot`, a native-resolution PNG.
  - Browser screenshots: Playwright `page.screenshot(type="png")`.
  - Before/after screenshots: `controller_screenshot`.
  - OmniParser annotated image: OmniParser's `get_som_labeled_img`, a PNG drawn
    on the same native screenshot.
- Versions in use:
  - Server: `requirements.txt`, which has no image library on the agent path.
  - Controller (`controller_client/requirements.txt`): PyAutoGUI 0.9.54,
    playwright ~1.50.0 and Pillow ~11.2.1. `psutil` 7.2.2 and
    `pyobjc-framework-quartz/cocoa` 12.2.1 are present only as transitive
    dependencies.

## Finding: OmniParser image processing (the "extra")

**It matches. No code change is needed.** The OmniParser annotated image takes
exactly the same path as every other screenshot:
- `parse_screen` returns the annotated image.
- `match_element` and `verify_candidate` wrap it in the same `ImageContent`,
  through the same serializer.
- It goes out as a PNG `input_image` data URL with `detail` omitted, which is
  `auto`.
- The server resizes no image anywhere.

OpenAI's vision guide says that for `gpt-5.6-sol`, `auto`/`original` "keeps
dimensions; scales down only above 65,535 px per side". So every image is sent
and seen at native resolution.

The only difference is on the runner and is about transport, not the LLM.
`omniparser_executor.bound_find_element_result` shrinks the annotated PNG in
0.75 steps (LANCZOS, never below a 1280 px long edge), and only when the
`find_element_result` message would exceed `OMNIPARSER_MAX_RESULT_BYTES`
(8 MiB). Element coordinates stay in original-pixel space. The matcher picks an
index, not coordinates, so this is harmless.

Things worth knowing, all unchanged by this plan:
- (a) Native images are costly. A 2880x1800 screenshot is ~5,130 patches,
  about 6,200 input tokens. A verified click sends 3-4 images.
- (b) For GPT-5.6, more than 30,000 patches per image is **rejected** with a
  400, not resized. An 8K screen (7680x4320, 32,400 patches) would block every
  case as `ai_service`.
- (c) Suspected and not in scope: on a Retina Mac, `screencapture` returns
  physical pixels while pyautogui clicks in points. OmniParser centres would
  then be 2x off. This affects click and double-click alike and needs its own
  fix plan if macOS Retina runners are in use.
- (d) The local `.env` still has
  `OPENAI_BASE_URL=https://api.openai.com/v1/chat/completions`. Per
  `docs/changed/2026-10-22-llm-responses-api.md`, every call 400s until it is
  `/v1/responses`. This is a manual fix outside this plan.

## Goals
- Add `double_click`, a verified desktop tool, and `browser_double_click`, a
  Playwright tool.
- Raise the agent loop cap to 300 where it actually governs test runs.
- Add a `close_app` tool that gracefully quits a desktop app by name. Prompt
  rules make the planner schedule closing after an app's last use, and make the
  sub-agent do it as housekeeping that never fails a step.
- Old (0.4.0) runners keep connecting. The new actions are optional
  capabilities.

## Non-goals
- No change to `ORCHESTRATOR_MAX_SUBTASKS`, sub-task time budgets, or the
  per-case Celery limits (see Q1).
- No right-click, triple-click or modifier-click tools.
- No deterministic closing of launched apps in `cleanup_environment` between
  cases (Q3). No fix for macOS/Windows `launch_app` process tracking.
- No `close_app` on Linux runners: it replies with a key_press hint. Linux is
  not an `ApplicationPlatform`.
- No change to image encoding or `detail`, and no HiDPI coordinate mapping.
- No refactor of the tool taxonomy beyond adding the new tools and `launch_app`.

## Design

### Data model
- `projects/models.py`: `BlockedReason.RUNNER_OUTDATED = "runner_outdated",
  "Test Runner needs an update"`. Migration `0022`, choices only, no SQL.
  Assumed by Q2.

### Controller protocol (`controller_client/protocol.py`)
- `MessageType.DOUBLE_CLICK = "double_click"`,
  `MessageType.BROWSER_DOUBLE_CLICK = "browser_double_click"` and
  `MessageType.CLOSE_APP = "close_app"`.
  - These are **new message types** rather than a `clicks` field on `click`. An
    old runner then answers `UNKNOWN_COMMAND` instead of silently
    single-clicking.
- `ClientCapability.DOUBLE_CLICK_V1` (desktop and browser) and
  `ClientCapability.CLOSE_APP_V1`. Both are optional and **not** added to
  `REQUIRED_CLIENT_CAPABILITIES`.
- Payloads:
  - `DoubleClickPayload(x: int, y: int)`: left button only.
  - Browser double-click reuses `BrowserClickPayload` (`element_index`,
    `browser`) and its parser.
  - `CloseAppPayload(app_name: str, force: bool)`. `force` defaults to `False`
    when the field is missing.
  - Add `parse_double_click_payload` and `parse_close_app_payload`.
- `CLIENT_VERSION = "0.5.0"`.

### Controller execution
- `controller_client/executor.py` `execute_double_click(payload)`:
  - First `ensure_input_not_blocked()`, as every input action does.
  - On Windows and Linux: `pyautogui.doubleClick(x, y)`. pyautogui calls
    `click(clicks=2)`, and the OS detects the double-click from timing.
  - **On macOS: do not use pyautogui's double-click.** In 0.9.54,
    `_pyautogui_osx._multiClick` posts two plain down/up pairs and never sets
    `kCGMouseEventClickState`. That is the field AppKit reads as `clickCount`,
    so Cocoa apps see two single clicks.
    - Instead, move to the point and post Quartz mouse events: down/up with
      click state 1, then down/up with click state 2, using
      `CGEventCreateMouseEvent`, `CGEventSetIntegerValueField` and
      `CGEventPost(kCGHIDEventTap, ...)`.
    - The import is platform-conditional at module top, following the
      `input_guard.py` pattern.
    - mypy: add a narrow local stub `controller_client/stubs/Quartz/__init__.pyi`
      covering only those calls and constants, and set `mypy_path` in
      `controller_client/setup.cfg`.
- `controller_client/browser_executor.py` `execute_browser_double_click(session,
  payload)`: `_locate_element(...).dblclick(timeout=_ACTION_TIMEOUT_MS)`, with
  the same error mapping as `execute_browser_click` (not actionable / execution
  failed).
- New `controller_client/app_closer.py` `execute_close_app(payload) ->
  ActionResultPayload`. It may split per platform into `app_closer_macos.py`
  and `app_closer_windows.py` if that is cleaner.
  1. **List running GUI apps.**
     - macOS: `NSWorkspace.sharedWorkspace().runningApplications()`, regular
       activation policy only, giving name and pid.
     - Windows: `EnumWindows` over visible, titled top-level windows via
       `ctypes`, giving hwnd, title, pid and process name (`psutil`).
  2. **Match** `app_name` with `app_discovery.compute_match_score` and
     `MATCH_THRESHOLD`, reused as-is.
     - Windows scores the window title and the process exe stem. For shared
       host processes (`ApplicationFrameHost.exe`) it scores the title only, so
       closing "Calculator" does not close other UWP apps.
  3. **Protect** the runner:
     - never the controller's own pid or any ancestor (`psutil.Process().parents()`;
       this covers Terminal/iTerm/Windows Terminal and the shell);
     - never the console window (`GetConsoleWindow`);
     - never `explorer.exe` / `Finder` / `Dock`.
     A match that is only protected gets the reply "refused: that is the Test
     Runner or the OS shell".
  4. **Close gracefully.**
     - macOS: `NSRunningApplication.terminate()`, a normal Quit.
     - Windows: `PostMessage(WM_CLOSE)` to each matched window.
     - Then poll up to 5 s for the app or windows to go away.
  5. **Reply.**
     - `success=True`, "Closed <name>" when it closed.
     - `success=False`, "No running app matches '<x>'. Running apps: ..." (top
       5) when nothing matched.
     - `success=False`, "<name> is still open; it may be showing a save or
       confirm dialog. Handle the dialog, or call close_app with force=true"
       when it timed out.
  6. **`force=True`.**
     - macOS: `forceTerminate()`.
     - Windows: `taskkill /F /PID` for the matched non-host pids.
     - Never applied to protected pids or to shared host processes.
  - Linux: `success=False`, "close_app is not supported on Linux test
    machines; use key_press alt+F4 with the app focused".
- `controller_client/client.py`: register `_handle_double_click`,
  `_handle_browser_double_click` (the browser one uses `_browser_error_code`)
  and `_handle_close_app`. All of them run in `asyncio.to_thread`, like their
  siblings.
- `controller_client/requirements.txt`: pin `psutil~=7.2.2`, which is used
  directly now, plus its stub package `types-psutil` (look up the current
  version on PyPI). pyobjc stays transitive through PyAutoGUI. If it ever needs
  pinning, that is a separate change.

### Server dispatch
- `projects/controller_protocol.py`:
  - `DoubleClickActionEvent(x, y)`, `BrowserDoubleClickActionEvent(element_index,
    browser)` and `CloseAppActionEvent(app_name, force)`.
  - Registry entries `controller.double_click`, `controller.browser_double_click`
    and `controller.close_app`, with their payload keys.
- `projects/controller_consumer.py`: `controller_double_click`,
  `controller_browser_double_click` and `controller_close_app`, each calling
  `_forward_action`.
- `projects/services.py`, next to their siblings:
  - `controller_double_click(project_id, x, y, timeout=30.0)`, with
    `runner_local=True`;
  - `controller_browser_double_click(project_id, element_index, *, browser,
    timeout=30.0)`;
  - `controller_close_app(project_id, app_name, *, force=False, timeout=30.0)`,
    with `runner_local=True`. The 30 s timeout is well above the runner's 5 s
    close wait.
- New `projects/runner_capability_services.py`:
  - `runner_supports(project_id, capability: ClientCapability) -> bool`, a
    single `values_list("agent_capabilities")` query, as
    `ensure_runner_supports_browser` does;
  - `ensure_runner_supports_double_click(project_id)`, which raises
    `RunnerOutdatedError`.
  `browser_selection_services.py` stays as it is (no refactor).
- `projects/exceptions.py`: `RunnerOutdatedError(InfrastructureError)` with a
  fixed `safe_message`, for example "This Test Runner is too old to
  double-click. Download a fresh Test Runner from the Environment page."
- `projects/case_blocking.py`: map `RunnerOutdatedError` to
  `BlockedReason.RUNNER_OUTDATED`. `blocked_message` returns the server-built
  text.

### Agent tools (`agents/`)
- `tool_definitions.py`:
  - `double_click(description, expected_result?)`, described like `click`
    (verified, up to 10 candidates, element-not-found after 3), with "Not for
    web pages; use browser_double_click".
  - `browser_double_click(description)`.
  - `close_app(app_name, force?)`: "Quit a desktop application the test no
    longer needs so its windows stop cluttering the screen. Never for the
    browser opened by browser tools, or for the Test Runner." The `force`
    parameter says "only after a plain close reported the app is still open and
    discarding unsaved work is acceptable".
- `tool_registry.py`: `_handle_double_click`, `_handle_browser_double_click`
  and `_handle_close_app`. `force` is read as a strict bool, and anything else
  counts as false.
- `tools_controller.py`:
  - `double_click(...)`:
    - First `ensure_runner_supports_double_click(project_id)`. That raises
      `RunnerOutdatedError`, and `safe_tool_call` re-raises it as an
      `InfrastructureError`, so the case is Blocked.
    - Then the same verified flow as `click`. To avoid copying it, extract a
      private `_verified_click(project_id, ..., press: Callable[[int, int],
      ActionResult], verb: str, default_expectation: Callable[[str], str])`
      that both `click` and `double_click` call. `click`'s behaviour and
      messages stay byte-identical.
  - `browser_double_click(...)`: the capability check, then
    `find_element_index`, then `controller_browser_double_click`, mirroring
    `browser_click`.
  - `close_app(...)`:
    - If the runner lacks `CLOSE_APP_V1`, return a **tool error**, not a block:
      "This Test Runner is too old for close_app; close the app from its own
      menu or with key_press (cmd+q on macOS, alt+F4 on Windows) after making
      sure the app is focused". Closing is housekeeping, never the verdict.
    - Otherwise map `ActionResult` to `ToolResult` like `launch_app`.
- `verified_element_actions.py`: `default_double_click_expectation(description)`:
  "the double-click landed on '<d>' and the UI reacted (item opened, edit mode,
  word/row selected, window maximised, or similar)".

### Prompts
- `prompt_parts.build_tool_taxonomy`: add `double_click`, `launch_app` and
  `close_app` under desktop tools and `browser_double_click` under browser
  tools, then renumber.
- `build_desktop_tool_examples` / `build_browser_tool_examples`: one example
  each. Add a rule: "Use double_click / browser_double_click only when the step
  says double-click or the UI needs it (open a file or desktop icon, edit a grid
  cell). Never emulate it with two click calls."
- New `build_app_lifecycle_rules()`, included in `build_tool_guidelines`:
  - Close an app with `close_app` when the step tells you to.
  - Close an app **you opened in this step** that the expected result does not
    need to stay open.
  - Do it only **after** verifying the expected result.
  - A failed or refused close is housekeeping: mention it in the SUMMARY and
    never turn the step into FAIL because of it.
  - Never close the Test Runner or its terminal, never close the browser opened
    by browser tools (it is closed automatically), and never close the
    application under test while the step still uses it.
  - Prefer `close_app` over `key_press` quit shortcuts, which act on whatever
    window has focus.
- `orchestrator_prompts.build_plan_system_prompt`, new rule:
  - When a desktop application the case opened is not used by any later
    sub-task, end the description of the **last** sub-task that uses it with
    "Afterwards, close <App> with close_app (housekeeping, does not affect this
    step's result)".
  - This includes the final sub-task of the case, so apps do not leak into the
    next case.
  - Never add a separate close-only sub-task. Do not schedule closing for the
    browser used by browser tools.
- `sub_agent_prompts._build_state_section`: change "are still open" to "are
  still open unless a summary says they were closed".

### Iterations (Q1 assumption)
- `auto_tester/settings.py`: set the `AGENT_MAX_ITERATIONS` default and the
  `SUB_AGENT_MAX_ITERATIONS` default to 300.
- `agents/types.py`: `AgentConfig.max_iterations` default to 300, so it matches.
- `example.env`: `AGENT_MAX_ITERATIONS=300`, `SUB_AGENT_MAX_ITERATIONS=300`.
- **Manual step:** edit every deployed `.env`, because the local `.env` pins 30
  and 15.
- Effect: the iteration count stops being the binding limit, and the sub-task
  wall-clock budget (150-900 s) decides. A turn is at least one LLM round trip,
  usually several seconds with reasoning, so 300 turns cannot fit in 900 s.
  This is intended.
- There is no new cost ceiling. A looping agent now runs until its time budget
  rather than for 15 turns. `summarize_context_if_needed` keeps the context
  bounded.

### Pages and forms
None. The new Blocked reason is shown by the existing badge, reason column and
"Why this case was blocked" card, which render `blocked_reason_label`.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| (none) | | | | | |

### Background work and WebSockets
- Celery: no new task. Everything runs inside `execute_test_run_case` (queue
  `execution`).
- Controller WebSocket actions (server to runner):
  - `double_click {x, y}` gets `action_result`;
  - `browser_double_click {element_index, browser}` gets `action_result`;
  - `close_app {app_name, force}` gets `action_result`.
- Errors come back as `UNKNOWN_COMMAND` from an old runner, `EXECUTION_FAILED`
  (including UIPI), or `BROWSER_NOT_INSTALLED`.
- Browser-panel WebSocket events: unchanged. Case events already carry
  `blocked_reason` and `blocked_reason_label`.

### Settings / infrastructure
- Only the two defaults change. The close wait (5 s) is a runner-side constant,
  not an env var.

### Data flow
- **double_click:** sub-agent LLM calls `double_click`.
  1. The capability check queries `Project.agent_capabilities`.
  2. `find_element` runs on the runner, the vision match picks an index, and the
     candidate is verified.
  3. The before screenshot is taken and `controller.double_click` is sent. The
     runner runs the input guard, then a Quartz click-state-2 sequence or
     pyautogui, and replies `action_result`.
  4. Settle, take the after screenshot and verify the outcome. The tool returns
     "Double-clicked ... (verified on attempt n)".
  On an old runner the case is Blocked `runner_outdated`.
- **close_app:** the planner puts "Afterwards, close X with close_app" in the
  last step that uses X.
  1. The sub-agent verifies the expected result, then calls `close_app`.
  2. The capability check runs, then `controller.close_app`.
  3. The runner matches the running apps, skips protected processes, sends a
     graceful quit and polls for 5 s.
  4. The reply is the result text. The sub-agent reports PASS with "closed X"
     in its SUMMARY, and the next step's state says X was closed.

## Alternatives considered
- **`clicks: int` field on the existing `click` message.** Rejected: 0.4.0
  runners ignore unknown fields and would single-click while reporting success.
  A capability check would cover it, but a new message type also fails loudly
  if that check is ever missed.
- **A `double: bool` parameter on the `click` tool.** Rejected: a separate tool
  name is easier for the model to choose on purpose, and keeps `click` messages
  unchanged.
- **Making `DOUBLE_CLICK_V1` / `CLOSE_APP_V1` required.** Rejected: every
  deployed runner would be refused at handshake until it is re-downloaded. The
  browser-selection precedent made the new capability optional.
- **Close apps by prompt only, with `key_press` cmd+q / alt+F4.** Rejected: it
  acts on the focused window, and with the runner's terminal in front it quits
  the Test Runner. alt+F4 closes one window, not the app.
- **A separate close-only sub-task per app.** Rejected: it costs a full
  sub-agent run, and its failure would fail the case (a false Jira ticket).
- **Closing all launched apps deterministically in `cleanup_environment`.**
  This is the stronger guarantee across cases. It is deferred (Q3) because
  macOS and Windows launches are not tracked today, and quitting an app that was
  already running before `launch_app` (for example the user's own Terminal) is
  risky.
- **No new abstraction** for capability checks beyond one small
  `runner_supports` service. `ensure_runner_supports_browser` is not
  generalised, to keep the diff focused.

## Risks and mitigations
- **macOS double-click semantics.** The click-state fix is based on Apple's
  documented `kCGMouseEventClickState` and on reading the pyautogui 0.9.54
  source. It is not live-tested. Mitigation: manual check in Finder and
  TextEdit (Verification 2). pyautogui's own path stays the fallback on
  Windows/Linux only.
- **Retries after a double-click.** If outcome verification rejects a
  double-click that did act (for example a file opened), the next candidate is
  double-clicked too. This is the same risk `click` carries today. It is
  accepted, and the default expectation spells out what "reacted" means.
- **close_app closing the wrong thing.** Mitigations:
  - fuzzy-match threshold 0.7;
  - protected pid ancestry, console window and OS shell;
  - title-only matching for shared UWP hosts;
  - graceful close by default, with `force` only on explicit request.
  The prompt forbids the Test Runner, the Playwright browser and the app under
  test while it is in use.
- **macOS Automation (TCC) prompts.** `NSRunningApplication.terminate()` from an
  unsandboxed Python process is not expected to need Automation permission, but
  this is unverified. If a prompt appears, the 5 s wait expires and the agent
  gets "still open". Verify manually (Verification 4) and document the needed
  grant in the runner card if one is required.
- **Unsaved-changes dialogs.** A graceful close leaves the dialog up and
  reports "still open". The agent handles the dialog or forces. This is by
  design.
- **Iterations at 300.** Runaway LLM spend is now bounded only by time
  (sub-task budget at most 900 s; case soft limit 1800 s). Watch token usage
  after rollout.
- **Controller compatibility.**
  - 0.4.0 runners connect. Their double-click cases go Blocked `runner_outdated`
    (Q2), and their close_app calls return a tool error.
  - The ZIP picks up 0.5.0 on server redeploy.
  - `scripts/verify_server_image.sh` must still pass, because the protocol is
    imported server-side.
- **Migration 0022** is choices-only. Before rolling it back, run
  `UPDATE ... SET blocked_reason='internal_error' WHERE blocked_reason='runner_outdated'`.
- **No XSS surface.** Tool texts reach the case logs. They are rendered
  auto-escaped, as today.

## close_tab (added after Q3)

The user's answer to Q3: in some cases only a **tab** must be closed, e.g. a
Terminal tab the test opened, because another tab of the same app runs the Test
Runner itself and must never be closed. Deterministic cleanup stays out of scope.

- Protocol: `MessageType.CLOSE_TAB = "close_tab"`, `CloseTabPayload(app_name)`,
  covered by the same optional `CLOSE_APP_V1` capability.
- Runner (`controller_client/app_closer.py` `execute_close_tab`):
  1. Find the frontmost app (macOS `NSWorkspace.frontmostApplication()`;
     Windows `GetForegroundWindow` -> pid -> process name and title). It must
     match `app_name` (`compute_match_score` >= `MATCH_THRESHOLD`), otherwise
     refuse: "<front app> is in front, not <app_name>; click the tab you want
     to close first".
  2. Guard the Test Runner. If the front app is the runner's own pid or an
     ancestor (its terminal):
     - macOS Terminal / iTerm2: read the selected tab's tty with `osascript`
       and compare it with the runner's controlling tty. Same tty, or tty
       unknown (osascript failed or Automation denied) -> refuse.
     - Windows: the foreground window is `GetConsoleWindow()` -> refuse. Any
       other ancestor (Windows Terminal, tabs not identifiable) -> refuse.
     - Refusal text: "That tab runs the Test Runner" or "cannot tell whether
       the front tab runs the Test Runner; type `exit` in the tab you opened
       instead".
  3. Press the close-tab shortcut through the input guard: macOS `cmd+w`;
     Windows `ctrl+shift+w` for Windows Terminal, `ctrl+w` otherwise.
     Reply "Closed the front tab of <app>". Linux: unsupported, like close_app.
- `close_app` on a protected terminal replies "<app> hosts the Test Runner;
  close only the tab you opened with close_tab".
- Server: `ClosedTabActionEvent`/`controller.close_tab`,
  `controller_close_tab(project_id, app_name, *, timeout=30.0)` with
  `runner_local=True`.
- Agent: tool `close_tab(app_name)`. Prompt rule: when the app also hosts the
  Test Runner (its terminal) or has other tabs the test still needs, close only
  the tab you opened with `close_tab`, never the app. The planner's closing
  note says "close the <App> tab you opened with close_tab" for such apps.

## Implementation steps
1. **Shared contracts first** (sequential, before the sub-agents). Files:
   `controller_client/protocol.py` (message types, capabilities, payloads,
   parsers, `CLIENT_VERSION`), `projects/exceptions.py` (`RunnerOutdatedError`),
   `projects/models.py` + `projects/migrations/0022_*.py` (`RUNNER_OUTDATED`),
   `projects/case_blocking.py`.
2. **Runner** (sub-agent A). Files: `controller_client/executor.py`
   (`execute_double_click`, macOS Quartz path), `controller_client/stubs/Quartz/__init__.pyi`
   (and an AppKit stub if `app_closer` needs one), `controller_client/setup.cfg`
   (`mypy_path`), `controller_client/browser_executor.py`
   (`execute_browser_double_click`), `controller_client/app_closer.py` (plus
   optional per-OS modules), `controller_client/client.py` (handlers),
   `controller_client/requirements.txt` (`psutil`, `types-psutil`).
3. **Server dispatch** (sub-agent B). Files: `projects/controller_protocol.py`,
   `projects/controller_consumer.py`, `projects/services.py` (three
   `controller_*` functions), `projects/runner_capability_services.py` (new).
4. **Agent tools and prompts** (sub-agent C; it consumes the step 3 signatures
   as specified above). Files: `agents/services/tool_definitions.py`,
   `tool_registry.py`, `tools_controller.py` (`_verified_click`, `double_click`,
   `browser_double_click`, `close_app`), `verified_element_actions.py`,
   `prompt_parts.py`, `sub_agent_prompts.py`, `orchestrator_prompts.py`.
5. **Iteration caps** (sub-agent D, or done directly). Files:
   `auto_tester/settings.py`, `agents/types.py`, `example.env`.
6. **Integrate and document** (orchestrator). Wire the imports, run
   `pre-commit run --all-files` (server mypy plus the controller mypy config),
   `python manage.py check` and `scripts/verify_server_image.sh`. Update
   `agents/CLAUDE.md` (double-click verified like click; close_app rules) and
   the `controller_client` section of `CLAUDE.md` (new actions, 0.5.0). Write
   `docs/changed/2026-10-22-agent-double-click-close-app.md` at the end.

## Verification
- Manual checks. There are no automated tests in this project.
  1. `pre-commit run --all-files`, the controller mypy run, `python manage.py
     check` and `python manage.py migrate --plan` (shows only 0022).
  2. **macOS runner (0.5.0):** a case "double-click the Documents folder in
     Finder" opens it. A case "double-click a word in TextEdit" selects the
     word. Then repeat on a Windows runner with File Explorer and Notepad.
  3. **Browser:** a test page with an `ondblclick` handler (or a grid cell that
     edits on double-click): `browser_double_click` triggers it in chromium and
     in firefox.
  4. **close_app:**
     - Launch Calculator and TextEdit/Notepad and close them by name. The app
       is gone and the reply is "Closed ...".
     - With unsaved text, the reply is "still open"; `force=true` then closes
       it.
     - `close_app("Terminal")` / `close_app("python")` on the machine running
       the runner is refused.
     - On Windows, closing "Calculator" leaves other UWP windows open.
     - Check that no macOS Automation prompt appears.
  5. **Planner:** run a case that opens a desktop app in step 1 and only uses
     the browser afterwards. The logs show "Afterwards, close ..." in step 1's
     description, the close happening after verification, and the step PASS.
     The next case's first OmniParser parse shows no leftover window.
  6. **Old runner (0.4.0):** it connects. A double-click case ends Blocked
     `runner_outdated` with the fixed message, and close_app returns the
     key_press hint as a tool error.
  7. **Iterations:** with `.env` updated, a case log shows "Agent iteration
     n/300". A long step ends by sub-task timeout, not `max_iterations`.
- Run the `reviewer` and `verifier` agents in parallel on the combined diff.
  Give `verifier` the original request and this plan.

## Open questions
- **Q1. Which "30" should become 300?**
  - The only live per-step cap in test runs is `SUB_AGENT_MAX_ITERATIONS`, which
    is **15**, not 30.
  - `AGENT_MAX_ITERATIONS=30` only affects `run_agent`, which test runs do not
    use.
  - The other live 30 is `ORCHESTRATOR_MAX_SUBTASKS`, the number of plan steps;
    longer cases are silently truncated.

  Should 300 apply to the per-agent loop caps, to the sub-task cap, or to both?
  If it applies to sub-tasks, the 1800 s per-case time limit would also need
  raising. (Assumption in this draft: `AGENT_MAX_ITERATIONS` and
  `SUB_AGENT_MAX_ITERATIONS` both become 300. `ORCHESTRATOR_MAX_SUBTASKS` and
  the case time limit stay unchanged.)
- **Q2. What should happen on an old Test Runner when a case needs
  double-click?** Block the case with a new reason, "Test Runner needs an
  update" (no Jira ticket), or let the agent see a tool error and probably fail
  the step? (Assumption: Block with a new `runner_outdated` reason. An old
  runner's `close_app` only returns a tool error.)
- **Q3. Should the Test Runner also close the apps it launched when it cleans
  up between cases?** That is a deterministic safety net if the agent forgets.
  It needs launch tracking on macOS and Windows, and must skip apps that were
  already running. (Assumption: out of scope here. Agent-driven closing only,
  with a follow-up plan if wanted.)
- **Q4. Should double-click also cover web pages (`browser_double_click`)?**
  The agent is forbidden from using desktop clicks inside a web page.
  (Assumption: yes, both desktop and browser.)

## Resolved questions
- Q1: per-agent loops. `AGENT_MAX_ITERATIONS`, `SUB_AGENT_MAX_ITERATIONS` and the
  `AgentConfig` default become 300; sub-task cap and case limit unchanged.
- Q2: Block with `runner_outdated`.
- Q3: agent-driven closing only, plus `close_tab` (see above).
- Q4: desktop and browser double-click.

## Changelog
- 2026-10-22: created (Draft).
- 2026-10-22: questions answered; `close_tab` added; In progress.
- 2026-10-22: review pass 1: reverted an accidental `CLEANUP_TIMEOUT_SECONDS`
  default change; terminal hosts (Windows Terminal handoff, iTerm2 and other
  macOS terminals that are not ancestors) are now protected fail-closed;
  platform dispatch and console lookup done once. Pass 2 clean.
- 2026-10-22: deferred (verifier, optional Low): treat an unreadable Windows exe
  name as a possible terminal host; raise when the runner's own tty read fails.
  Both reads do not fail in practice; worth revisiting if a runner ever closes
  its own host. `ssh localhost` from a Terminal tab defeats the tty check
  (documented, not fixed).
- 2026-10-22: Done.
