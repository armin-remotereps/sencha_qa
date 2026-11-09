# Coordinate variants of the pointer tools

Date: 2026-11-09
Plan: docs/plan/feat-coordinate-pointer-tools.md

## What changed
- `agents/services/act_and_check.py` (new): `act_once_and_check`, the one "act once, optionally check once" flow; `click_at`, phone `swipe` and `rotate_device` moved onto it with unchanged wording (`tools_mobile._act_and_check` removed).
- `agents/services/tools_controller.py`: `double_click_at`, `hover_at`, `drag_at(duration)`; `tools_mobile.py`: `long_press_at`.
- `agents/services/tool_registry.py`: handlers and `_required_point` for the new tools; `click_at` keeps `_read_point` (its one-coordinate-missing message differs).
- `agents/services/tool_definitions.py` / `prompt_parts.py`: definitions per target (desktop: double_click_at, hover_at, drag_at; phone: double_click_at, long_press_at, drag_at), taxonomies, rules and examples.
- `mcp_gateway`: the four as machine tools (`machine_tools.py`, `machine_handlers.py`, `DragAtRequest`, `LongPressAtRequest`), CONTROL throttle, AI when `expected_result` is given, no visual engine; wording that recommends `click_at` now names the `*_at` tools. 56 machine tools, 71 MCP tools.
- `controller_client/protocol.py`: `DRAG_MIN_SECONDS` 0.1, `DRAG_MAX_SECONDS` 10, `DEFAULT_DRAG_SECONDS` 0.5 (server-side bounds only).

## Why
`double_click`, `hover`, `drag` and `long_press` could only find targets by description through the visual engine; targets it cannot describe (canvas points, slider positions) or a visual engine outage left no way to act on them.

## Behaviour and contract changes
- New agent and MCP tools: `double_click_at(x, y, expected_result?)`, `hover_at(x, y, expected_result?)` (desktop), `drag_at(start_x, start_y, end_x, end_y, duration?, expected_result?)`, `long_press_at(x, y, duration?, expected_result?)` (phone).
- No Test Runner change, no capability, no `CLIENT_VERSION` bump: they reuse the `double_click`, `hover`, `drag`, `long_press` messages. `double_click_at` refuses runners without `double_click_v1` like `double_click` (Need help `runner_outdated`; MCP `[machine_outdated]`).
- `take_screenshot`'s size note names "click_at, the other *_at tools and scroll".

## Notes for future changes
- Failed-case teardown stays `click` only and the person-step confirm allowlist has no input tools (user decision).
- Playwright `browser_*` description tools got no coordinate variant; the MCP `browse_*` tools address elements by index/css/xpath.
- `act_once_and_check` takes many pass-through parameters; if more callers come, bundle `vision_config` / `on_screenshot` / `cancellation_check` / `deadline` into one dataclass.
- Not yet run on a real runner or device.
