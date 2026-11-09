# Right click tools

Date: 2026-11-09
Plan: docs/plan/feat-right-click-tools.md

## What changed
- `agents/services/tools_controller.py`: `right_click` (verified flow `verified_click` with `controller_click(button="right")`, `default_right_click_expectation` in `verified_element_actions.py`) and `right_click_at` (`act_once_and_check`, like `click_at`).
- `agents/services/tool_registry.py`: `_handle_right_click`, `_handle_right_click_at` (`_required_point`).
- `agents/services/tool_definitions.py` / `prompt_parts.py`: desktop definitions, taxonomy, rule "only when the step asks for a right-click or context menu", examples.
- `mcp_gateway`: `right_click` (AI, visual engine) and `right_click_at` (CONTROL, AI with `expected_result`) machine tools, desktop only; 58 machine tools, 73 MCP tools.

## Why
Context menus could not be opened: the tools only pressed the left button.

## Behaviour and contract changes
- New desktop agent and MCP tools `right_click(description, expected_result?)` and `right_click_at(x, y, expected_result?)`.
- No Test Runner change or capability (the shared `CLIENT_VERSION` 0.23.3 bump is in `2026-11-09-coordinate-pointer-tools.md`): the `click` message's `button` field has carried "right" since the first client version. Phones get neither tool (the phone backend refuses a non-left button); long press is their context menu.

## Notes for future changes
- No `button` argument on `click` / `click_at`, no browser right click, no middle click.
- Teardown stays `click` only.
- Not yet run on a real runner.
