# Middle click and held modifier keys

Date: 2026-11-09
Plan: docs/plan/feat-middle-click-and-modifiers.md

## What changed
- `agents/services/tools_controller.py`, `tool_registry.py`, `tool_definitions.py`, `prompt_parts.py`, `verified_element_actions.py`: desktop `middle_click` (verified flow, `controller_click(button="middle")`, `default_middle_click_expectation`) and `middle_click_at` (`act_once_and_check`); optional `modifiers` on click, double_click, right_click, middle_click, their `*_at` variants, hover, hover_at, drag, drag_at and scroll (`_modifiers_parameter`, `_read_modifiers`).
- `agents/pointer_modifiers.py` (new leaf): `read_pointer_modifiers` (canonical names plus aliases `cmd`/`command`→`primary`, `option`→`alt`, `win`/`super`→`meta`), `modifiers_text`.
- `projects/services.py`: `controller_click/double_click/hover/drag/scroll(*, modifiers=)`; the field is sent only when non-empty; a modified action waits for a reconnecting runner but is never resent (`wait_before_send`, `_run_with_reconnect_retry(wait_first=)`). `projects/runner_capability_services.ensure_runner_supports_pointer_modifiers`.
- `controller_client/`: `PointerModifier`, `parse_pointer_modifiers`, payload `modifiers` fields, `POINTER_MODIFIERS_V1` (desktop only), CLIENT_VERSION 0.24.0. Backends hold the keys for the action and release them in reverse in `finally` (pyautogui + `_release_modifiers` check, Windows `0xE8` menu-mask tap before Alt/Win up; GNOME `_holding` on the session stream; macOS flagged Quartz events in the new `macos_pointer_events.py`, where the old double-click and scroll Quartz code moved). Phones refuse modifiers.
- `mcp_gateway`: `middle_click` (AI, visual engine) and `middle_click_at` (CONTROL) desktop rows, `ModifiersArg` on the 13 pointer tools; 60 machine tools, 75 MCP tools.

## Why
The tools could not press the mouse wheel as a button, and could not hold Ctrl/Cmd, Shift, Alt/Option or Win/Super during a pointer action (multi-select, range select, Alt+drag copy, Ctrl+wheel zoom, Shift+wheel). `key_press` releases every key before it returns, so it could not be combined with a click.

## Behaviour and contract changes
- New agent and MCP tools `middle_click(description, expected_result?, modifiers?)`, `middle_click_at(x, y, expected_result?, modifiers?)` (desktop; no runner change, the `click` message's `button` already took "middle").
- New optional `modifiers` argument (list of `primary`, `ctrl`, `shift`, `alt`, `meta`; `primary` = Cmd on macOS, Ctrl elsewhere) on the desktop pointer tools and an optional `modifiers` field on the `click`, `double_click`, `hover`, `drag` and `scroll` controller messages.
- A runner without `pointer_modifiers_v1` is refused before any parse or input: Need help `runner_outdated`, MCP `[machine_outdated]`. Update Test Runner is offered (0.24.0).
- A modified verified press is never retried on another candidate after a `missed` check; it returns an error asking to re-observe.
- Every call without modifiers sends the same messages and returns the same texts as before. The lost-connection error of no-resend actions now adds "it may already have been done, so check before repeating it".

## Notes for future changes
- Not yet run on a real runner. Manual checks in the plan's Verification: macOS flagged events, the Windows Alt/Win mask tap, GNOME Super on hover/scroll (may open Activities; fallback is refusing `meta` there).
- No modifiers on browser tools, `type_text`, phone tools; no key_down/key_up across calls; no middle-button drag.
- Deferred: the capability is checked before a reconnect wait, not after; an older runner taking over in that window would do a plain action.
