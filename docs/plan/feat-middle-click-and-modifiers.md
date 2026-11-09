# feat: Middle click and held modifier keys on pointer actions

Status: Done
Created: 2026-11-09
Request: This follows `docs/plan/feat-right-click-tools.md` and
`docs/plan/feat-coordinate-pointer-tools.md`. It has two parts, both desktop
only, and both reach the AI agent and the MCP machine tools.

1. **Middle click** (pressing the mouse wheel as a button). Two new tools,
   `middle_click(description, expected_result?)` and
   `middle_click_at(x, y, expected_result?)`, mirror `right_click` /
   `right_click_at`.
2. **Held modifier keys.** An optional `modifiers` list holds Ctrl/Cmd, Shift,
   Alt/Option and Win/Super down while a pointer action happens. It applies to:
   - `click`, `click_at`, `double_click`, `double_click_at`, `right_click`,
     `right_click_at`, `middle_click`, `middle_click_at`;
   - `drag`, `drag_at`, `hover`, `hover_at`, `scroll`.

   Examples are Ctrl/Cmd+click to add to a selection, Shift+click to select a
   range, Alt+drag to copy, Ctrl+wheel to zoom and Shift+wheel to scroll
   sideways.

Part 2 needs a Test Runner protocol change with a new optional capability. Part 1
needs none. Playwright `browser_*` and MCP `browse_*` tools are out of scope.

## Context

### Middle click needs no Test Runner change (confirmed)

- `projects/services.py::controller_click(project_id, x, y, button="left", timeout=30.0)`
  sends `button` as a free string. `controller_client/protocol.py::parse_click_payload`
  takes any string (default `"left"`), and `executor.execute_click` passes it to
  `get_target_backend().click(x, y, button)`.
- Every desktop backend already handles `"middle"`:
  - **pyautogui backend** (Windows and macOS, `desktop_pyautogui.py`) calls
    `pyautogui.click(button=button)`. In PyAutoGUI 0.9.54,
    `_pyautogui_osx._click` posts `kCGEventOtherMouseDown/Up` with
    `kCGMouseButtonCenter`, and `_pyautogui_win._click` sends
    `MOUSEEVENTF_MIDDLEDOWN/UP`. Checked in
    `controller_client/.venv/.../pyautogui/`.
  - **GNOME Wayland backend** (`desktop_gnome.py`) has `_BUTTONS["middle"] = BTN_MIDDLE`.
    It has been there since the backend was added (`git log -S` → `aba712b`).
    Before that, Linux ran on pyautogui, which also knows `middle`.
  - **Phone backend** (`mobile_backend.py:157`) refuses any non-left button with
    `_NO_RIGHT_CLICK`: "A phone has no right or middle click …".
- So middle click is a server-only change, exactly like right click. Every
  connected desktop runner can do it.
- `MouseButton` (protocol.py:362, LEFT/RIGHT) is not used anywhere. It is left
  as is.

### Modifiers need a protocol change (confirmed)

- None of the pointer payloads carry modifiers today: `ClickPayload`,
  `DoubleClickPayload`, `HoverPayload`, `DragPayload` and `ScrollPayload`
  (protocol.py:454–501), parsed at protocol.py:846–897.
- Parsers ignore unknown keys. An older runner would therefore **silently drop**
  a `modifiers` field and perform a plain click. The server must refuse before
  it sends.
- The only way to hold a key today is `key_press`, and it releases everything
  before it returns:
  - pyautogui `press_combination` → `_release_modifiers`;
  - GNOME `press_keys` → `_then_release`.
- **Stuck-modifier handling** in the pyautogui backend:
  - `held_modifiers()` and `_release_modifiers(context)` exist per platform.
  - macOS reads `CGEventSourceFlagsState` and releases with `keyUp` of both
    sides.
  - Windows reads `GetAsyncKeyState` on the left/right VKs and releases only
    what is down, because "a blind Win or Alt key-up could open Start or the
    menu bar".
  - `type_text` calls `_release_modifiers("before typing")`.
  - `diagnose --input` (`input_diagnostics._check_modifiers`) uses the same
    code.
  - The GNOME backend keeps no OS modifier state. Every keysym it presses is
    released in `_then_release`, quietly when the press failed.
- On macOS the backend already posts its own Quartz events for `double_click`
  (`_double_click`, with click state 1/2) and `scroll` (`_scroll`). pyautogui's
  own mouse events use `CGEventCreateMouseEvent(None, …)` with no flags. Reports
  of `keyDown('shift')` + `pyautogui.click()` on macOS say it is unreliable. The
  reliable way is to set the flags on each posted mouse event with
  `CGEventSetFlags` (Apple docs, Qt's macnativeevents test code). The local
  stubs `controller_client/stubs/Quartz/__init__.pyi` do not declare
  `CGEventSetFlags` or the right/other/drag/move event types yet.
- Windows UIPI: `executor._guard_input()` runs `input_guard.ensure_input_not_blocked()`
  before every pointer action on a desktop target. That covers modified actions
  too, as long as the guard runs before the first key-down.
- The backend interface `controller_client/desktop_backend.DesktopBackend`
  (click / double_click / move_to / drag / scroll) is implemented by
  `PyAutoGuiDesktopBackend`, `GnomeDesktopBackend` and the phone
  `mobile_backend` (through `MobileTargetBackend`). Direct callers outside the
  executor are `input_diagnostics` (`move_to`, `execute_hover(HoverPayload(x, y))`,
  `execute_scroll(ScrollPayload(...))`, positional) and `mobile_checks`
  (`click(x, y, "left")`.

### Server plumbing

- `projects/controller_protocol.py`:
  - The TypedDicts `ClickActionEvent`, `DoubleClickActionEvent`,
    `HoverActionEvent`, `DragActionEvent` and `ScrollActionEvent` (l.27–56).
  - `ActionTypeRegistry._PAYLOAD_KEYS` (l.401–412).
  - `_build_message` copies only the keys the event has. A key that is left out
    is therefore never sent (the precedent is scroll's x/y).
- `projects/services.py`: `controller_click` (l.1663), `controller_double_click`,
  `controller_scroll`, `controller_hover`, `controller_drag`.
- `projects/runner_capability_services.py`: `runner_supports`,
  `ensure_runner_supports_double_click`. The latter raises `RunnerOutdatedError`,
  an InfrastructureError, so `safe_tool_call` re-raises it and the case gets
  Need help with fault `runner_outdated`. MCP maps it to `[machine_outdated]`
  (`mcp_gateway/services/machine_errors.py:297`).
- The scroll precedent is different. Without `scroll_v1`, `tools_controller.scroll`
  returns a tool error with a key_press fallback.

### Agent

- `agents/services/tools_controller.py`:
  - `verified_click(press=…)` is shared by click, double_click and
    right_click. `hover` and `drag` have their own `_attempt` closures over
    `_run_verified_action` / `_confirm_or_finish`.
  - `_confirm_or_finish` returns `None` on a `missed` verdict, which means
    "try the next candidate" (up to 10 candidates). The vision reason is
    dropped at that point.
  - The `*_at` tools go through `act_and_check.act_once_and_check(act=lambda: …)`.
  - `scroll` checks `SCROLL_V1`.
- `agents/services/tool_registry.py`: handlers, `_required_point`,
  `_read_point`, `_strict_int`, `_InvalidArgumentError`, and
  `dispatch_tool_call`, which enforces `tool_names_for(runner_target)`.
  `ToolContext.runner_target` is known in every handler.
- `agents/services/tool_definitions.py`:
  - Desktop definitions are in `get_controller_tool_definitions` and phone
    definitions in `get_mobile_tool_definitions`. `click`, `double_click`,
    `click_at`, `double_click_at`, `drag`, `drag_at` and `scroll` exist on
    both. `hover`, `hover_at`, `right_click*` are desktop only.
  - `ToolParameter` supports `type="array"` with `items_schema` and
    `max_items`.
- `agents/services/prompt_parts.py`:
  - The desktop taxonomy has 30 entries (l.133–172).
  - `build_desktop_tool_examples` (l.372–421) holds the desktop examples.
  - `build_agent_persona` / `build_environment_context` already tell the model
    the runner's OS from `system_info` (`agents/os_shell.get_os_name`, e.g.
    "a macOS desktop environment").
- Allowlists stay unchanged: `case_teardown.TEARDOWN_TOOL_NAMES` (`click`) and
  `person_steps.CONFIRM_TOOL_NAMES` (no input tools).
  `person_step_prompts._TOOL_NAMES` strips the new tool names automatically.

### MCP

- `mcp_gateway/machine_tools.py`: the `_TOOLS` table, whose assert expects 58.
- `mcp_gateway/machine_handlers.py`: handlers plus the `MACHINE_HANDLERS` rows.
- `mcp_gateway/schemas.py`: `DescribedActionRequest`, `ClickAtRequest`,
  `DragRequest`, `DragAtRequest`, `ScrollRequest`. `MachineRequest.agent_arguments`
  dumps the fields as agent arguments, with `exclude_none`.
- `mcp_gateway/services/run_agent_tool._ECHO_WITHOUT_CHECK_TOOLS`.
- `mcp_gateway/mcp_app.py` docstrings give 58 / 73.
- `mcp_gateway/workflow.py` names the `*_at` tools.

### Docs

- `CLAUDE.md`:
  - The MCP section: "plus the 58 machine tools", "42 more tools (57 in all)"
    with the right-click mention, "16 more, 73 in all".
  - The controller_client capability list.
- `agents/CLAUDE.md`: the **Double-click** and **`click_at` and `scroll`**
  paragraphs.

### In-flight plan

`docs/plan/perf-setup-chat-tokens.md` (Draft) is about the token cost of tool
lists. This plan adds one parameter to about 13 desktop definitions, so the
parameter text is written once in a shared helper and kept to one or two
sentences.

## Goals

- Agent and MCP get `middle_click` (with the verified flow, like `right_click`)
  and `middle_click_at` (one action plus an optional single check), desktop only,
  with no runner change.
- An optional `modifiers` list on the 13 desktop pointer tools above. The runner
  presses the keys down, performs the action, and always releases them, also on
  errors and timeouts inside the backend. Stuck-key handling and the Windows
  input guard keep working.
- An older runner is never sent `modifiers`. It is refused before any input or
  screen parse.
- Calls without modifiers send exactly the same messages as today and return
  exactly the same text.

## Non-goals

- No modifiers on phones: no phone tool gets the parameter, and handlers refuse
  it on a phone target. No modifiers on `type_text`, `key_press` (which already
  takes combinations), `long_press`, `swipe` or the browser tools.
- No Playwright `browser_*` or WebDriver `browse_*` middle click or modifiers.
  These are DOM element actions behind other protocol messages
  (`browser_click`, `web_browser_act`). Supporting them is a separate change, so
  "trivially covered" does not apply.
- No "hold a key across several tool calls" tool (key_down / key_up). Every
  modified action releases its keys before it returns.
- No `button` argument on `click` / `click_at`. Separate tool names stay, as in
  the right-click plan.
- No middle button for `drag` / `drag_at`. The drag stays left-button.
- No change to the teardown or confirm allowlists. No change to the `MouseButton`
  enum. No new `diagnose` check (see Verification for the manual check).

## Design

### Data model

None. No migration.

### Wire protocol (`controller_client/protocol.py`, shared, written first)

- `class PointerModifier(StrEnum)` has five values:

  | Value | macOS | Windows | Linux (GNOME) |
  |---|---|---|---|
  | `primary` | Command | Ctrl | Ctrl |
  | `ctrl` | Control | Ctrl | Ctrl |
  | `shift` | Shift | Shift | Shift |
  | `alt` | Option | Alt | Alt |
  | `meta` | Command | Win | Super |

  The runner resolves the names, because it knows its own platform and each
  backend owns its key names.
- `POINTER_MODIFIER_ORDER: Final[tuple[PointerModifier, ...]]` is the
  canonical press order: primary, ctrl, alt, shift, meta. Keys are pressed in
  this order and released in reverse.
- `def parse_pointer_modifiers(data: dict[str, object]) -> tuple[PointerModifier, ...]`
  reads the field:
  - absent means `()`;
  - otherwise it must be a list of strings;
  - each string must be a member of `PointerModifier`;
  - no duplicates.

  Anything else raises `ProtocolError`. The result is in canonical order.
- `ClickPayload`, `DoubleClickPayload`, `HoverPayload`, `DragPayload` and
  `ScrollPayload` gain a trailing field `modifiers: tuple[PointerModifier, ...] = ()`.
  The trailing default keeps the positional constructions in
  `input_diagnostics` valid. Their `parse_*_payload` functions call
  `parse_pointer_modifiers`.
- `ClientCapability.POINTER_MODIFIERS_V1 = "pointer_modifiers_v1"`, with a
  comment: optional; `modifiers` on click / double_click / hover / drag / scroll;
  older controllers ignore the field, so the server checks the capability.
- It is advertised only on a desktop target. Add a `_DESKTOP_CAPABILITIES`
  frozenset that `advertised_capabilities` drops when `mobile`, symmetric to
  `_MOBILE_CAPABILITIES`. Add it to the "optional capabilities" comment above
  `REQUIRED_CLIENT_CAPABILITIES`. It is not required.
- `CLIENT_VERSION` goes from `"0.23.3"` to `"0.24.0"`. New capabilities have
  bumped the minor version before (0.22.0, 0.23.0).

### Test Runner (`controller_client/`)

**`desktop_backend.DesktopBackend`.** `click`, `double_click`, `move_to`, `drag`
and `scroll` gain a keyword-only `modifiers: Sequence[PointerModifier] = ()`.
The docstring says the backend holds them for the whole action and releases
them before it returns, also when the action raises.

**`executor.py`.** The `execute_*` functions pass `payload.modifiers` through.
`_guard_input()` stays the first call, so it runs before any key-down. The
success message appends " holding shift+primary" (canonical names) only when
modifiers are present. This is a runner log and reply text that the server does
not show.

**`desktop_pyautogui.py`.** One shared private wrapper, conceptually
`_with_modifiers(modifiers, action)`:

1. Resolve the names to pyautogui keys:
   - macOS: primary/meta→`command`, ctrl→`ctrl`, alt→`option`, shift→`shift`;
   - Windows: primary/ctrl→`ctrl`, alt→`alt`, shift→`shift`, meta→`winleft`.

   Drop keys that resolve to the same key (primary+meta on macOS).
2. Press each key with `pyautogui.keyDown` in canonical order, recording each
   one as pressed.
3. Run the action.
4. In `finally`:
   - On Windows, when `alt` or `winleft` was held, tap the unassigned virtual
     key `0xE8` (the "menu mask" AutoHotkey uses) with `user32.keybd_event`, so
     releasing Alt or Win cannot activate the menu bar or open Start.
   - Release the pressed keys with `pyautogui.keyUp`, in reverse order.
   - Call `_release_modifiers("after a modified pointer action")`, which
     catches a lost key-up through the existing per-platform check.

   A failure in the release step is logged and never hides the action's own
   error. Use the same rule as GNOME `_then_release`: quiet when the action
   failed.

With empty modifiers the wrapper is not used, so today's code paths run
unchanged.

**macOS mouse events carry the flags.** With non-empty modifiers on macOS the
backend posts its own Quartz events and sets `CGEventSetFlags(event, mask)` on
every event: down, up, dragged, moved and scroll. The mask is built from
`_MAC_MODIFIER_KEYS` masks. The events are:

- click: left/right/other down and up for left, right and middle;
- double click: the existing click-state 1/2 loop, now with flags;
- drag: down, a stepped series of `*MouseDragged` events over `duration`, then
  up;
- move: `kCGEventMouseMoved`;
- scroll: the existing `CGEventCreateScrollWheelEvent`, now with flags.

The key-downs from step 2 still go out, so apps that read the live modifier
state see them too.

These helpers live in a new darwin-only module,
**`controller_client/macos_pointer_events.py`**. It is imported under
`sys.platform == "darwin"` like `screen_capture_macos`, and it keeps
`desktop_pyautogui.py` from growing past ~500 lines. The existing `_double_click`
and `_scroll` Quartz code moves there with a `flags: int = 0` parameter, and
flags 0 behaves as today.

The local stub `controller_client/stubs/Quartz/__init__.pyi` gains exactly the
names that are used: `CGEventSetFlags`, `kCGEventRightMouseDown/Up`,
`kCGEventOtherMouseDown/Up`, `kCGEventLeftMouseDragged`,
`kCGEventRightMouseDragged`, `kCGEventOtherMouseDragged`, `kCGEventMouseMoved`,
`kCGMouseButtonRight` and `kCGMouseButtonCenter` (`kCGEventFlagMask*` already
exist).

**`desktop_gnome.py`.**
- Resolve the names to keysyms through `keysyms.key_name_to_keysym`:
  primary/ctrl→Control_L, shift→Shift_L, alt→Alt_L, meta→Super_L. Deduplicate.
- Wrap each pointer action in one private helper. It presses the keysyms with
  `self._session.key(keysym, True, stream_path=stream)` and records each one
  as pressed, runs the action (which already releases its own button through
  `_then_release`), and finally releases the pressed keysyms through
  `_then_release(..., self._release_keys(reversed(pressed), stream, quiet))`.
  The keys therefore go out on the same session stream as the pointer events.
- The comment on modifier keys ("nothing extra is released …") gains one
  sentence covering held modifiers.

**`mobile_backend.py`.** The five methods accept `modifiers`. When it is
non-empty they raise `ExecutionError("A phone has no modifier keys")`. This is
defence in depth: the server never sends modifiers to a phone, and the runner
does not advertise the capability there.

**`client.py`** needs no change, because the parse functions carry the field.

### Server plumbing (shared, written first)

- `projects/controller_protocol.py`:
  - The five TypedDicts gain `modifiers: NotRequired[list[str]]`.
  - `_PAYLOAD_KEYS` for `controller.click`, `controller.double_click`,
    `controller.hover`, `controller.scroll` and `controller.drag` gains
    `"modifiers"`.
- `projects/services.py`:
  - `controller_click`, `controller_double_click`, `controller_hover`,
    `controller_drag` and `controller_scroll` gain a keyword-only
    `modifiers: Sequence[PointerModifier] = ()`.
  - The key is added to the event only when the list is non-empty, as
    `[m.value for m in modifiers]`, using the same `point`-dict pattern as
    `controller_scroll`. Without modifiers the bytes on the wire are unchanged.
  - Existing positional callers (`management/commands/test_controller_actions.py`,
    `controller_find_elements`, teardown) are unaffected.
- `projects/runner_capability_services.py`: add
  `ensure_runner_supports_pointer_modifiers(project_id: int) -> None`. It raises
  `RunnerOutdatedError("This Test Runner is too old to hold modifier keys
  during a click, drag, hover or scroll. Update the Test Runner from the
  Environment page.")` when `POINTER_MODIFIERS_V1` is missing. The text is fixed
  server text: it reaches the UI and TestRail, so it carries no runner version.

### Agent

**New leaf `agents/pointer_modifiers.py`.** It imports only
`controller_client.protocol`, so `projects` and `mcp_gateway` could import it
without the `agents.services` cycle.

- `class PointerModifierArgumentError(ValueError)`.
- `read_pointer_modifiers(raw: object) -> tuple[PointerModifier, ...]`:
  - `None` or `[]` means `()`.
  - Otherwise it must be a list (a JSON array) of at most 5 strings,
    case-insensitive and stripped.
  - Aliases are accepted, because models drift:
    `control`→ctrl, `option`/`opt`→alt, `cmd`/`command`→**primary**,
    `win`/`windows`/`super`/`meta`→meta. See Q2.
  - Duplicates after alias mapping are merged.
  - The result is in canonical order. Anything else raises the error with the
    allowed names.
- `modifiers_text(modifiers) -> str` returns `""` or `"shift+primary"`
  (canonical names joined by `+`). It is used in success messages and
  action summaries.

**`agents/services/verified_element_actions.py`.** Add
`default_middle_click_expectation(description)`: "the middle-click landed on
'{description}' and the UI reacted to it (a link opened in a new tab, a tab
closed, text was pasted, or similar)".

**`agents/services/tools_controller.py`.**

- Every pointer tool gains a keyword `modifiers: tuple[PointerModifier, ...] = ()`:
  click, double_click, right_click, middle_click, click_at, double_click_at,
  right_click_at, middle_click_at, hover, hover_at, drag, drag_at, scroll.
  Each passes it to its `controller_*` call. `verified_click` gains a
  `modifiers` parameter that only changes wording and retry behaviour, because
  `press` is already bound by the caller.
- **Precheck.** When `modifiers` is non-empty, call
  `ensure_runner_supports_pointer_modifiers(project_id)` before any screen
  parse or input.
  - Verified tools: chain it into the existing `precheck`. `double_click`
    becomes double_click_v1 then modifiers. `hover` and `drag` pass `precheck`
    to `_run_verified_action`.
  - `*_at` tools: pass `act_once_and_check(precheck=…)`.
  - `scroll`: the existing SCROLL_V1 tool error first, then the modifiers
    check.
- **Wording.** With modifiers, the success message and action summary insert
  "holding {modifiers_text}". Examples: "Clicked element at (x, y) holding
  shift: {description}", "Clicked at (x, y) holding primary.",
  "Scrolled down 5 notches at (x, y) holding ctrl.". The vision outcome
  check's `action_summary` says the same, so the verdict knows a modifier was
  held. Without modifiers every text is byte-identical to today.
- **No retry after a modified press (verified flow).**
  - `_confirm_or_finish` gains `retry_on_miss: bool = True`. When it is False,
    a `missed` verdict returns an error result instead of `None`: "{success
    message}, but the expected result is NOT shown: {reason}. Not retried:
    pressing again with {modifiers} held could undo or extend what this press
    did (e.g. toggle the selection back). Take a screenshot and decide."
  - `verified_click`, `hover` and `drag` pass `retry_on_miss=not modifiers`.
  - Candidate verification before the press still iterates over candidates,
    because nothing is pressed until a candidate is confirmed. So "up to 10
    candidates" still holds for finding the element, but a modified action is
    pressed at most once per call.
  - `unexpected` is unchanged (non-error, "NOT confirmed").
  - `act_once_and_check` never retries, so it needs no change. `modifiers`
    reach it through the `act` lambda and the message strings.
- **New tools.**
  - `middle_click(...)` = `verified_click(tool_name="middle_click",
    press=controller_click(..., button="middle", modifiers=modifiers),
    verb="middle-clicked", success_verb="Middle-clicked",
    default_expectation=default_middle_click_expectation)`.
  - `middle_click_at(x, y, ...)` = `act_once_and_check(tool_name="middle_click_at",
    act=… controller_click(..., button="middle") …, success_message="Middle-clicked
    at (x, y)", miss_hint=COORDINATE_MISS_HINT)`.
  - Neither has a precheck unless modifiers are given. Pass `button` as a
    keyword argument, never positionally.
- Update the `verified_click` docstring to "click, double_click, right_click
  and middle_click".

**`agents/services/tool_registry.py`.**

- Add a private `_read_modifiers(context, arguments) -> tuple[PointerModifier, ...]`:
  - It calls `read_pointer_modifiers(arguments.get("modifiers"))` and maps
    the error to `_InvalidArgumentError`.
  - It refuses non-empty modifiers when `context.runner_target` is not
    DESKTOP: "modifiers exist only on a desktop computer; a phone or tablet has
    no modifier keys." This covers the shared phone tools click, double_click,
    click_at, double_click_at, drag, drag_at and scroll.
- The 11 existing pointer handlers read it inside their argument
  `try`/`_invalid_argument_result` (as `_handle_scroll` does) and pass it on.
- New `_handle_middle_click` copies `_handle_right_click`, including "Vision
  model not configured.". New `_handle_middle_click_at` copies
  `_handle_right_click_at` (`_required_point`).
- Two new `_TOOL_HANDLERS` entries.

**`agents/services/tool_definitions.py`** (desktop
`get_controller_tool_definitions` only).

- New helper `_modifiers_parameter(action: str, example: str) -> ToolParameter`:
  - `type="array"`, `items_schema={"type": "string", "enum": [canonical values]}`,
    `max_items=5`, `required=False`.
  - Description: "Optional. Keys held down during the {action} and released
    afterwards: primary (Cmd on macOS, Ctrl on Windows and Linux), ctrl,
    shift, alt (Option on macOS), meta (Win / Super; Cmd on macOS). {example}
    Only when the step asks for it."
- Add it to the 11 existing desktop pointer definitions and to the two new
  ones. Examples:
  - click: `["primary"]` adds an item to a selection, `["shift"]` selects a
    range;
  - drag: `["alt"]` copies in many file managers;
  - scroll: `["ctrl"]` zooms in many apps, `["shift"]` scrolls sideways.
- New `middle_click` (after `right_click`). Description: "Middle-click (press
  the mouse wheel) an element found by vision-based description, e.g. open a
  link in a new tab, close a tab, or paste the selection on Linux. Verified
  before and checked after like click. Use it only when the step asks for a
  middle-click. Not for web pages opened with browser_navigate."
- New `middle_click_at` (after `right_click_at`), using `_pixel_parameter` and
  `_coordinate_expected_result("middle-click", "the link opens in a new tab",
  "middle-clicks")`.
- Phone definitions are unchanged.

**`agents/services/prompt_parts.py`.**

- Desktop taxonomy: add `middle_click` after `right_click` and
  `middle_click_at` after `right_click_at`, and renumber (32 entries).
- `build_desktop_tool_examples`:
  - Add `middle_click` to the three vision-tool lists.
  - Add the rule "Use middle_click / middle_click_at only when the step asks
    for a middle-click".
  - Extend the `*_at` rule to name `middle_click_at`.
  - Add one modifier rule: "To hold Ctrl/Cmd, Shift, Alt/Option or Win/Super
    during a click, drag, hover or scroll, pass modifiers (primary = Cmd on
    macOS, Ctrl elsewhere); never key_press the modifier first, key_press
    releases every key before it returns. A modified click is not retried on
    another candidate."
  - Change the existing "use scroll …, not PageDown or zoom" rule only by
    adding "(zoom with modifiers only when the step asks for it)".
  - Add example lines: `click(description="the second file", modifiers=["primary"],
    expected_result="both files are selected")`,
    `scroll(direction="up", amount=3, x=640, y=400, modifiers=["ctrl"])`,
    `middle_click(description="the Docs link", expected_result="Docs opens in a new tab")`.
- The phone taxonomy and examples are unchanged.

### Pages and forms

None.

### Background work and WebSockets

No new Celery task, consumer or event. The existing controller messages
`click` (`button="middle"`), `double_click`, `hover`, `drag` and `scroll` gain an
optional `modifiers` field. The field is sent only when non-empty and only to
runners that advertise `pointer_modifiers_v1`.

**MCP machine tools.**

| MCP tool | `_tool(...)` row | Request DTO | Annotation | Handler description (+ `_CLAIMED`) |
|---|---|---|---|---|
| `middle_click` | `CallKind.AI, _DESKTOP, uses_visual_engine=True` (after `right_click`) | `DescribedActionRequest` | `_ACT` | "Middle-click (press the mouse wheel) an element you describe in plain words, e.g. open a link in a new tab (desktop). Optional expected_result is checked afterwards; optional modifiers are held during the click." |
| `middle_click_at` | `CallKind.CONTROL, _DESKTOP, ai_with_expected_result=True` (after `right_click_at`) | `ClickAtRequest` | `_ACT` | "Middle-click at x, y in take_screenshot's width x height (desktop). …" |

- `schemas.py`: add `PointerModifierName = Literal["primary", "ctrl", "shift",
  "alt", "meta"]` (the canonical values, strict) and
  `ModifiersArg = Annotated[list[PointerModifierName] | None, Field(max_length=5,
  description="Optional (desktop): keys held during the action and released
  afterwards. primary = Cmd on macOS, Ctrl elsewhere; alt = Option on macOS;
  meta = Win / Super / Cmd.")]`.
- `DescribedActionRequest`, `ClickAtRequest`, `DragRequest`, `DragAtRequest`
  and `ScrollRequest` gain `modifiers: list[str] | None = None`, and their
  docstrings name the middle tools. `agent_arguments` drops `None`, so the
  agent arguments stay as they are today when no modifiers are given.
- The 13 handlers (11 existing pointer handlers plus the 2 new ones) take
  `modifiers: ModifiersArg = None` as their last parameter and put it on the
  request. A phone project calling `click(modifiers=[…])` gets the registry's
  neutral tool error. Phone-only handlers are unchanged.
- `RunnerOutdatedError` maps to `[machine_outdated]` through the existing
  `map_machine_exception`. No new code is needed for that.
- `run_agent_tool._ECHO_WITHOUT_CHECK_TOOLS` gains `middle_click_at`.
- Counts:
  - the `machine_tools` assert becomes 60;
  - the `machine_handlers` docstring says 60;
  - the `mcp_app` docstrings say 60 machine tools and 75 tools.
- `workflow.py`:
  - Add `middle_click` next to `right_click` and `middle_click_at` to the
    `*_at` list.
  - Add one sentence: "click, drag, hover and scroll tools take optional
    modifiers (desktop)."
  - Wording stays neutral: no AI, MCP or agent.

### Settings / infrastructure

None. No env var. The new runner version is shipped by the normal bundle, and
members press Update Test Runner (SELF_UPDATE_V1).

### Data flow

1. **Agent.** The model calls `click(description="row 3", modifiers=["primary"])`.
   1. `dispatch_tool_call` checks that the tool exists for the target.
   2. `_handle_click` → `_read_modifiers` (desktop, so `(PRIMARY,)`).
   3. `tools_controller.click` → precheck `ensure_runner_supports_pointer_modifiers`
      (one query).
   4. `verified_click`: screen parse plus candidate check, then a before
      screenshot, then `controller_click(..., modifiers=(PRIMARY,))`. The event
      carries `modifiers=["primary"]` → consumer → `click` message.
   5. On the runner, `execute_click`:
      1. the UIPI guard runs;
      2. `to_input` maps the point;
      3. `backend.click(x, y, "left", modifiers=…)` presses Cmd (macOS, with
         flags on the events) or Ctrl down;
      4. the click happens;
      5. the keys are released in `finally`, then the stuck-key check runs.
      6. The runner sends `action_result`.
   6. Server: the outcome check runs once. Confirmed means done; `missed` means
      an error with no retry.
2. **Old runner.** The precheck raises `RunnerOutdatedError`. Nothing is sent and
   no parse is spent. The agent case goes to Need help (`runner_outdated`), and
   MCP returns `[machine_outdated]`.
3. **MCP.** The handler takes `ModifiersArg` and passes it to the request DTO →
   machine gate (claim, `_DESKTOP` for the middle tools) → `run_agent_tool` →
   step 1 from the handler on, with no callbacks.

## Alternatives considered

- **Server-side resolution of `primary` using the runner OS** (the
  `agents/os_shell` pattern). Rejected: it costs a query per call, and the
  runner already knows its platform, while each backend owns its key names.
  The neutral `primary` on the wire also stays valid if the runner is moved to
  another OS.
- **Literal names only (`ctrl`, `cmd`, `option`, `win`, `super`).** Rejected.
  The model would have to map names to the platform every time. pyautogui's
  existing aliases map `cmd` to Win on Windows, which is the wrong key for
  "Ctrl/Cmd+click". The canonical five plus aliases cover both styles.
- **A backend context manager `holding(modifiers)` around the unchanged
  pointer methods.** Simpler, but on macOS pyautogui's mouse events carry no
  flags, so the modifier would not reliably reach the app. The flags must be
  set on the mouse events themselves.
- **Older runner → tool error (the SCROLL_V1 precedent)** instead of Need help.
  Scroll has a working fallback (key_press). A modified click has none: the
  agent would improvise with keyboard selection or fail the step for an
  environmental reason. The DOUBLE_CLICK_V1 precedent is chosen; see Q1.
- **Keep retrying candidates after a missed modified click.** Rejected. A
  Ctrl+click on a wrong candidate toggles that item, and the next press adds
  another one.
- **Key-down / key-up tools** so a key can be held across calls. They are more
  general but can leave keys stuck between calls. Out of scope.

## Risks and mitigations

- **Stuck modifier keys** (the worst case: every later typed character becomes
  a shortcut).
  - Releases run in `finally` / `_then_release`, in reverse order.
  - The pyautogui backend then runs `_release_modifiers`, and `type_text`
    still releases before typing.
  - If a reply timeout fires on the server while the runner is mid-action, the
    runner still finishes its `finally`.
  - Verification includes a forced error (off-screen point) with modifiers.
- **Windows Alt / Win key-up side effects.** Releasing Alt can activate the
  menu bar, and releasing Win can open Start, especially after a hover or
  scroll, where no click intervenes. The mitigation is the `0xE8` mask tap
  before releasing. Verified manually.
- **GNOME Super.** Mutter opens the Activities overview when Super is pressed
  and released alone. A button press probably cancels that, but motion (hover)
  and scroll may not. This is suspected, not confirmed. Verification covers
  `hover_at` and `scroll` with `meta` on GNOME. If the overview opens, the
  GNOME backend refuses `meta` for `move_to` / `scroll` with a clear error.
  That is a one-line follow-up, not a design change.
- **macOS flags.** If the flags were set on down events only, some apps would
  see an unmodified mouse-up. Set them on every posted event.
- **Effects that last only while a key is held** (Cmd+hover link preview). They
  are gone by the time the after screenshot is taken, because keys are released
  when the action returns. The tool definition says keys are released
  afterwards, so the model does not expect a lasting state.
- **Model over-use** (adding modifiers when the step does not ask). The
  parameter descriptions and the rule say "only when the step asks for it". The
  outcome check still runs.
- **Old runner silently ignoring the field.** The server-side capability check
  runs before the send, and the field is never sent when empty.
- **Phone misuse.** The registry refuses on non-desktop targets, the MCP
  `_DESKTOP` rows refuse the middle tools, and the runner's phone backend
  refuses too.
- **Prompt tokens.** The parameter is added to 13 definitions, using one short
  shared description.
- **Wording drift.** Texts without modifiers must stay byte-identical. The
  verifier compares them.
- No auth, secret, XSS or migration risk. The new inputs are a closed enum
  validated on both sides.

## Implementation steps

Steps 1–2 are shared and are written by the orchestrator first. Steps 3, 4, 5
and 6 touch disjoint files and can run as parallel sub-agents. Their shared
contract is:

- `PointerModifier` and its values;
- `parse_pointer_modifiers`;
- the backend keyword `modifiers`;
- the `controller_*(…, modifiers=…)` keyword;
- `ensure_runner_supports_pointer_modifiers`;
- `read_pointer_modifiers` / `modifiers_text`;
- the tool names `middle_click`, `middle_click_at`;
- the argument name `modifiers` (a list of canonical strings).

1. **Protocol** (files: `controller_client/protocol.py`): `PointerModifier`,
   `POINTER_MODIFIER_ORDER`, `parse_pointer_modifiers`, the payload fields and
   parsers, `POINTER_MODIFIERS_V1`, `_DESKTOP_CAPABILITIES` with
   `advertised_capabilities`, the optional-capabilities comment, and
   `CLIENT_VERSION` 0.24.0.
2. **Server plumbing and leaf helper** (files: `projects/controller_protocol.py`,
   `projects/services.py`, `projects/runner_capability_services.py`,
   `agents/pointer_modifiers.py` (new)): the TypedDict fields, payload keys,
   `controller_*` keyword, capability check, and alias reader and text.
3. **Test Runner backends** (files: `controller_client/desktop_backend.py`,
   `controller_client/desktop_pyautogui.py`,
   `controller_client/macos_pointer_events.py` (new),
   `controller_client/desktop_gnome.py`, `controller_client/mobile_backend.py`,
   `controller_client/executor.py`, `controller_client/stubs/Quartz/__init__.pyi`):
   the interface, hold-and-release per backend, macOS flagged events, the
   Windows mask key, phone refusal, and executor pass-through and messages. Run
   `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`.
4. **Agent tool layer** (files: `agents/services/verified_element_actions.py`,
   `agents/services/tools_controller.py`, `agents/services/tool_registry.py`):
   - `default_middle_click_expectation`;
   - `modifiers` on the 13 tools;
   - the prechecks;
   - `_confirm_or_finish(retry_on_miss=)`;
   - `middle_click` / `middle_click_at`;
   - `_read_modifiers`, the handlers and the `_TOOL_HANDLERS` entries.
5. **Definitions and prompts** (files: `agents/services/tool_definitions.py`,
   `agents/services/prompt_parts.py`): `_modifiers_parameter`, the parameters on
   the 13 desktop definitions, the two new definitions, the taxonomy (32), the
   rules and the examples.
6. **MCP** (files: `mcp_gateway/schemas.py`, `mcp_gateway/machine_tools.py`,
   `mcp_gateway/machine_handlers.py`, `mcp_gateway/services/run_agent_tool.py`,
   `mcp_gateway/mcp_app.py`, `mcp_gateway/workflow.py`): `ModifiersArg`, the
   request fields, two rows (assert 60), two handlers plus `modifiers` on 11
   handlers, the echo set, the counts (60 / 75) and the workflow wording.
7. **Integration** (orchestrator; files: `CLAUDE.md`, `agents/CLAUDE.md`):
   1. Check that every definition name has a handler and every MCP row's
      `agent_tool` is a registry name.
   2. Run `pre-commit run --all-files` and `mypy .`.
   3. `CLAUDE.md`:
      - "plus the 60 machine tools";
      - "44 more tools (59 in all)" naming `middle_click` / `middle_click_at`
        and the `modifiers` argument;
      - "16 more, 75 in all";
      - add `POINTER_MODIFIERS_V1` (CLIENT_VERSION 0.24.0, desktop only, older
        runner → Need help `runner_outdated` / `[machine_outdated]`) to the
        controller_client capability list.
   4. `agents/CLAUDE.md`:
      - **Double-click** paragraph: `middle_click` runs `verified_click` with
        `button="middle"`; a modified press is never retried.
      - **`click_at` and `scroll`** paragraph: `middle_click_at`, and
        `modifiers`.
   5. Write `docs/changed/2026-11-09-middle-click-and-modifiers.md`.

## Verification

- `pre-commit run --all-files`, `mypy .` and the controller_client mypy run are
  clean. `python manage.py check` passes.
- `python manage.py shell -c` (read-only) checks:
  - `tool_names_for(DESKTOP)` contains `middle_click` and `middle_click_at`,
    and the phone sets contain neither.
  - Importing `mcp_gateway.mcp_app` passes the 60-tool asserts.
  - `read_pointer_modifiers(["Cmd", "shift", "shift"]) == (PRIMARY, SHIFT)`.
  - `read_pointer_modifiers(["hyper"])` raises.
  - `parse_pointer_modifiers({"modifiers": ["primary", "primary"]})` raises
    `ProtocolError`.
  - `parse_click_payload({"x": 1, "y": 1})` has `modifiers == ()`.
  - The built `click` message for an event without modifiers has no
    `modifiers` key.
- Desktop runner, macOS, Windows and GNOME each (Machine setup chat or a case):
  - `middle_click` on a browser link: a new tab opens and the check is
    confirmed. `middle_click_at` on a tab: the tab closes.
  - Cmd/Ctrl multi-select in a file manager: `click` on file A, then
    `click(description="file B", modifiers=["primary"])`. Both are selected.
    `shift` on file D selects A–D.
  - `drag_at` with `["alt"]` (Option on macOS) copies a file in Finder /
    Explorer / Files.
  - `scroll` with `["ctrl"]` zooms a text editor or image viewer, and
    `["shift"]` scrolls a wide table sideways.
  - `hover_at` with `["primary"]` over a link in an editor that previews on
    Cmd/Ctrl-hover: the after screenshot shows the modifier was released.
  - After every modified call, `type_text("abc")` types plain "abc" and
    `diagnose --input` reports no stuck modifier.
  - Forced error: `click_at` with `modifiers=["shift"]` at an off-screen point
    returns the "outside the screen" error, and the next `type_text` is not
    shifted.
  - Windows: `hover_at` and `scroll` with `["meta"]` and `["alt"]` do not open
    Start or activate the menu bar. GNOME: `hover_at` and `scroll` with
    `["meta"]` do not open Activities. If they do, apply the follow-up in Risks.
  - Windows elevated window in front: a modified click returns
    `InputBlockedError` and presses no key.
- Old runner (0.23.3, or a stubbed capability list without
  `pointer_modifiers_v1`):
  - A case using `click(modifiers=…)` goes to Need help with fault
    `runner_outdated` before any screen parse.
  - The same call without modifiers works.
  - MCP returns `[machine_outdated]`.
  - `middle_click` works on the old runner.
- Phone runner: `click(modifiers=["shift"])` gets the "only on a desktop
  computer" tool error. MCP `middle_click` gives `[wrong_target]`.
- Verified flow: with modifiers and an `expected_result` that cannot be shown,
  `click` presses once and returns the "Not retried" error. Without modifiers it
  still retries other candidates.
- Wording: every tool's text without modifiers is unchanged (diff review).
- MCP (claimed Manual case, desktop): `tools/list` shows 75 tools, and the
  `modifiers` schema lists the five values. No result text names AI, MCP or an
  agent.
- Run `reviewer` and `verifier` in parallel on the diff.

## Open questions

Resolved with the user on 2026-11-09, all as assumed:
- **Q1.** Older runner → Need help `runner_outdated` (MCP `[machine_outdated]`).
- **Q2.** `cmd` / `command` alias → `primary` (Ctrl on Windows and Linux).
- **Q3.** A modified verified press is never retried on another candidate.

## Changelog
- 2026-11-09: created
- 2026-11-09: open questions resolved (all as assumed); status In progress
- 2026-11-09: implemented. Review fixes: modified pointer actions wait for a reconnect but are never resent; release-failure text says the action was done; macOS drag button-up never hides the drag error; MCP DTO field typed `list[PointerModifierName]` (list invariance). Rejected nits: `schemas.py` Literal kept instead of `list[PointerModifier]` (keeps `agent_arguments` dumping plain strings); two capability queries on a modified scroll/double_click kept (only with modifiers); GNOME `_release_keys` catching only `DesktopSessionError` is the existing `press_keys` pattern.
- 2026-11-09: deferred (rare): an older runner taking over within the reconnect grace between the capability check and the first send would get a modified action and do a plain one; closing it needs a capability recheck after `wait_for_controller`. The no-resend error hint applies to every no-resend action (accurate for all, nothing parses it).
- 2026-11-09: review and verification clean; Status Done.
