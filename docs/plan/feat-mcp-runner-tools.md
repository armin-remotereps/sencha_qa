# feat: The agent's machine tools as MCP project tools

Status: Done
Created: 2026-11-08
Request: "For mcp, add all tools that local ai has, like playwright, run command,
launch app, ... all of them."

**Purpose (user's framing): "MCP is for the AI that comes to help a human while
doing manual tests."** Every MCP tool is scoped to a Manual case that the caller
has claimed. There is no machine control outside one.

An MCP client claims a Manual case, performs it and hands it in. Today it cannot
drive the test machine while doing so. This change adds the server agent's tools
as MCP project tools that work **only inside a Manual case the caller claimed
through MCP**:

- commands, with detach, list, re-attach and stop;
- mouse and keyboard, by description or by coordinates;
- apps;
- the Playwright browser;
- phone gestures;
- web search and wait;
- an analysis-only screenshot.

Each tool is a thin adapter over the agent's own dispatch path. Evidence stays
with the existing `capture_screenshot` / `add_screenshot`.

## Decisions (user's answers, 2026-11-08)
- **D0 Existing tools unchanged.** The new tools are added alongside the 15 MCP
  tools that exist today. Every existing tool keeps its name, arguments and
  behaviour:
  - `list_projects`, `get_project`, `list_runs`, `list_cases`, `get_case`;
  - `claim_case`, `add_screenshot`, `list_windows`, `capture_screenshot`,
    `mark_step`, `hand_in`, `release_case`;
  - `list_knowledge`, `search_knowledge`, `read_knowledge`.

  This includes their "a tester" attribution and the Manual workflow text
  (`workflow.py` only gains a paragraph). Three internal points touch them, and
  none changes a contract:
  - `list_windows` and `capture_screenshot` take the renamed shared machine
    lock. They can answer the existing `[machine_busy]` while a control action
    holds it, as they already do while another capture holds it.
  - `get_project`'s response gains one additive `machine` block (`target`,
    `connected`). Every existing field is unchanged.
  - After `hand_in` / `release_case` commits, the claim's kept commands are
    stopped as best effort (D7). Their responses are unchanged.
- **D1 Security gate: none.** There is no per-project opt-in, Settings card or
  model field. Connection keys and OAuth grants may both use every tool, within
  their own MCP claim.
- **D2 Scope: only inside the caller's own live MCP claim.**
  - Every new tool takes required `pivot_id` and `claim_token`, the same as
    `capture_screenshot`. That includes `take_screenshot`, `web_search` and
    `wait`.
  - A call is allowed only while that claim is the caller's live MCP claim on
    this project (`own_claim.load_own_claim`, which accepts only
    `manual_claim_source == MANUAL_SOURCE_MCP`). Otherwise the existing claim
    refusals apply.
  - No "machine idle" mode and no reverse hold. The live MCP claim already keeps
    agent runs, Machine setup, readiness checks and runner updates off the
    machine; see "What a live MCP claim already blocks".
  - Captures and control actions share one per-project machine lock.
  - The person window hides around each action exactly as it does for the
    agent.
- **D3 Which tools: all of the agent's tools except `browser_take_screenshot`.**
  - Included: the description-based tools (`click`, `double_click`, `hover`,
    `drag`, `browser_click` / `browser_double_click` / `browser_type` /
    `browser_hover`, phone `long_press` / `click` / `double_click`), plus
    `web_search` and `wait`.
  - Coordinate forms only where the agent has them (`click_at`, `scroll`,
    `swipe`). The only new primitives are the command-session tools D7 needs.
  - The visual engine and AI cost is accepted. These tools have their own
    throttle and `[visual_engine_offline]` / `[ai_unavailable]` refusals.
- **D4 Screenshots: `take_screenshot`, for analysis only.**
  - Returns a downscaled JPEG preview plus the real screen size the coordinates
    refer to.
  - Never staged and never evidence. It works only within the claim and has its
    own SCREEN throttle.
  - Desktop: the full screen, ignoring the capture allowlist. Phone: the full
    device screen.
  - Reuses `controller_screenshot`.
  - `browser_take_screenshot` is excluded: viewport coordinates are not the
    `click_at` space, and Playwright runs headed, so `take_screenshot` already
    shows the page.
- **D5 Attribution: actions count as the user, like the Test Runner.**
  - An OAuth grant acts as `PersonStepActor.runner(user)`, labelled "<member>
    via Test Runner".
  - A Connection key acts as `PersonStepActor.runner(None)`, labelled "a person
    at the Test Runner".
  - With the hold gone, the new tools write nothing attributed or user-visible
    in the app (see Attribution). The rule stands for any future text.
  - Nothing user-visible mentions AI, MCP or an agent.
  - Manual tools keep "a tester". Aligning them is a possible follow-up, out of
    scope.
- **D6 Audit: Python logging only.** No audit table. The gateway's own log
  lines never contain typed text, command text or other arguments. The
  agent code it reuses keeps its existing debug log lines (the user wants
  logs for debugging). The throttles stay.
- **D7 Commands belong to the claim.**
  - `execute_command(detach=true)` returns the session id at once. Without
    detach, it waits a bounded time, then returns a still-running id the same
    way.
  - Ids are kept in Redis per claim. Only that claim's caller can
    `read_command_output` (re-attach), `stop_command` or `list_commands` them.
  - **Confirmed by the user:** when the claim ends (hand-in, release, web
    Release of a stuck claim, re-test, re-run, reset, run stop), the claim's
    detached commands are stopped. Delivery is best effort: at once after an
    MCP hand-in or release, within about 60 s for any other end, and never
    while the runner is offline (a runner restart kills them anyway).
- **D8 Runner protocol change accepted (user, 2026-11-08).** Optional capability
  `KEPT_SESSIONS_V1`, with `CLIENT_VERSION` going from 0.20.0 to 0.21.0. Limits:
  4 kept commands per claim, a 4 h lifetime and a 1 MiB output buffer each.
  Runners must be updated (Update Test Runner) before `detach` works. Older
  runners get the degraded single-session behaviour described below.

## Context
- **MCP gateway.** Tools are built in `mcp_gateway/mcp_app.py` (`_TOOLS`).
  - `services/call_guard.guarded_call` resolves the project (one query),
    applies a `CallKind` throttle, maps errors to neutral `McpToolError` text
    and logs one line without arguments.
  - Claim checks live in `services/own_claim.py` (`load_own_claim`,
    `refuse_paused`).
  - Runner round trips follow the capture pattern: readiness
    (`projects/runner_capture_readiness.py`), the process slot and per-project
    Redis lock (`services/capture_lock.py`), the controller seam, then `[code]`
    errors (`services/capture_errors.py`).
  - Call deadline: 55 s.
  - Staging rows of ended claims are deleted by the hourly sweep
    (`services/sweep.py`).
- **Agent tools.**
  - Definitions: `agents/services/tool_definitions.py` (`tool_definitions_for(target)`).
  - Dispatch: `agents/services/tool_registry.py`, through `dispatch_tool_call`
    and `allowlisted_dispatcher`.
  - Implementations: `tools_controller.py`, `tools_mobile.py`,
    `tools_search.py`, `tools_utility.py`.
  - Description tools take `ToolContext.vision_config` (`llm_config.build_llm_config()`).
    Retries are bounded by `VerificationBudget(deadline)` on `agent_monotonic`,
    which only stops new attempts.
  - `wait` shortens itself to the deadline.
  - `safe_tool_call` re-raises `InfrastructureError`.
  - Machine setup reuses the set the same way (`setup_tools.build_machine_dispatcher`).
- **Reconnect wait (confirmed).** `wait_for_controller` waits up to 300 s for a
  reconnect while any run is Started, and a Manual run is always Started.
- **Runner command sessions (confirmed).**
  - The runner handles one message at a time.
  - `InteractiveSessionManager` keeps **one** session, and the next start kills
    it.
  - Start and an empty `send_input` each read about 5 s.
  - `wait_for_command` blocks until the command exits.
  - The 300 s limit is enforced on the next read.
  - Unread output fills the pty.
  - `stop()` kills everything.
- **Coordinates (confirmed).** The runner's screenshot `width` / `height` are
  the pointer coordinate space on desktop and phone.
  `controller_client/executor.py` maps them with `pointer_mapping.map_to_input`
  (Retina, unscaled DPI; iOS points through `mobile_ios`).
- **Playwright** is launched headed (`controller_client/browser_executor.py`,
  `headless=False`).
- **Versions.** `mcp~=2.3.0`. `CLIENT_VERSION` is 0.20.0.

### What a live MCP claim already blocks (checked in code)

| Other user of the machine | Blocked today while an MCP claim is live? | Where |
|---|---|---|
| Agent / HITL run Start | Yes: the Manual run is Started, an active run. | `start_test_run` → `_has_active_test_run` |
| Agent run Resume / Re-run / redesign re-run | Yes: another Started run. | `run_reopen_services._list_blockers` → `_has_other_started_run` |
| A Paused agent run (Need help, person step) acting | Impossible: one active run per project. | same |
| Machine setup turn live at claim time | Claim refused. | `manual_case_claim_services` (`live_turn_exists`) |
| Machine setup `run_setup_task` during the claim | Refused while a claimed case is unpaused. | `setup_tools.py` (`manual_tester_on_machine`) |
| Manual Discuss thread (`MANUAL_CASE` role, the only one exempt) | Hidden and refused for MCP claims. | manual Discuss/Pause rules |
| Pausing the MCP claim, which would free the machine for setup | Refused for MCP claims, so the claim is always unpaused. | `manual_case_pause_services` |
| Readiness Scan / Check machine requested during the claim | Refused. | `runner_gates.runner_action_refusal` (`manual_tester_on_machine`) |
| Runner update | Refused while any run is Started; Start is refused during an update. | `runner_update_offer.list_runner_update_blockers`, `start_test_run` |
| Another Manual claim (runner window or MCP) | One claim per run, one active run per project. | `manual_case_claim_services` |
| **Readiness Scan / Check machine already running when the claim was made** | **No (gap)** | Claim does not look at the readiness locks. |

The one real gap is closed for the new tools only (D0 forbids changing
`claim_case`). Their gate checks the readiness locks
(`machine_readiness/project_locks.is_locked(SCAN_LOCK_PREFIX | QUICK_CHECK_LOCK_PREFIX, project_id)`,
two cache reads) and answers `[machine_busy]` while one is held. The scan only
stats files, but a quick check shares the sequential runner. Everything else is
already blocked, so the reverse hold (`remote_tester_hold.py`) and the earlier
refusals (agent run active, setup turn live, runner updating, someone else's
claim) are **dropped**.

## Goals
- Inside their own MCP claim, clients can call every agent tool except
  `browser_take_screenshot`, with the agent's arguments and behaviour through
  `dispatch_tool_call`. The blocking `wait_for_command` is replaced (D7).
  `take_screenshot` is analysis-only.
- Commands survive the call and belong to the claim: detach, list, re-attach,
  stop, and best-effort stop at claim end.
- Bounded calls: no reconnect wait, a deadline per tool class, the shared
  machine lock, per-project throttles.
- No AI or MCP wording anywhere user-visible.

## Non-goals
- Any contract change to the 15 existing tools (D0), including `claim_case`
  gates.
- Use outside an MCP claim, a reverse hold, an opt-in gate (D1, D2).
- An audit table (D6).
- Evidence from any tool other than `capture_screenshot` / `add_screenshot`.
- `browser_take_screenshot` (D4).
- Coordinate or index primitives the agent does not have (D3).
- Changing the manual tools' "a tester" attribution (follow-up).
- Recording MCP actions in case data, logs, TestRail or test sessions.

## Design

### Tool set (36 new, alongside the existing 15; 51 in all)

Every new tool takes `project_id`, `pivot_id` and `claim_token` first, all
required.
- **Desktop and phone** (the agent's phone wording applies on a phone):
  - commands: `execute_command(command, cwd?, detach?)`,
    `read_command_output(session_id, wait_seconds?)`,
    `send_command_input(session_id, input_text)`, `stop_command(session_id)`,
    `list_commands()`;
  - screen and input: `take_screenshot()`, `click(description,
    expected_result?)`, `double_click(description, expected_result?)`,
    `click_at(x, y, expected_result?)`, `type_text(text)`, `key_press(keys)`;
  - apps: `launch_app(app_name)`, `close_app(app_name, force?)`,
    `check_app_installed(app_name)`.
- **Desktop only:**
  - input: `hover(description, expected_result?)`, `drag(start_description,
    end_description, expected_result?)`, `scroll(direction, amount?, x?, y?)`;
  - `close_tab(app_name)`;
  - browser: `browser_navigate(url, browser?)`, `browser_click(description,
    browser?)`, `browser_double_click(description, browser?)`,
    `browser_type(description, text, browser?)`, `browser_hover(description,
    browser?)`, `browser_get_page_content(browser?)`, `browser_get_url(browser?)`,
    `browser_download(url, save_path?, browser?)`, `browser_list_downloads()`.
- **Phone only:** `long_press(description, duration?, expected_result?)`,
  `swipe(direction, x?, y?, distance?, duration?, expected_result?)`,
  `press_button(button)`, `rotate_device(orientation, expected_result?)`,
  `hide_keyboard()`, `open_url(url)`, `install_app(path)`.
- **No runner:** `web_search(query)`, `wait(seconds ≤ 50)`. These run the claim
  check only.

Notes:
- **Arguments.** Handlers declare the agent tool's arguments explicitly. The
  descriptions are written for an MCP client, with no sub-agent or "Test
  Runner" wording.
- **Target.** The tool list is static, because one connection can span desktop
  and phone projects. A mismatch gives `[wrong_target]` before any round trip.
- **`browser`.** Stateless and optional on every browser tool (default
  chromium). Each call uses `BrowserSelection(current=…)`. A browser other than
  chromium calls `ensure_runner_supports_browser`.
- **`wait_for_command`.** Not exposed: it would block the sequential runner for
  up to 300 s. `read_command_output` replaces it.
- **`get_project`.** Gains `machine: {target, connected}` (additive, D0).
- **`take_screenshot` (D4).** Service `machine_screenshot.py`:
  1. Gate, slot, machine lock (60 s) and `no_reconnect_wait`.
  2. `projects.services.controller_screenshot`, the agent path (one retry on
     timeout; the person window hides).
  3. Preview from `image_preview.py`: edge ≤ `MCP_SCREEN_PREVIEW_MAX_EDGE` =
     1568, ≤ `MCP_SCREEN_PREVIEW_MAX_BYTES` = 600 KiB.
  4. Returns a `CallToolResult` like `capture_screenshot`'s: JSON with `width`,
     `height`, `preview_width`, `preview_height` and `note`, plus
     `ImageContent`.
  - No DB, disk or staging write and no `shot_id`, so it can never reach
    `mark_step` / `hand_in`.
  - The client maps a preview point with `x = px * width / preview_width` (y
    alike).
  - A runner that reports 0 x 0 gets `width` / `height` null and a note, as the
    agent does.
  - Desktop shows the full screen and does not follow the allowlist; phone
    shows the full device screen.

### Gate (`mcp_gateway/services/machine_gate.open_machine_session(call, *, pivot_id, claim_token, tool) -> MachineSession`)

Checks in order, all cheap:
1. **Claim.** `load_own_claim(project_id, pivot_id, claim_token, lock=False)`
   plus `refuse_paused` (one query). These are the existing refusals: not
   claimed, ended, not an MCP claim, wrong token.
2. **Target and connection.** `capture_readiness(project_id)` (one query):
   - not connected → `[machine_offline]`;
   - phone device offline → `[device_offline]`;
   - tool not for this target → `[wrong_target]`.
3. **Readiness locks.** A readiness Scan or Check machine is running →
   `[machine_busy]`. Two cache reads; this closes the one gap.
4. **Visual engine** (desktop `click` / `double_click` / `hover` / `drag`, phone
   `click` / `double_click` / `long_press`): `get_visual_engine_status()` has no
   ready worker → `[visual_engine_offline]` (one query).

`web_search` and `wait` run step 1 only. There is no hold and nothing is
written. `MachineSession` carries `project_id`, `pivot_id`,
`claim_started_at` (the claim's `manual_claimed_at`) and `runner_target`.

### Attribution (D5)
- `McpCall` gains `machine_actor`: `PersonStepActor.runner(grant.user)` for
  OAuth (`_resolve_call` adds `grant__user` to its link query, the same query)
  or `PersonStepActor.runner(None)` for a Connection key.
- `McpCall.actor` stays `tester()` for the existing tools.
- The new tools write nothing attributed or user-visible in the app:
  - no case, log, TestRail, test-session, notification or runner-window write;
  - no hold text.

  `machine_actor` is used only in the debug log line (`actor=member|anonymous`,
  never a name) and is the rule for any later user-visible text.
- MCP-facing texts pass `neutral_machine_text` ("Test Runner" / "controller" →
  "test machine"). Refusals never mention MCP, AI or an agent.

### Services (`mcp_gateway/services/`, one use case each)
- `machine_gate.py`: the gate above.
- `machine_round_trip.py`: the renamed `capture_lock.py`.
  - `machine_round_trip()`, `machine_lock(project_id, seconds)` on
    `mcp:machine-lock:{id}`, `machine_busy`.
  - `list_windows` and `capture_screenshot` switch to it.
- `run_agent_tool.py`: the reuse seam.
  1. Builds a `ToolContext`: `project_id`, `BrowserSelection`, `runner_target`,
     `vision_config` / `summarizer_config` from `build_llm_config()`, no
     callbacks, `deadline = agent_monotonic() +` the class deadline.
  2. `allowlisted_dispatcher(MCP_AGENT_TOOL_NAMES)`.
  3. Runs inside `machine_round_trip()`, `machine_lock()` and
     `no_reconnect_wait()`.
  4. Maps re-raised `InfrastructureError`s to `[code]`s: `[machine_offline]`,
     `[machine_unresponsive]`, `[machine_outdated]`, `[browser_unavailable]`,
     `[device_offline]`, `[visual_engine_offline]`, `[ai_unavailable]`.
  5. Neutralises the text and fences page content and search results as data.
  - `web_search` and `wait` skip the slot, lock and no-wait.
- `machine_screenshot.py`: `take_screenshot`.
- `command_sessions.py`:
  - `execute_command(detach)`, `read_command_output` (empty `send_input` polls
    until exit or `min(wait_seconds, 40)`), `stop_command`, `list_commands`;
  - output cleaned (`clean_command_output`), capped at 60 000 chars (head and
    tail) and fenced;
  - On a `KEPT_SESSIONS_V1` runner, every command is started kept through
    `projects.controller_command_sessions.controller_start_kept_command`. It
    reuses the agent's shell preparation: `runner_os_name`,
    `ensure_runner_supports_windows_shell` and
    `agents.services.shell_command.build_shell_command`, the same steps as
    `tools_controller.execute_command`.
  - On an older runner, the call goes through
    `run_agent_tool("execute_command")`, the agent path unchanged.
  - Reads use `controller_read_command_output`, an empty `send_input` that also
    returns `output_dropped`. Input uses the existing `controller_send_input`.
    Stop uses the existing `controller_terminate_interactive_command`. All of
    them run under `machine_round_trip`, `machine_lock` and
    `no_reconnect_wait`.
- `command_registry.py`: claim-owned ids.
  - Redis hash `mcp:commands:{project_id}:{pivot_id}`: `session_id` → JSON
    `{claim_started_at, started_at, detached, command_head (≤ 120 chars, never
    logged)}`.
  - Project ids with entries are kept in the set `mcp:commands:projects`.
  - `read_command_output`, `stop_command` and `send_command_input` accept a
    `session_id` only if it is in this claim's hash with a matching
    `claim_started_at`, else `[command_not_found]`. `list_commands` lists only
    this claim's entries.
  - Entries are removed after a read that sees the exit, after
    `stop_command`, after the claim-end stop, or when the runner does not know
    the id.
  - With `KEPT_SESSIONS_V1`, `list_commands` reconciles with the runner's
    session list. Without it, the hash is returned marked "may have ended".
  - Keys expire at `MCP_DETACHED_MAX_SECONDS` + 1 h.
- `stop_claim_commands.py`: the best-effort stop at claim end.
  `stop_ended_claim_commands(project_id, pivot_id | None)` terminates every
  registered session whose claim is no longer live (the pivot's token is empty
  or `manual_claimed_at` differs from `claim_started_at`), through
  `controller_terminate_interactive_command` under `machine_lock` and
  `no_reconnect_wait`, then deletes the entries. An offline runner leaves them
  until expiry; a runner restart kills them anyway. It is called from two
  places:
  - **MCP ends** (`hand_in`, `release_case`): `transaction.on_commit` sends
    task `mcp_gateway.tasks.stop_claim_commands(project_id, pivot_id)` (queue
    `assist`), so the existing tools' responses and timings are unchanged.
  - **Every other end** (web Release, re-test, re-run, reset, run stop or
    finish, resume clearing a claim; about 7 sites clear
    `MANUAL_CLAIM_CLEARED`): beat task `mcp_gateway.tasks.sweep_ended_claim_commands`
    every 60 s on `assist`. It iterates `mcp:commands:projects` (empty set:
    one Redis call), so the stop comes ≤ 60 s late.
  - Not chosen: on-commit hooks at all 7 `projects` sites. That is invasive,
    `projects` must not import `mcp_gateway`, and the sweep also covers ends
    added later.
- `machine_errors.py`: the `[code]` texts and `neutral_machine_text`. It reuses
  `CaptureErrorCode` values and adds `wrong_target`, `browser_unavailable`,
  `visual_engine_offline`, `ai_unavailable`, `command_not_found` and
  `too_many_commands`.
- `call_guard.py`:
  - new `CallKind` values `CONTROL`, `AI`, `COMMAND` and `SCREEN`;
  - `McpCall.machine_actor`;
  - the log line gains `session` for command tools; still no arguments.

### Throttles and deadlines (`mcp_gateway/limits.py`)

| Kind | Tools | Per project/min | Deadline | Lock TTL |
|---|---|---|---|---|
| CONTROL | `click_at`, `type_text`, `key_press`, `scroll`, `swipe`, `press_button`, `rotate_device`, `hide_keyboard`, `open_url`, `install_app`, app tools, `browser_navigate` / `get_page_content` / `get_url` / `download` / `list_downloads`, `wait` | 120 | 50 s | 60 s |
| AI | description tools and `web_search`; `click_at` / `swipe` / `rotate_device` with `expected_result` | 20 | 45 s for new attempts | 180 s |
| COMMAND | the five command tools | 30 | 50 s | 60 s per round trip |
| SCREEN | `take_screenshot` | 30 | 50 s | 60 s |

Other constants: `MCP_READ_OUTPUT_MAX_WAIT_SECONDS` = 40,
`MCP_EXECUTE_WAIT_SECONDS` = 40, `MCP_DETACHED_MAX_SECONDS` = 4 h,
`MCP_MAX_KEPT_COMMANDS` = 4 per claim (also sent to the runner),
`MCP_COMMAND_OUTPUT_MAX_CHARS` = 60 000. `wait` holds no lock. No env vars.

### Commands (D7) and the wire protocol: a protocol change is required
Today's runner cannot keep a command:
- the next start from anyone kills it;
- a session older than 300 s dies on its next read;
- unread output stalls it.

**Optional capability `KEPT_SESSIONS_V1`, `CLIENT_VERSION` 0.20.0 → 0.21.0.**
`REQUIRED_CLIENT_CAPABILITIES` is unchanged.
- `StartInteractiveCmdPayload` gains optional `keep`, `max_seconds`,
  `initial_read_ms` (the server sends 1000 for detach, 5000 otherwise) and
  `max_kept`. They are sent only to a runner that advertises the capability.
- New message `list_interactive_sessions` → `interactive_sessions`
  (`{sessions: [{session_id, alive, exit_code, elapsed_ms, kept}]}`).
  Runner-local, never resent.
- **Runner** (`controller_client/protocol.py`, `interactive_session.py`,
  `interactive_process_{posix,windows}.py`, `executor.py`, `client.py`):
  - the single foreground session is unchanged (the agent);
  - up to `max_kept` kept sessions are never replaced by a new start;
  - a reader thread drains each kept session into a 1 MiB ring buffer (dropped
    output is flagged) and enforces `max_seconds`;
  - send, read, terminate and wait work by id;
  - a start over the cap gets a coded refusal (`[too_many_commands]`);
  - `stop()` still kills everything;
  - the wording is "kept session" and never mentions MCP.
- **Server seam:** `projects/controller_command_sessions.py` (new):
  `controller_start_kept_command`, `controller_list_interactive_sessions`
  (`resend_on_reconnect=False`, `runner_local=True`), plus forwarders in the
  controller consumer. `controller_start_interactive_command` is unchanged.
- **MCP behaviour:** on a runner with the capability, every MCP command is
  started kept.
  - `detach=true` returns after about 1 s with `session_id`.
  - Otherwise the call polls up to 40 s, then returns `session_id`, `alive`,
    `exit_code` and the output so far.
- **Older runners:**
  - `detach` gives `[machine_outdated]`.
  - A plain command uses the foreground session and is registered with a
    warning that the next command replaces it and it ends after 300 s.
  - `list_commands` marks entries "may have ended".

### Other `projects` changes
- `projects/controller_reconnect_services.py`: a `no_reconnect_wait()` context
  manager (`ContextVar[bool]`, default off). While it is set,
  `wait_for_controller` raises `ControllerNotConnectedError` at once and
  records no outage. It is set only in MCP worker threads and the D7 tasks.
- `projects/controller_command_sessions.py` (see above).
- No other change: no hold, no gate readers, no model and no migration.

### Pages and forms
None.

### Background work and WebSockets
- Celery tasks on the `assist` queue: `mcp_gateway.tasks.stop_claim_commands`
  (on commit) and `mcp_gateway.tasks.sweep_ended_claim_commands` (beat, 60 s,
  registered in a data migration of `django_celery_beat` like the existing MCP
  sweeps).
- Controller consumer: forwarders for `list_interactive_sessions` /
  `interactive_sessions`.
- No browser WebSocket events.

### Import rule (update CLAUDE.md › MCP gateway)
- `mcp_gateway` may also import:
  - `agents.services.tool_registry`, `agents.services.llm_config`,
    `agents.types`, `agents.exceptions`, `agents.command_output`;
  - `projects.controller_outage_clock`, `projects.controller_command_sessions`;
  - `visual_engine.services.pool_status`;
  - `machine_readiness.project_locks`.
- No app imports `mcp_gateway`.
- The `mcp` process now loads the agent runtime (`OPENAI_*`, `SEARXNG_*`,
  channel layer; already in the shared image and `.env`). Check with
  `scripts/verify_server_image.sh` and the compose `mcp` env.

### Data flow (`click` by description)
1. `mcp_app.click(project_id, pivot_id, claim_token, description,
   expected_result?)` → `guarded_call(kind=AI)`: project and grant in one query,
   AI throttle.
2. `open_machine_session`: own claim (1 query), readiness (1 query), target,
   readiness locks (2 cache reads), visual engine (1 query).
3. `run_agent_tool`: slot, `machine_lock(180 s)` and `no_reconnect_wait`, then
   `allowlisted_dispatcher` → `tools_controller.click` (OmniParser, vision
   confirm, `controller_click`, outcome check, 45 s budget). The runner hides
   the person window.
4. A `[code]` error or the neutralised text returns, plus one log line.

## Alternatives considered
- **Machine control outside a claim, with a reverse hold.** Dropped (D2): MCP
  exists for Manual cases, and the claim already provides the exclusion.
- **Fixing the readiness-lock gap in `claim_case`.** Rejected: it changes an
  existing tool (D0). The new tools check the locks instead.
- **Calling `controller_*` seams directly.** Rejected: it would duplicate
  validation, fallbacks, shell wrapping and the verified flows.
- **Generating MCP tools from `ToolDefinition`s.** Rejected: explicit contracts,
  and the agent wording leaks.
- **Detach without a protocol change.** Kept only as the older-runner fallback.
- **On-commit cleanup hooks at every claim-clearing site.** Rejected in favour
  of the 60 s sweep plus on-commit for MCP's own ends.

## Risks and mitigations
- **Remote code execution with no opt-in (D1, accepted).**
  - Any holder of the Connection key can claim a case and run commands as the
    runner's user. A Manual run must be running and a case must be claimable.
  - Regenerating the key or removing the member ends access.
  - Throttles and logs apply.
  - Record the decision in `docs/changed/`.
- **Description tools exceed the client timeout.** An in-flight vision call can
  pass 55 s. The 180 s lock keeps other MCP calls off the machine. Claim-wide
  exclusion keeps everyone else off.
- **Claim-end stop is best effort.**
  - Up to 60 s late for ends outside MCP.
  - Nothing happens while the runner is offline (a runner restart kills kept
    sessions anyway).
  - Sessions started by the older-runner foreground path are stopped the same
    way.
- **Prompt injection.** Page, command and search text is fenced as data, and
  the workflow text repeats the rule.
- **Worker threads.** 8 slots per process. AI calls hold one for about 60 s or
  more. `wait` holds none.
- **No-trace and attribution.** No user-visible text names AI, MCP or an agent.
  `controller_client/` wording is generic. Command text is never logged.
- **Runner change.** Reader threads and the ring buffer on Windows and POSIX
  are untestable without real machines; walk the list on both.

## Implementation split

Three phases:
- **Phase 0:** shared code the orchestrator writes first.
- **Phase 1:** four parts (A to D) in separate files, built in parallel by
  sub-agents. No file is owned by two parts.
- **Phase 2:** the orchestrator integrates and does the final pass.

Every part follows CLAUDE.md: thin adapters, full types, imports at the top,
and `pre-commit` clean.

### Phase 0: shared code (orchestrator, before any sub-agent)
1. **`controller_client/protocol.py`** (stdlib only, imported by both the server
   and the runner):
   - `CLIENT_VERSION = "0.21.0"`, `ClientCapability.KEPT_SESSIONS_V1 =
     "kept_sessions_v1"` (optional, not in `REQUIRED_CLIENT_CAPABILITIES`).
   - Limits:
     - `KEPT_SESSIONS_MAX = 4`
     - `KEPT_SESSION_MAX_SECONDS = 14_400`
     - `KEPT_SESSION_BUFFER_BYTES = 1_048_576`
     - `INITIAL_READ_MS_MIN = 200`
     - `INITIAL_READ_MS_MAX = 5_000`
     - `INTERACTIVE_SESSIONS_MAX_ENTRIES = 32`
   - `StartInteractiveCmdPayload(command: str, keep: bool = False,
     max_seconds: int | None = None, initial_read_ms: int | None = None,
     max_kept: int | None = None)`.
     - The parser accepts the fields being absent (older servers) and clamps
       them to the limits.
     - `keep=True` requires `max_seconds` and `max_kept`.
   - `InteractiveOutputPayload` gains `output_dropped: bool = False` (true when
     the kept buffer discarded output since the last read). Older servers ignore
     it.
   - `MessageType.LIST_INTERACTIVE_SESSIONS = "list_interactive_sessions"`
     (server → runner, no fields besides `request_id`).
   - `MessageType.INTERACTIVE_SESSIONS = "interactive_sessions"` (runner →
     server reply).
   - `@dataclass(frozen=True) InteractiveSessionInfo(session_id: str, alive:
     bool, exit_code: int | None, elapsed_ms: float, kept: bool)`.
   - `@dataclass(frozen=True) InteractiveSessionsPayload(sessions:
     tuple[InteractiveSessionInfo, ...])`.
   - `serialize_list_interactive_sessions_payload() -> dict[str, object]`,
     `parse_interactive_sessions_payload(data: dict[str, object]) ->
     InteractiveSessionsPayload` (≤ `INTERACTIVE_SESSIONS_MAX_ENTRIES`), and
     serialize/parse for the extended start payload.
   - `ErrorCode.TOO_MANY_SESSIONS = "too_many_sessions"`, sent on a kept start
     over `max_kept`.
2. **`projects/exceptions.py`:** `KeptSessionLimitError(ControllerActionError)`.
3. **`projects/controller_reconnect_services.py`:**
   `no_reconnect_wait() -> AbstractContextManager[None]` (`ContextVar[bool]`).
   While it is set, `wait_for_controller` raises `ControllerNotConnectedError`
   at once and records no outage.
4. **`mcp_gateway/limits.py`:** every constant in "Throttles and deadlines",
   plus `MCP_SCREEN_PREVIEW_MAX_EDGE` = 1568, `MCP_SCREEN_PREVIEW_MAX_BYTES` =
   600 KiB and `MCP_KEPT_SWEEP_SECONDS` = 60.
5. **`mcp_gateway/machine_tools.py`** (new, the single tool table): `MachineTool`
   (frozen):

   | Field | Type | Meaning |
   |---|---|---|
   | `name` | `str` | MCP tool name |
   | `agent_tool` | `str \| None` | registry name; None for the command tools and `take_screenshot` |
   | `kind` | `CallKind` | throttle class |
   | `targets` | `frozenset[RunnerTarget]` | where the tool runs |
   | `needs_runner` | `bool` | False for `web_search` and `wait` |
   | `uses_visual_engine` | `bool` | description tools that parse the screen |
   | `ai_with_expected_result` | `bool` | `click_at` / `swipe` / `rotate_device` |

   It also exports `MACHINE_TOOLS: Mapping[str, MachineTool]` and
   `MCP_AGENT_TOOL_NAMES: frozenset[str]`.
6. **`mcp_gateway/services/machine_errors.py`:**
   - `MachineErrorCode(StrEnum)`: the reused `CaptureErrorCode` values plus
     `WRONG_TARGET`, `BROWSER_UNAVAILABLE`, `VISUAL_ENGINE_OFFLINE`,
     `AI_UNAVAILABLE`, `COMMAND_NOT_FOUND` and `TOO_MANY_COMMANDS`.
   - `machine_error(code: MachineErrorCode, detail: str = "") -> McpToolError`.
   - `map_machine_exception(exc: Exception) -> McpToolError | None`:
     `InfrastructureError` subclasses, `ControllerNotConnectedError` and
     `KeptSessionLimitError`.
   - `neutral_machine_text(text: str) -> str`.
7. **`mcp_gateway/services/call_guard.py`:**
   - `CallKind.CONTROL / AI / COMMAND / SCREEN` with their throttles;
   - `McpCall.machine_actor: PersonStepActor`, with the link query adding
     `grant__user`;
   - the log line gains `actor` (member/anonymous) and `session`.
8. **`mcp_gateway/services/machine_round_trip.py`:** the renamed
   `capture_lock.py`.
   - `machine_round_trip() -> ContextManager[None]`,
     `machine_lock(project_id: int, seconds: int) -> ContextManager[None]`,
     `machine_busy(project_id: int) -> bool`.
   - `list_windows.py` and `capture_screenshot.py` are updated to it, with
     unchanged behaviour (D0).
9. **`mcp_gateway/services/machine_gate.py`:**
   - `@dataclass(frozen=True) MachineSession(project_id: int, pivot_id: int,
     claim_started_at: datetime, runner_target: RunnerTarget | None)`.
     `runner_target` is None for tools that need no runner.
   - `open_machine_session(call: McpCall, *, pivot_id: int, claim_token: str,
     tool: MachineTool) -> MachineSession` runs the gate exactly as in the Gate
     section.
10. **`mcp_gateway/schemas.py`:** every request DTO and `Annotated` argument
    type for the 36 tools, and the response DTOs:
    - `MachineActionResponse(text: str)`;
    - `TakeScreenshotResponse(width: int | None, height: int | None,
      preview_width: int, preview_height: int, note: str)`;
    - `CommandResponse(session_id: str | None, output: str, alive: bool,
      exit_code: int | None, output_dropped: bool, kept: bool, note: str)`;
    - `CommandItem(session_id: str, started_at: datetime, detached: bool,
      command_head: str, alive: bool | None, exit_code: int | None)`;
    - `ListCommandsResponse(commands: list[CommandItem], note: str)`;
    - `MachineInfo(target: str, connected: bool)` for the `get_project`
      `machine` block.

### Phase 1: parallel parts (one sub-agent each)

**Part A: runner** (owns `controller_client/interactive_session.py`,
`interactive_process_posix.py`, `interactive_process_windows.py`,
`executor.py`, `client.py`)
- Consumes only the Phase 0 protocol.
- `InteractiveSessionManager`:
  - The single foreground session is unchanged. A non-kept start still replaces
    only the foreground session.
  - Kept sessions: `start_session(command, timeout, *, keep: bool = False,
    max_seconds: float | None = None, max_kept: int | None = None)`. A kept
    start over `max_kept` raises an `ExecutionError` that `client.py` answers
    with `ErrorCode.TOO_MANY_SESSIONS`.
  - `get_session(session_id)` searches the foreground and kept sessions;
    `terminate_session` and `terminate_all` cover both.
  - `list_sessions() -> tuple[InteractiveSessionInfo, ...]`.
- Kept session:
  - A daemon reader thread drains the process into a ring buffer of
    `KEPT_SESSION_BUFFER_BYTES` and sets a dropped flag.
  - The thread terminates the process after `max_seconds`.
  - `read_output` and `send_input` return the buffered text and reset the flag.
    `execute_send_input` reports `output_dropped`.
  - `execute_start_interactive_cmd` uses `initial_read_ms` as the first read
    timeout.
- `client.py`:
  - handler `MessageType.LIST_INTERACTIVE_SESSIONS` → reply
    `INTERACTIVE_SESSIONS`;
  - `LIST_INTERACTIVE_SESSIONS` is added to `_WINDOW_SAFE_MESSAGE_TYPES`, since
    listing touches no screen;
  - advertise `KEPT_SESSIONS_V1` in the handshake.
- No server or MCP wording. Strict mypy with `controller_client/setup.cfg`.

**Part B: server controller seam** (owns
`projects/controller_command_sessions.py` (new),
`projects/controller_consumer.py`, `projects/controller_protocol.py`)
- `@dataclass(frozen=True) CommandOutput(session_id: str, output: str, alive:
  bool, exit_code: int | None, elapsed_ms: float, output_dropped: bool)`.
- Functions:
  - `controller_start_kept_command(project_id: int, command: str, *,
    max_seconds: int, initial_read_ms: int, max_kept: int, timeout: float =
    30.0) -> CommandOutput`. Event `controller.start_interactive_cmd` with
    `keep=True` and the three fields. `TOO_MANY_SESSIONS` raises
    `KeptSessionLimitError`.
  - `controller_read_command_output(project_id: int, session_id: str, *,
    timeout: float = 30.0) -> CommandOutput`. Event `controller.send_input` with
    `input_text=""`; parses `output_dropped`.
  - `controller_list_interactive_sessions(project_id: int, *, timeout: float =
    10.0) -> tuple[InteractiveSessionInfo, ...]`. Event
    `controller.list_interactive_sessions`; reply via
    `parse_interactive_sessions_payload`.
- All three use `dispatch_controller_action(..., resend_on_reconnect=False,
  runner_local=True)`. They raise `ControllerNotConnectedError`,
  `TestRunnerUnresponsiveError`, `ControllerActionError` or
  `KeptSessionLimitError`. No transaction is open.
- Consumer and protocol builder:
  - the start message carries the optional fields only when the event has them
    (agent events never do, so the agent path is unchanged);
  - new event TypedDict `ListInteractiveSessionsActionEvent` and forwarder
    `controller_list_interactive_sessions`;
  - the reply type `interactive_sessions` is routed to the reply tracker, with
    a size cap like `WINDOW_LIST_RESULT_MAX_BYTES`;
  - `output_dropped` is passed through on `interactive_output` replies.

**Part C: agent tool adapters and `take_screenshot`** (owns
`mcp_gateway/services/run_agent_tool.py`,
`mcp_gateway/services/machine_screenshot.py`, `mcp_gateway/image_preview.py`)
- `run_agent_tool(session: MachineSession, call: McpCall, *, tool:
  MachineTool, arguments: dict[str, object], browser: BrowserName =
  DEFAULT_BROWSER) -> MachineActionResponse`, as in Services (ToolContext,
  `allowlisted_dispatcher(MCP_AGENT_TOOL_NAMES)`, slot, `machine_lock`,
  `no_reconnect_wait`, `map_machine_exception`, `neutral_machine_text`, fences).
  A `ToolResult.is_error` raises `McpToolError(neutral text)`.
- `run_utility_tool(call: McpCall, *, tool: MachineTool, arguments:
  dict[str, object]) -> MachineActionResponse` for `web_search` and `wait`: no
  slot or lock, `deadline = agent_monotonic() + 50`.
- `@dataclass(frozen=True) ScreenOutcome(response: TakeScreenshotResponse,
  preview_jpeg: bytes)`.
- `take_screenshot(session: MachineSession) -> ScreenOutcome`.
- `image_preview.py`: add keyword parameters `max_edge: int` and `max_bytes:
  int` defaulting to the current capture constants, so `stage_shot` and
  `capture_screenshot` behave exactly as before.

**Part D: command use cases and claim-end stop** (owns
`mcp_gateway/services/command_sessions.py`,
`mcp_gateway/services/command_registry.py`,
`mcp_gateway/services/stop_claim_commands.py`, `mcp_gateway/tasks.py`,
`mcp_gateway/services/hand_in.py`, `mcp_gateway/services/release_case.py`, the
beat data migration in `mcp_gateway/migrations/`)
- Consumes Part B's three functions by the signatures above (it may stub them
  until B lands), plus the existing `controller_send_input` and
  `controller_terminate_interactive_command`.
- `command_registry.py` (Redis hash `mcp:commands:{project_id}:{pivot_id}`, set
  `mcp:commands:projects`):
  - `@dataclass(frozen=True) KeptCommand(session_id: str, claim_started_at:
    datetime, started_at: datetime, detached: bool, command_head: str)`;
  - `register(project_id: int, pivot_id: int, entry: KeptCommand) -> None`;
  - `owned(project_id: int, pivot_id: int, claim_started_at: datetime,
    session_id: str) -> KeptCommand | None`;
  - `list_owned(project_id: int, pivot_id: int, claim_started_at: datetime) ->
    list[KeptCommand]`;
  - `forget(project_id: int, pivot_id: int, session_id: str) -> None`;
  - `registered_projects() -> list[int]`;
  - `entries_by_pivot(project_id: int) -> dict[int, list[KeptCommand]]`.
- `command_sessions.py`, each taking `(session: MachineSession, call: McpCall,
  request: <DTO>)`:
  - `execute_command(...) -> CommandResponse`;
  - `read_command_output(...) -> CommandResponse`;
  - `send_command_input(...) -> CommandResponse`;
  - `stop_command(...) -> CommandResponse`;
  - `list_commands(session: MachineSession, call: McpCall) ->
    ListCommandsResponse`.
  - The older-runner `execute_command` path calls Part C's `run_agent_tool`.
    The orchestrator wires that seam in Phase 2 if C is not ready yet.
- `stop_claim_commands.py`: `stop_ended_claim_commands(project_id: int,
  pivot_id: int | None = None) -> int`, the number stopped. One query for the
  pivots' claim fields.
- `tasks.py`: `stop_claim_commands(project_id: int, pivot_id: int) -> None` and
  `sweep_ended_claim_commands() -> None`, both `queue="assist"`.
  - `hand_in.py` / `release_case.py` add one `transaction.on_commit(lambda:
    stop_claim_commands.delay(project_id, pivot_id))` after a successful
    commit. Responses are unchanged (D0).
  - A data migration registers the 60 s beat schedule.

### Phase 2: integration and final pass (orchestrator)
- `mcp_gateway/mcp_app.py`: the 36 handlers and `_TOOLS` entries. Each packs its
  DTO, then runs `_run(...)` → `open_machine_session` → the Part C or D
  function.
  - `take_screenshot` returns a `CallToolResult` with `ImageContent`.
  - `destructive_hint=True` on `execute_command`, `send_command_input`,
    `stop_command`, `close_app`, `close_tab`, `install_app`,
    `browser_download` and `drag`.
- `mcp_gateway/workflow.py`: a "Using the test machine while performing your
  claimed case" paragraph.
- `mcp_gateway/services/read_project.py`: the `machine` block.
- Wire the seams (Part D's older-runner path to Part C; Part D's calls to
  Part B).
- Check the queue routing for `assist` and the beat migration.
- Run `pre-commit run --all-files`, `scripts/verify_server_image.sh` and
  `python manage.py check`.
- CLAUDE.md updates: MCP gateway tools, import rule, `KEPT_SESSIONS_V1` in the
  controller capabilities. Write `docs/changed/<date>-mcp-machine-tools.md`.

## Verification
- `pre-commit run --all-files` (server and `controller_client` mypy),
  `scripts/verify_server_image.sh`, `python manage.py check`.
- Manual checks with MCP Inspector, then Claude and Codex, on staging with a
  Windows and a Linux runner:
  1. Without a claim, or with a wrong token, a released claim, or a runner-window
     claim's pivot: every new tool (`web_search` and `wait` too) gets the
     existing claim refusal.
  2. With an MCP claim, over both Connection key and OAuth: `click_at`,
     `type_text` and `launch_app` work.
  3. `click("the Save button", expected_result=…)` works. Vision worker stopped
     → `[visual_engine_offline]`. Broken AI key → `[ai_unavailable]`.
  4. `browser_navigate(browser=firefox)` then `browser_click(description,
     browser=firefox)` works.
  5. Phone project: a desktop-only tool → `[wrong_target]`. `long_press` and
     `swipe` work.
  6. During the claim, Machine setup `run_setup_task`, readiness Scan, agent run
     Start and Update Test Runner all refuse with their existing messages.
  7. Start a readiness Check machine, then claim at once → new tools answer
     `[machine_busy]` until it finishes.
  8. `take_screenshot` returns a preview plus `width` / `height`. A `click_at`
     scaled back from the preview hits the target on a Retina Mac, a Windows
     display at 150 % and an iOS simulator. A UAC or installer dialog is
     visible. Nothing is staged (`get_case` shows no new shot). 31 calls in a
     minute → throttled.
  9. `execute_command("sleep 600", detach=true)` returns at once.
     `list_commands` shows it. A second command does not kill it.
     `read_command_output` re-attaches; `stop_command` ends it. Same on Windows
     (`ping -n 600`).
  10. Another claim (after hand-in, re-claim another case) cannot read, stop or
      list the first claim's ids → `[command_not_found]`.
  11. Detached command, then `hand_in` → the command is gone within seconds.
      Detached command, then web Release of the stuck claim → gone within about
      60 s.
  12. Continuous output for 10 minutes while detached does not stall, and the
      read reports dropped output.
  13. A plain `execute_command("sleep 120")` returns after about 40 s with an id
      and `alive=true`.
  14. A runner without `KEPT_SESSIONS_V1` (0.20.0): `detach` →
      `[machine_outdated]`; a plain command works with its warning.
  15. Stop the runner mid-`launch_app` → fails within about 30 s, no 5-minute
      hang.
  16. A capture and a control call at the same time → one gets
      `[machine_busy]`.
  17. Logs show the tool, project, auth, grant, actor kind, outcome, ms and
      session, and never command text or typed text.
  18. 21 description clicks in a minute → throttled.
  19. Regression (D0): the full existing Manual workflow works unchanged and is
      attributed "a tester". `tools/list` shows 51 tools, with the old 15
      unchanged in name and schema.
- Run the `reviewer` and `verifier` agents (give `verifier` the request, the
  Decisions and this plan).

## Open questions
None. Q8 (protocol change) was accepted as D8, and the claim-end stop was
confirmed in D7.

## Changelog
- 2026-11-08: created (Draft).
- 2026-11-08: user's answers recorded as D1–D7 (no gate; agent tools including
  description-based ones, `web_search` and `wait`; D5 attribution; logging
  only; detach, list, re-attach and stop). Found the `KEPT_SESSIONS_V1`
  protocol need (Q8). Status Ready.
- 2026-11-08: D0 added. The new tools sit alongside the existing 15, which are
  unchanged.
- 2026-11-08: D3/D4 revised. `take_screenshot` is added for analysis only;
  `browser_take_screenshot` stays excluded. 36 new tools, 51 in all.
- 2026-11-08: D2 corrected by the user (Q2 had never been answered).
  - Every new tool requires `pivot_id` + `claim_token` and works only inside
    the caller's live MCP claim.
  - Dropped the idle-machine mode, the reverse hold (`remote_tester_hold.py`)
    and its readers, and the gate refusals a live claim makes impossible (agent
    run active, setup turn live, runner updating, someone else's claim).
  - Code check: the claim already blocks agent runs, Machine setup, readiness
    and runner updates. The one gap (a readiness Scan or Check machine already
    running at claim time) is closed in the new tools' gate.
  - D7 commands are now owned by the claim (Redis per claim; read, stop and
    list limited to it) and stopped as best effort at claim end (on commit
    after MCP hand-in or release; a 60 s beat sweep for other ends; Q9 pending
    confirmation).
  - No `projects` hold or migration remains.
- 2026-11-08: the user confirmed the claim-end stop (D7) and accepted the detach
  protocol change (D8: `KEPT_SESSIONS_V1`, CLIENT_VERSION 0.21.0, 4 kept
  commands per claim, 4 h lifetime, 1 MiB buffer).
  - Added the user's framing to the purpose.
  - Kept commands now start through `controller_start_kept_command`, reusing
    the agent's shell preparation.
  - Replaced the steps with the Implementation split (Phase 0 shared code, Parts
    A to D in parallel, Phase 2 integration) and its exact interfaces.
  - No open questions remain.
- 2026-11-08: implemented (Phase 0, Parts A–D, Phase 2), two review rounds.
  - Deviations: `drag` and `scroll` also run on phones (the agent has them
    there); the 60 s sweep is registered in `CELERY_BEAT_SCHEDULE`, like the
    existing MCP sweeps, not by a data migration; handlers live in
    `mcp_gateway/machine_handlers.py` with the shared `tool_runner.py`
    (both import the SDK).
  - Added from review: a scoped call deadline (`projects/call_deadline.py`)
    that caps controller reply waits, the LLM request and the visual-engine
    parse, with every lock TTL = deadline + margin; `wait` sleeps on the
    event loop; `web_search` behind a process-wide cap, snippets only (the
    server fetches no result page, so no SSRF; an agent-side guard was
    tried and reverted as out of scope), errors as `[search_failed]`; every agent tool reply fenced as data and its wording
    neutralised; orphaned kept sessions stopped; a bounded executor for the
    `mcp` process.
  - Rejected: removing argument text from the agent's existing log lines
    (`tool_utils.safe_tool_call`, `tools_search`): the user wants logs kept
    for debugging (D6 reworded). Making SEND_INPUT window-safe on the runner:
    it changes the agent path; the person window hides and shows on each
    command read, as for the agent.
  - Accepted residuals (final rounds): `neutral_machine_text` is best effort.
    It skips file names, paths and "user agent", but a path segment with
    spaces in which the word is not first (`C:\Program Files\Acme AI
    Tools\`) is still rewritten, and `AI/ML` is kept. A call deadline is a
    hard bound for the caller (`agents/http_deadline.request_by` stops
    waiting at the deadline); an abandoned HTTP transfer ends on its own
    phase timeouts in a daemon thread.
