# feat: Coordinate variants of the pointer tools

Status: Done
Created: 2026-11-09
Request: Every screen tool that today takes only a natural-language description
resolved by the visual engine gets a position variant that takes screenshot-pixel
coordinates, modelled on the existing `click_at(x, y, expected_result?)`:
`double_click_at(x, y, expected_result?)` (desktop and phone),
`hover_at(x, y, expected_result?)` (desktop only),
`drag_at(start_x, start_y, end_x, end_y, duration?, expected_result?)` (desktop and phone) and
`long_press_at(x, y, duration?, expected_result?)` (phone only). They must behave
like `click_at` (non-negative pixel validation, optional single before/after
outcome check, screenshot callback, cancellation and deadline) and be available
everywhere `click_at` is: the agent's tester and setup tools via
`tool_definitions_for(runner_target)` and the MCP machine tools.

## Context

- `click_at` today:
  - Definitions: `agents/services/tool_definitions.py` desktop
    `get_controller_tool_definitions` (~l.193) and phone
    `get_mobile_tool_definitions` (~l.831).
  - Handler: `agents/services/tool_registry.py::_handle_click_at` (x/y through
    `_read_point`: strict ints, both required, non-negative), registered in
    `_TOOL_HANDLERS`.
  - Implementation: `agents/services/tools_controller.py::click_at`: sends
    `controller_click`; without `expected_result` one action and a plain result;
    with it `take_before_screenshot` → act → `verified_element_actions.confirm_action`
    (acted_on `()`, which calls `on_screenshot` with the after image) →
    confirmed / unexpected (`format_unconfirmed_message`, not an error) / missed
    (error, never retried). `vision_config is None` with an expected_result is a
    tool error.
  - `agents/services/tools_mobile.py::_act_and_check` is a near copy of that
    flow for `swipe` and `rotate_device` (different wording only).
- Low-level controller calls already take screenshot pixels:
  `projects/services.py` `controller_double_click(project_id, x, y)` (l.1682),
  `controller_hover(project_id, x, y)` (l.1727),
  `controller_drag(project_id, start_x, start_y, end_x, end_y, button="left", duration=0.5)`
  (l.1744); `projects/controller_mobile_actions.py::controller_long_press(project_id, x, y, duration)`.
- Test Runner: `controller_client/executor.py` `execute_double_click`,
  `execute_hover`, `execute_drag` and `controller_client/mobile_executor.py::execute_long_press`
  all map every point through `to_input` → `pointer_mapping.map_to_input`, which
  also refuses points outside the latest screenshot ("Coordinates (x, y) are
  outside the screen ..."; reaches the server as a failed action →
  `ControllerActionError` → tool error). **No Test Runner change, no new
  capability, no `CLIENT_VERSION` bump.**
- Capabilities: `double_click` calls
  `projects/runner_capability_services.ensure_runner_supports_double_click`
  before acting (`RunnerOutdatedError`, an InfrastructureError: `safe_tool_call`
  re-raises, the case asks for help with fault `runner_outdated`; MCP maps it to
  `[machine_outdated]` in `mcp_gateway/services/machine_errors.map_machine_exception`).
  `hover` / `drag` messages exist on every runner; `long_press` exists on every
  phone runner (`MOBILE_DEVICE_V1` is the phone runner itself).
- Tool availability: the tester sub-agents (`agent_loop` → `tool_definitions_for`)
  and Machine setup (`machine_setup/services/setup_tools.py` →
  `tool_definitions_for`) take the full per-target list, so new definitions reach
  them with no extra wiring. `dispatch_tool_call` enforces the per-target name set
  (`tool_names_for`).
- Allowlists: `case_teardown.TEARDOWN_TOOL_NAMES` has `click` only (no
  `click_at`); `person_steps.CONFIRM_TOOL_NAMES` has no input tools. Both stay
  unchanged. `person_step_prompts._TOOL_NAMES` is derived from
  `get_all_tool_definitions()` (longest first), so the new names are stripped from
  person-facing text automatically.
- MCP: `mcp_gateway/machine_tools.py` (`_TOOLS`, assert `== 52`,
  `click_at` = `CallKind.CONTROL`, `_ALL`, `ai_with_expected_result=True`,
  `uses_visual_engine=False`), `mcp_gateway/machine_handlers.py` (`click_at`
  handler with `ClickAtRequest`, `MACHINE_HANDLERS` row, docstring "The 52 machine
  tools"), `mcp_gateway/schemas.py` (`XArg`/`YArg` over `_MachinePixel`,
  `ClickAtRequest`, `LongPressDurationArg`, `MachineRequest.agent_arguments`
  dumps field names as the agent tool's argument names),
  `mcp_gateway/services/run_agent_tool.py::_ECHO_WITHOUT_CHECK_TOOLS`
  (`click_at`, `swipe`, `rotate_device`), wording that names `click_at` in
  `services/call_guard.py` (`_AI_THROTTLED`), `services/machine_errors.py`
  (visual engine / AI unavailable), `services/machine_screenshot.py` (`_NOTE`,
  `_NOTE_NO_SIZE`), `workflow.py` (server instructions), `limits.py` (comment),
  `mcp_app.py` (counts 52 / 67). `mcp_app.build_mcp_server` registers every
  `MACHINE_HANDLERS` row in a loop.
- Prompts naming `click_at`: `agents/services/prompt_parts.py`
  (`build_tool_taxonomy` numbered lists, desktop rules and examples,
  `build_mobile_tool_examples`), `tools_controller.take_screenshot` size suffix,
  `vision_qa.answer_screenshot_question` docstring.
- Docs: `CLAUDE.md` (MCP section: "52 machine tools", "36 more tools (51 in
  all)", "67 in all"), `agents/CLAUDE.md` (`click_at` and `scroll` paragraph).

## Goals
- Four new agent tools with `click_at` semantics, on the right targets.
- The same four as MCP machine tools inside a claimed Manual case, with the same
  throttle, budget and no-trace behaviour as `click_at`.
- One shared "act once, optionally check once" helper instead of a third copy.
- Prompts and tool descriptions tell the model when to use them.

## Non-goals
- No Test Runner change, protocol change, capability or `CLIENT_VERSION` bump.
- No coordinate variants of the Playwright `browser_*` description tools
  (`browser_click`, `browser_double_click`, `browser_type`, `browser_hover`):
  they act on DOM elements, not screen pixels; the MCP `browse_*` tools already
  address elements by `index` / `css` / `xpath`.
- No `drag_at` `button` parameter (left button, as for `drag`). `drag_at` takes an
  optional `duration` (Q1).
- No change to teardown (`click` only) or person-step confirm allowlists.
- No change to the description tools themselves or to `click_at`'s wording.

## Design

### Data model
None. No migration.

### Services

New leaf module **`agents/services/act_and_check.py`** (one use case: run one
input action, then optionally one outcome check; imports only
`agents.services.tool_utils.safe_tool_call`, `agents.services.verified_element_actions`
(`VerificationBudget`, `confirm_action`, `format_unconfirmed_message`,
`take_before_screenshot`) and `agents.types`; never `tools_controller`, so no
import cycle):

```
def act_once_and_check(
    project_id: int, *, tool_name: str, act: Callable[[], None],
    success_message: str, action_summary: str, expected_result: str,
    vision_config: LLMConfig | None, done_suffix: str, miss_hint: str,
    on_screenshot: ScreenshotCallback | None,
    cancellation_check: CancellationCheck | None, deadline: float | None,
    precheck: Callable[[], None] | None = None,
) -> ToolResult
```

Behaviour (exactly today's `click_at` / `_act_and_check`): inside
`safe_tool_call(tool_name, ...)`: `precheck()` if given; budget
`raise_if_cancelled()`; no expected_result → `act()`, result
`f"{success_message}{done_suffix}"`; expected_result with no vision config →
error "Vision model not configured, so expected_result cannot be checked. Call
{tool_name} without it."; else before screenshot → `act()` → `confirm_action(acted_on=())`
→ confirmed `"{success_message}; expected result confirmed."` / unexpected
`format_unconfirmed_message` (not an error) / missed
`"{success_message}, but the expected result is NOT shown: {reason}. {miss_hint}"`
(error). `act` raises on a failed action (callers wrap
`tools_controller.ensure_action_succeeded(controller_*(...))`).

Migrations onto the helper, wording kept byte for byte:
- `tools_controller.click_at`: `done_suffix="."`,
  `miss_hint="Take a screenshot and ask for the target's coordinates again."`.
- `tools_mobile.swipe`, `tools_mobile.rotate_device`: `done_suffix=". Take a
  screenshot to see the result."`, `miss_hint="Take a screenshot before trying
  again."`; `tools_mobile._act_and_check` is deleted.

New tool functions (all keyword-only after `project_id`, all with
`vision_config: LLMConfig | None`, `expected_result: str = ""`,
`on_screenshot: ScreenshotCallback | None = None`,
`cancellation_check: CancellationCheck | None = None`,
`deadline: float | None = None`, returning `ToolResult`):

| Function | Module | Controller call | precheck | success_message / action_summary |
|---|---|---|---|---|
| `double_click_at(x, y)` | `tools_controller` | `controller_double_click` | `ensure_runner_supports_double_click(project_id)` | "Double-clicked at (x, y)" / "double-clicked at (x, y)" |
| `hover_at(x, y)` | `tools_controller` | `controller_hover` | none | "Hovered at (x, y)" / "hovered at (x, y)" |
| `drag_at(start_x, start_y, end_x, end_y, duration: float)` | `tools_controller` | `controller_drag(..., duration=duration)` | none | "Dragged from (sx, sy) to (ex, ey)" / "dragged from (sx, sy) to (ex, ey)" |
| `long_press_at(x, y, duration: float)` | `tools_mobile` | `controller_long_press` | none | "Long-pressed at (x, y) for {duration:g}s" / same lower-case |

All four use `done_suffix="."` and the `click_at` miss hint. On a phone the
runner's `double_click` / `drag` messages are the double tap and drag gesture, so
the words stay "Double-clicked" / "Dragged" as for the description tools.

Registry (`agents/services/tool_registry.py`): a private reader
`_required_point(arguments, tool_name, x_key="x", y_key="y") -> tuple[int, int]`
(strict ints via `_strict_int`, both present, non-negative, else
`_InvalidArgumentError` naming the tool and keys); handlers
`_handle_double_click_at`, `_handle_hover_at`, `_handle_drag_at` (reads
`start_x/start_y` and `end_x/end_y`), `_handle_long_press_at` (duration through
`_optional_number` with `DEFAULT_LONG_PRESS_SECONDS`, `LONG_PRESS_MIN_SECONDS`,
`LONG_PRESS_MAX_SECONDS`, as `_handle_long_press`), each passing
`context.vision_config` (may be None), `on_screenshot`, `cancellation_check`,
`deadline`; four `_TOOL_HANDLERS` entries. `_handle_click_at` may switch to
`_required_point` if its error text stays the same; otherwise leave it.

Definitions (`agents/services/tool_definitions.py`), parameter names are the
contract shared with MCP:
- Desktop `get_controller_tool_definitions`: after `click_at`, add
  `double_click_at` (x, y, expected_result), and after `drag` add `hover_at`
  (x, y, expected_result) and `drag_at` (start_x, start_y, end_x, end_y,
  expected_result); descriptions follow `click_at`: "at pixel coordinates of the
  desktop screenshot, for targets <tool> cannot find by description ... Get the
  coordinates from take_screenshot ... Not verified unless expected_result is
  given, and never retried. Prefer <tool> whenever the target can be described.
  Never for web pages opened with browser_navigate". `double_click_at` keeps
  "Use it only when the step says double-click ...; never emulate it with two
  click_at calls".
- Phone `get_mobile_tool_definitions`: `double_click_at` (double-tap wording),
  `long_press_at` (x, y, duration, expected_result; duration text as
  `long_press`), `drag_at` (phone wording, "To scroll or page, use scroll or
  swipe instead"). No `hover_at` on a phone.
- `get_all_tool_definitions` picks them up automatically (desktop wording for
  shared names).

Wording updates:
- `agents/services/prompt_parts.py`: add the tools to both numbered taxonomies
  (renumber), extend the desktop rule "When click cannot find a target ... use
  click_at" to "use click_at, double_click_at, hover_at or drag_at", add one
  example line each to the desktop and phone example blocks, and the phone rule
  "take a new screenshot before click_at, swipe or scroll with coordinates" to
  name the `*_at` tools.
- `tools_controller.take_screenshot` suffix: "(Screenshot: W x H pixels; the
  *_at tools (click_at, double_click_at, ...), scroll and swipe use these
  coordinates.)" listing the target's names generically is fine; keep it one line.
- `vision_qa.answer_screenshot_question` docstring.

### Pages and forms
None.

### Background work and WebSockets
No new Celery task, consumer, event or controller action. Existing controller
messages `double_click`, `hover`, `drag`, `long_press` are reused.

MCP machine tools (`mcp_gateway`):

| MCP tool | `_tool(...)` row | Request DTO | Annotation |
|---|---|---|---|
| `double_click_at` | `CallKind.CONTROL, _ALL, ai_with_expected_result=True` | `ClickAtRequest` (docstring: click_at, double_click_at, hover_at) | `_ACT` |
| `hover_at` | `CallKind.CONTROL, _DESKTOP, ai_with_expected_result=True` | `ClickAtRequest` | `_ACT` |
| `drag_at` | `CallKind.CONTROL, _ALL, ai_with_expected_result=True` | new `DragAtRequest(start_x, start_y, end_x, end_y, duration, expected_result)` | `_DESTRUCTIVE` (as `drag`) |
| `long_press_at` | `CallKind.CONTROL, _PHONE, ai_with_expected_result=True` | new `LongPressAtRequest(x, y, duration, expected_result)` | `_ACT` |

- `uses_visual_engine` stays False (no screen parse), so they work while the
  visual engine is offline: the point of the `[visual_engine_offline]` advice.
- New argument aliases in `schemas.py`: `StartXArg`, `StartYArg`, `EndXArg`,
  `EndYArg` over `_MachinePixel` with descriptions "... where the drag starts /
  ends, in take_screenshot's width x height".
- Handlers call `_agent(name, project_id, request, expected_result=expected_result)`
  so the throttle class becomes AI when an expected_result is given.
- `MACHINE_HANDLERS` descriptions: one sentence each, ending with `_CLAIMED`,
  e.g. "Double-click (double-tap on a phone) at x, y in take_screenshot's width x
  height. Optional expected_result is checked afterwards."
- `machine_tools` assert → 56; `machine_handlers` docstring → 56; `mcp_app`
  docstrings → 56 machine tools, 71 tools.
- `run_agent_tool._ECHO_WITHOUT_CHECK_TOOLS` gains the four names (their
  success text is coordinates plus fixed wording; with an expected_result the
  text carries a vision reason and goes through `neutral_machine_text` as today).
- Wording that recommends `click_at` (`call_guard._AI_THROTTLED`,
  `machine_errors` VISUAL_ENGINE_OFFLINE / AI_UNAVAILABLE,
  `machine_screenshot._NOTE` / `_NOTE_NO_SIZE`, `schemas.TakeScreenshotResponse`
  comment, `limits.py` comment, `workflow.py` machine-tools paragraph,
  `take_screenshot` handler description) names the coordinate tools as a group:
  "click_at and the other *_at tools". Keep "never names AI, MCP or an agent".

### Settings / infrastructure
None. No env var.

### Data flow
1. Agent: model calls `drag_at(...)` → `dispatch_tool_call` (target check via
   `tool_names_for`) → `_handle_drag_at` validates ints → `tools_controller.drag_at`
   → `act_once_and_check` → `controller_drag` over the channel layer → runner maps
   pixels (`to_input`) and drags → `action_result` → optional `confirm_action`
   (after screenshot to `on_screenshot`, vision verdict) → `ToolResult`.
2. MCP: client calls `drag_at` → `machine_handlers.drag_at` builds `DragAtRequest`
   (pydantic bounds) → `_agent` → `tool_runner.run_project_tool` (throttle by
   `effective_kind`) → `machine_gate.open_machine_session` (claim, target) →
   `run_agent_tool` (slot, lock, no reconnect wait, deadline,
   `allowlisted_dispatcher(MCP_AGENT_TOOL_NAMES)`) → same path as 1 with no
   callbacks → fenced `MachineActionResponse`.

## Alternatives considered
- **Optional `x`/`y` on the description tools** (one tool, two modes): rejected;
  it mixes the verified-candidate flow with a single unverified action, makes
  every description tool's schema ambiguous, and breaks the precedent `click` /
  `click_at` set (plans, digests and allowlists key on names).
- **Copy `click_at`'s body four more times**: rejected; there would be six copies
  of one flow. The helper is a leaf module so `tools_mobile` (which imports
  `tools_controller`) and `tools_controller` can both use it without a cycle.
- **A runner-side "pointer_at" message**: unnecessary; every needed message
  already takes pixels and maps them.

## Risks and mitigations
- **Wording drift on `click_at`, `swipe`, `rotate_device`** after moving them
  onto the helper: the plan fixes `done_suffix` / `miss_hint` per tool; the
  verifier compares the old and new texts.
- **Old runner and `double_click_at`**: refused by the same precheck as
  `double_click` (Need help `runner_outdated` for the agent, `[machine_outdated]`
  over MCP) before any input is sent.
- **Coordinates outside the screen / stale screenshot**: the runner refuses
  out-of-bounds points; a stale but in-bounds point acts on the wrong place, same
  as `click_at` today. Descriptions tell the model to take coordinates from the
  latest screenshot and to prefer the description tools.
- **`drag_at` and `long_press_at` are destructive-ish** gestures with no
  pre-verification: same trade-off as `click_at`; MCP annotates `drag_at` as
  destructive.
- **Model over-uses coordinate tools** instead of verified ones: descriptions and
  rules say "Prefer <tool> whenever the target can be described".
- **Prompt length**: four more definitions per target (a few hundred tokens);
  accepted.
- **MCP throttles**: coordinate calls without a check count as CONTROL (120/min),
  with a check as AI (20/min), identical to `click_at`.
- No XSS, auth, secrets or migration risk.

## Implementation steps

The three parts touch disjoint files and share only the contract fixed above
(tool names and parameter names: `x`, `y`, `start_x`, `start_y`, `end_x`,
`end_y`, `duration`, `expected_result`). They can run as parallel sub-agents.

**Part A: agent tool layer** (one sub-agent; files depend on each other line by line)
1. Create `agents/services/act_and_check.py` with `act_once_and_check`.
2. `agents/services/tools_controller.py`: `click_at` onto the helper (wording
   unchanged); add `double_click_at`, `hover_at`, `drag_at`; update the
   `take_screenshot` size suffix.
3. `agents/services/tools_mobile.py`: delete `_act_and_check`; `swipe`,
   `rotate_device` onto the helper (wording unchanged); add `long_press_at`;
   update the module docstring.
4. `agents/services/tool_registry.py`: `_required_point`, four handlers, four
   `_TOOL_HANDLERS` entries.

**Part B: definitions and prompts** (one sub-agent)
5. `agents/services/tool_definitions.py`: desktop `double_click_at`, `hover_at`,
   `drag_at`; phone `double_click_at`, `long_press_at`, `drag_at`.
6. `agents/services/prompt_parts.py`: taxonomies (renumbered), rules, examples.
7. `agents/services/vision_qa.py`: docstring.

**Part C: MCP gateway** (one sub-agent)
8. `mcp_gateway/schemas.py`: `StartXArg`/`StartYArg`/`EndXArg`/`EndYArg`,
   `DragAtRequest`, `LongPressAtRequest`, `ClickAtRequest` docstring,
   `TakeScreenshotResponse` comment.
9. `mcp_gateway/machine_tools.py`: four rows (screen group for the first three,
   phone group for `long_press_at`), assert 56, comment on `kind`.
10. `mcp_gateway/machine_handlers.py`: four handlers, four `MACHINE_HANDLERS`
    rows, module docstring 56, `take_screenshot` description.
11. `mcp_gateway/services/run_agent_tool.py`: `_ECHO_WITHOUT_CHECK_TOOLS`.
12. Wording: `mcp_gateway/services/call_guard.py`,
    `mcp_gateway/services/machine_errors.py`,
    `mcp_gateway/services/machine_screenshot.py`, `mcp_gateway/workflow.py`,
    `mcp_gateway/limits.py` (comment), `mcp_gateway/mcp_app.py` (56 / 71).

**Integration (orchestrator)**
13. Read the combined diff; check that every new definition name has a registry
    handler and that every MCP row's `agent_tool` is a registry name for its
    targets; `pre-commit run --all-files`; `mypy .`.
14. Docs: `agents/CLAUDE.md` (extend the `click_at` paragraph to the `*_at`
    family and the shared helper), `CLAUDE.md` (MCP: "plus the 56 machine tools",
    "40 more tools (55 in all)" naming the coordinate tools, "71 in all"), then
    `docs/changed/2026-11-09-coordinate-pointer-tools.md` per workflow step 6.

## Verification
- `mypy .` and `pre-commit run --all-files` clean.
- `python manage.py check`.
- `python manage.py shell -c` (read-only): assert that `tool_names_for(DESKTOP)`
  contains `double_click_at`, `hover_at`, `drag_at` and not `long_press_at`;
  `tool_names_for(ANDROID)` contains `double_click_at`, `drag_at`,
  `long_press_at` and not `hover_at`; `MCP_AGENT_TOOL_NAMES` contains all four;
  importing `mcp_gateway.mcp_app` passes the 56-tool asserts.
- Desktop runner, Machine setup chat (or a test case): ask for
  `double_click_at` on a desktop icon (opens), `hover_at` over a toolbar button
  (tooltip, with expected_result), `drag_at` of a file to a folder; one call with
  an off-screen point returns the runner's "outside the screen" tool error;
  `click_at`, `swipe` texts unchanged.
- A runner without `double_click_v1` (or a stubbed capability list): a case
  using `double_click_at` goes Need help with fault `runner_outdated`.
- Phone runner (emulator): `double_click_at` on a map, `long_press_at` on an app
  icon with `duration=2`, `drag_at` of a list item; `hover_at` refused by
  `dispatch_tool_call` as "Tool not available".
- MCP (claimed Manual case, desktop project): `tools/list` shows 71 tools;
  `double_click_at`, `hover_at`, `drag_at` work with the visual engine stopped;
  `long_press_at` answers `[wrong_target]`; with an expected_result the call
  counts against the AI throttle; result text contains no "agent"/"AI"/"MCP".
- Run `reviewer` and `verifier` agents in parallel on the diff.

## Open questions
- Q1 (resolved 2026-11-09, user): `drag_at` takes an optional `duration` in seconds.
  Bounds are new constants in `controller_client/protocol.py` next to the long-press
  ones: `DRAG_MIN_SECONDS` 0.1, `DRAG_MAX_SECONDS` 10.0, `DEFAULT_DRAG_SECONDS` 0.5
  (written by the orchestrator before the parts start; server-side bound only, no
  runner behaviour change and no `CLIENT_VERSION` bump). Registry reads it with
  `_optional_number(..., DEFAULT_DRAG_SECONDS, DRAG_MIN_SECONDS, DRAG_MAX_SECONDS)`;
  definitions describe it like `long_press`'s duration; MCP `DragAtRequest` gets a
  `DragDurationArg` bounded the same way. Success text keeps
  "Dragged from (sx, sy) to (ex, ey)" (duration not echoed).
- Q2 (resolved 2026-11-09, user): teardown stays `click` only.

## Changelog
- 2026-11-09: created
- 2026-11-09: Q1/Q2 answered (drag_at duration; teardown unchanged); status In progress
- 2026-11-09: review fixes (MCP drag_at duration optional/None, size note target-neutral); act_once_and_check parameter bundling deferred (six callers); status Done
- 2026-11-09: second verify pass: reverted click_at to _read_point (one-missing-coordinate message would change); reviewer nit on a long comment in machine_tools.py left as is
- 2026-11-09: after deploy: `CLIENT_VERSION` bumped 0.23.2 → 0.23.3 because `controller_client/protocol.py` changed (drag duration constants), so installed runners match the bundle
