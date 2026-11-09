# feat: Right-click tools

Status: Done
Created: 2026-11-09
Request: "Add right click as well", a follow-up to
`docs/plan/feat-coordinate-pointer-tools.md`. Desktop agents and MCP clients get
two new pointer tools: `right_click(description, expected_result?)`, found and
checked by vision like `click`, and `right_click_at(x, y, expected_result?)`,
which takes screenshot-pixel coordinates like `click_at`. Typical use: opening
context menus. Desktop only. On a phone, `long_press` / `long_press_at` is the
equivalent. No Test Runner change.

## Context

- `projects/services.py::controller_click(project_id, x, y, button="left", timeout=30.0)`
  already sends `button` with the `click` message. Every desktop backend handles
  `"right"`: `controller_client/executor.py` passes `payload.button` to
  `get_target_backend().click`, pyautogui in `desktop_pyautogui.py`, and
  `_button_code` in `desktop_gnome.py`. The phone backend
  (`controller_client/mobile_backend.py:157`) raises `_NO_RIGHT_CLICK` ("A phone
  has no right or middle click: use long_press for a context menu"). **So there is
  no Test Runner change, no new capability and no `CLIENT_VERSION` bump.** Every
  runner version that is connected today can right-click.
- `agents/services/tools_controller.py`:
  - `verified_click(project_id, *, tool_name, description, vision_config, expected_result, press: PressFunc, verb, success_verb, default_expectation, on_screenshot, cancellation_check, deadline, precheck=None)`
    is the verified flow that `click` and `double_click` already share. It
    resolves the element, takes a before screenshot, calls `press(x, y)`, then
    always runs the outcome check, using `default_expectation(description)` when
    no expected_result is given. `click` is a thin wrapper that passes
    `_press = controller_click(project_id, x, y)`. **`right_click` needs a new
    wrapper only, with `press` bound to `controller_click(..., button="right")`.**
  - `click_at` / `double_click_at` are thin wrappers over
    `agents/services/act_and_check.act_once_and_check` with
    `COORDINATE_MISS_HINT`. **`right_click_at` follows the same pattern.**
- `agents/services/verified_element_actions.py`: `default_click_expectation` and
  `default_double_click_expectation` (l.246–257). A right-click needs its own
  default: the reaction differs (a context menu).
- `agents/services/tool_registry.py`: `_handle_click` returns "Vision model not
  configured." when `vision_config` is None. `_handle_double_click_at` uses
  `_required_point(arguments, "double_click_at")`. The `_TOOL_HANDLERS` dict is
  at about l.840.
- `agents/services/tool_definitions.py`: the desktop
  `get_controller_tool_definitions` defines `click` (l.170), `double_click`,
  `click_at`, and `double_click_at`, which uses `_pixel_parameter` /
  `_coordinate_expected_result`. `tool_definitions_for` / `tool_names_for` are
  built from these lists, so desktop-only definitions are enough: the phone agent
  never sees the tools, and `dispatch_tool_call` refuses them on a phone.
  `get_all_tool_definitions` picks them up, and `person_step_prompts._TOOL_NAMES`
  strips the new names (longest first) automatically.
- `agents/services/prompt_parts.py`:
  - The desktop taxonomy, numbered 1–28 (l.138–168).
  - `build_desktop_tool_examples` (l.370–405): the vision-tool rules and the
    "When click cannot find a target … use click_at, or double_click_at, hover_at
    or drag_at" rule.
  - `build_tool_selection_rules` (l.537): web pages must use `browser_*` tools.
- Allowlists stay unchanged: `case_teardown.TEARDOWN_TOOL_NAMES` (`click` only,
  a user decision) and `person_steps.CONFIRM_TOOL_NAMES`.
- MCP (`mcp_gateway`):
  - `machine_tools.py` table: `click` is `CallKind.AI, _ALL, uses_visual_engine=True`,
    and `click_at` is `CallKind.CONTROL, _ALL, ai_with_expected_result=True`. The
    table assert expects 56 tools.
  - `machine_handlers.py`: the `click` / `click_at` handlers build
    `DescribedActionRequest` / `ClickAtRequest`. `click_at` calls
    `_agent(name, project_id, request, expected_result=expected_result)`. The
    `MACHINE_HANDLERS` rows have `_ACT` annotations. The docstring says "The 56
    machine tools".
  - `schemas.py`: the docstrings of `DescribedActionRequest` ("click,
    double_click, hover") and `ClickAtRequest` ("click_at, double_click_at,
    hover_at").
  - `services/run_agent_tool.py::_ECHO_WITHOUT_CHECK_TOOLS`.
  - `mcp_app.py`: the docstrings say 56 machine tools and 71 tools.
  - `workflow.py` (l.69–70) lists the `*_at` tools by name.
- `CLAUDE.md` gives the counts "56 machine tools" (l.170), "40 more tools (55 in
  all)" plus the list of coordinate tools (l.176), and "16 more, 71 in all"
  (l.177). `agents/CLAUDE.md` has the **Double-click** paragraph and the
  **`click_at` and `scroll`** paragraph.

## Goals
- An agent `right_click(description, expected_result?)` with exactly `click`'s
  verified flow (candidate verification, up to 10 candidates, outcome check that
  always runs, unexpected/missed handling). Only the button differs, and the
  default expectation is a context-menu reaction.
- An agent `right_click_at(x, y, expected_result?)` with exactly `click_at`'s
  behaviour (strict non-negative ints, one optional check, no retry, works with
  the visual engine offline).
- Both tools only for desktop runners. They are also MCP machine tools inside a
  claimed Manual case, with the same throttle, budget and no-trace behaviour as
  `click` / `click_at`.

## Non-goals
- No Test Runner, protocol, capability or `CLIENT_VERSION` change.
- No phone variant. On a phone, `long_press` / `long_press_at` opens context
  menus, and the phone backend already refuses non-left buttons.
- No `browser_right_click` and no `browse_right_click`. Playwright and WebDriver
  pages are out of scope. Desktop tools on a `browser_navigate` page stay
  discouraged by the existing rule.
- No middle click, and no generic `button` parameter on `click` / `click_at`.
  Separate tool names keep the model's choice explicit and the MCP table one row
  per tool. A `button` argument would also leak onto phones, where it is refused.
- No change to the teardown or person-step confirm allowlists.
- No change to `click`, `click_at` or `verified_click` behaviour or wording,
  apart from `verified_click`'s docstring.

## Design

### Data model
None. No migration.

### Services

`agents/services/verified_element_actions.py`: new
`default_right_click_expectation(description: str) -> str`, which returns
"the right-click landed on '{description}' and the UI reacted to it (context
menu, selection, or similar)".

`agents/services/tools_controller.py` (no new abstraction, because both shared
flows already exist):

| Function | Signature (keyword-only after `project_id`) | Body |
|---|---|---|
| `right_click` | `(project_id, *, description: str, vision_config: LLMConfig, expected_result: str = "", on_screenshot: ScreenshotCallback \| None = None, cancellation_check: CancellationCheck \| None = None, deadline: float \| None = None) -> ToolResult` | `verified_click(tool_name="right_click", press=controller_click(project_id, x, y, button="right"), verb="right-clicked", success_verb="Right-clicked", default_expectation=default_right_click_expectation)`, no precheck |
| `right_click_at` | `(project_id, *, x: int, y: int, vision_config: LLMConfig \| None, expected_result: str = "", on_screenshot=None, cancellation_check=None, deadline=None) -> ToolResult` | `act_once_and_check(tool_name="right_click_at", act=ensure_action_succeeded(controller_click(project_id, x, y, button="right")), success_message="Right-clicked at (x, y)", action_summary="right-clicked at (x, y)", done_suffix=".", miss_hint=COORDINATE_MISS_HINT)`, no precheck |

Change the `verified_click` docstring from "shared by click and double_click" to
"click, double_click and right_click". Pass `button="right"` as a keyword
argument, never positionally, so it cannot be mistaken for `timeout`.

`agents/services/tool_registry.py`:
- `_handle_right_click`: a copy of `_handle_click`'s shape. It returns "Vision
  model not configured." when `vision_config` is None and passes the
  description, `_read_expected_result`, `on_screenshot`, `cancellation_check`
  and `deadline`.
- `_handle_right_click_at`: `_required_point(arguments, "right_click_at")`, then
  `tools_controller.right_click_at` with `context.vision_config` (may be None).
- Two `_TOOL_HANDLERS` entries next to `click` / `click_at`. `_handle_click_at`
  stays as it is.

`agents/services/tool_definitions.py` (desktop `get_controller_tool_definitions`
only; the parameter names are the contract shared with MCP):
- `right_click`, after `double_click`, with parameters `description` (required)
  and `expected_result` (optional, worded like `click`'s with "after the
  right-click", e.g. "the context menu with Rename and Delete opens").
  Description: "Right-click an element on the desktop found by vision-based
  natural-language description, usually to open its context menu. Verified
  before and checked after like click (up to 10 candidates …). Use it only when
  the step asks for a right-click or a context menu; otherwise use click. Not for
  web pages opened with browser_navigate."
- `right_click_at`, after `double_click_at`, with parameters
  `_pixel_parameter("x", "Horizontal")`, `_pixel_parameter("y", "Vertical")` and
  `_coordinate_expected_result("right-click", "the context menu of the selected
  file opens", "right-clicks")`. The description follows `double_click_at`: "for
  targets right_click cannot find by description … Not verified unless
  expected_result is given, and never retried. Prefer right_click whenever the
  target can be described. Never for web pages opened with browser_navigate."
- Nothing goes in `get_mobile_tool_definitions`.

`agents/services/prompt_parts.py`:
- Desktop taxonomy: add "right_click — Right-click an element found by
  vision-based description (context menus)" after `double_click`, and
  "right_click_at — Right-click at pixel coordinates from take_screenshot" after
  `double_click_at`. Renumber everything below them (30 entries).
- `build_desktop_tool_examples`:
  - Add `right_click` to the lists "Vision-based tools (click, double_click,
    hover, drag)", "click, double_click, hover and drag verify …" and "When
    click, double_click, hover or drag returns an error".
  - Add one rule: "Use right_click only when the step asks for a right-click or
    a context menu; pick the menu entry afterwards with click".
  - Extend the click_at rule's list to "double_click_at, right_click_at, hover_at
    or drag_at".
  - Add two example lines: `right_click(description="the report.pdf file",
    expected_result="its context menu opens")` and `right_click_at(x=420,
    y=310)`.
- Leave the phone taxonomy, phone examples and `build_tool_selection_rules`
  unchanged.

### Pages and forms
None.

### Background work and WebSockets
No new Celery task, consumer, event or controller message. The existing `click`
message is reused with `button="right"`.

MCP machine tools:

| MCP tool | `_tool(...)` row (placed after the matching double-click row) | Request DTO | Annotation | Handler description |
|---|---|---|---|---|
| `right_click` | `CallKind.AI, _DESKTOP, uses_visual_engine=True` | `DescribedActionRequest` | `_ACT` | "Right-click an element you describe in plain words, e.g. to open its context menu (desktop; on a phone use long_press). Optional expected_result is checked afterwards." + `_CLAIMED` |
| `right_click_at` | `CallKind.CONTROL, _DESKTOP, ai_with_expected_result=True` | `ClickAtRequest` | `_ACT` | "Right-click at x, y in take_screenshot's width x height (desktop; on a phone use long_press_at). Optional expected_result is checked afterwards." + `_CLAIMED` |

- The handlers mirror `click` (`_agent("right_click", project_id, request)`) and
  `click_at` (`_agent("right_click_at", project_id, request,
  expected_result=expected_result)`). They reuse the `DescriptionArg`, `XArg`,
  `YArg` and `ExpectedResultArg` aliases, so no new schema class is needed. Only
  the docstrings change: `DescribedActionRequest` becomes "click, double_click,
  right_click, hover" and `ClickAtRequest` becomes "click_at, double_click_at,
  right_click_at, hover_at".
- `run_agent_tool._ECHO_WITHOUT_CHECK_TOOLS` gains `right_click_at`.
  `right_click` stays out of it, like `click`.
- New counts: the `machine_tools` assert becomes 58, the `machine_handlers`
  docstring says 58, and the `mcp_app` docstrings say 58 machine tools and 73
  tools.
- `workflow.py`: "machine tools (take_screenshot, click, right_click, click_at
  and the other *_at tools (double_click_at, right_click_at, hover_at, drag_at,
  long_press_at), …". This is the only place that lists the `*_at` tools by name.
  Every other mention says "click_at and the other *_at tools" and already covers
  `right_click_at`.
- The `_DESKTOP` target set makes the existing gate refuse both tools on a phone
  runner, exactly as it does for `hover` / `hover_at`.

### Settings / infrastructure
None.

### Data flow
1. Agent path: the model calls `right_click(description=…)`.
   `dispatch_tool_call` checks the name against `tool_names_for(DESKTOP)`, then
   `_handle_right_click` → `tools_controller.right_click` → `verified_click`,
   which does the OmniParser parse, vision candidate check, before screenshot,
   `controller_click(button="right")` → `click` message → runner
   `backend.click(x, y, "right")` → outcome check → result.
2. `right_click_at` follows the same path through `act_once_and_check`: one
   action, and one check only when an expected_result is given.
3. MCP path: the handler builds the request DTO → machine gate (claim, desktop
   target, throttle class) → `run_agent_tool` → the same `dispatch_tool_call`.
   The text goes back through the neutral mapping.

## Alternatives considered
- **A `button` argument on `click` / `click_at`.** This needs fewer tool names,
  but it changes the existing tools' contract, which would then also appear on
  phones (where the runner refuses it). The model also picks tools more reliably
  by name. Rejected.
- **A new verified flow for right-click.** Not needed, because `verified_click`
  already takes `press`, the verbs and the default expectation.
- **Adding right-click to the teardown allowlist** (to close leftovers through
  context menus). The previous change's user decision keeps teardown at `click`
  only. Not done.

## Risks and mitigations
- *The model right-clicks where a left click was meant.* The definitions and
  prompt rules limit it to steps that ask for a right-click or a context menu.
  The outcome check of `right_click` always runs (default expectation).
- *A context menu stays open after a check that did not confirm the result.* This
  is the same as any unconfirmed action: the result says "performed but NOT
  confirmed", and the existing rule tells the model to re-observe. Escape is
  available through `key_press`.
- *Phone misuse through MCP.* The `_DESKTOP` target refuses it before any runner
  call. The runner also refuses with `_NO_RIGHT_CLICK`.
- *Windows UIPI.* `input_guard` already covers `click` with any button.
- No security surface: no new input beyond ints and strings already validated
  for `click` / `click_at`. Tool output is fenced and neutral as before.

## Implementation steps
Parts 1–3 touch disjoint files and can run in parallel. They share only the tool
names `right_click` / `right_click_at` and the parameter names `description`,
`expected_result`, `x`, `y` defined above.

1. **Agent tools and handlers** (files: `agents/services/verified_element_actions.py`,
   `agents/services/tools_controller.py`, `agents/services/tool_registry.py`):
   add `default_right_click_expectation`, `right_click`, `right_click_at`
   (signatures above), `_handle_right_click`, `_handle_right_click_at` and the two
   `_TOOL_HANDLERS` entries, and update the `verified_click` docstring.
2. **Definitions and prompts** (files: `agents/services/tool_definitions.py`,
   `agents/services/prompt_parts.py`): add the two desktop definitions, the
   taxonomy entries with renumbering, and the rules and examples.
3. **MCP machine tools** (files: `mcp_gateway/machine_tools.py`,
   `mcp_gateway/machine_handlers.py`, `mcp_gateway/schemas.py`,
   `mcp_gateway/services/run_agent_tool.py`, `mcp_gateway/mcp_app.py`,
   `mcp_gateway/workflow.py`): add two table rows and set the assert to 58, add
   two handlers and two `MACHINE_HANDLERS` rows, update the docstrings, the echo
   set, the counts and the workflow wording.
4. **Integration** (orchestrator; files: `CLAUDE.md`, `agents/CLAUDE.md`):
   - `CLAUDE.md`: 56 → 58 machine tools, "40 more tools (55 in all)" → "42 more
     tools (57 in all)" with `right_click` / `right_click_at` (desktop) named next
     to the coordinate tools, and "16 more, 71 in all" → "73 in all".
   - `agents/CLAUDE.md`: in the **Double-click** paragraph add that `right_click`
     runs the same `verified_click` flow with `button="right"`; in the coordinate
     paragraph add `right_click_at` (desktop).
   - Run `pre-commit run --all-files`, then write
     `docs/changed/2026-11-09-right-click-tools.md`.

## Verification
- `mypy .` and `pre-commit run --all-files` are clean.
- Import check:
  `python manage.py shell -c "from mcp_gateway import machine_tools, mcp_app"`.
  The import must succeed, which means the assert for 58 tools passes.
- Tool lists, checked through `python manage.py shell -c`:
  - `tool_names_for(RunnerTarget.DESKTOP)` contains both names.
  - The Android and iOS name sets contain neither.
  - `MACHINE_TOOLS["right_click"].kind is CallKind.AI`.
  - `effective_kind` for `right_click_at` is CONTROL without an expected_result
    and AI with one.
- Manual run on a desktop Test Runner (a Machine setup chat or an agent run):
  - Ask the agent to right-click a desktop file. The context menu opens and the
    result reads "Right-clicked element at (x, y): …" with the check confirmed.
  - Call `right_click_at` on a blank desktop area with no expected_result. The
    desktop context menu opens and the result is "Right-clicked at (x, y).".
  - Call `right_click_at` with a negative x. The tool returns an argument error
    and sends nothing to the runner.
- MCP, inside a claimed Manual case:
  - `right_click_at` on a desktop runner works.
  - On a phone runner both tools are refused by the target gate.
  - With the visual engine offline, `right_click_at` still works and
    `right_click` returns `[visual_engine_offline]`.
- A phone agent's taxonomy, examples and tool list do not mention right-click.
- Afterwards, run the `reviewer` and `verifier` agents in parallel.

## Open questions
None. The decisions this request could have raised are settled by the
established facts and the previous plan: desktop only, separate tool names
rather than a `button` argument, no runner change, and teardown and confirm
allowlists unchanged.

## Changelog
- 2026-11-09: created
