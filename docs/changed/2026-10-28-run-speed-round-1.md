# Run speed, round 1

Date: 2026-10-28
Plan: docs/plan/feat-run-speed-round-1.md

## What changed
- `agents/services/shell_command.py`: POSIX `cwd` is quoted (`cd -- <quoted>`); a leading `~`, `~user`, `$HOME` or `${HOME}` still expands.
- `agents/services/agent_loop.py`, `llm_client.py`, `sub_agent.py`, `orchestrator.py`: when a step, recovery or restore sub-agent runs out of time, the model gets one last call with `tool_choice="none"` (`FinalAnswerPolicy`). An explicit `RESULT: PASS` there is a normal pass, with "(answered when the step's time budget ran out)" added to the summary.
- New tools `scroll(direction, amount?, x?, y?)` and `click_at(x, y, expected_result?)` (`tools_controller`, `tool_registry`, `tool_definitions`, `projects.services.controller_scroll`, `controller.scroll` consumer event). `click_at` reuses the `click` message. `take_screenshot` tells the vision model the image size.
- Test Runner:
  - `controller_client/executor.py` maps every pointer action from screenshot pixels to input coordinates (`pointer_mapping.map_to_input`, backend `capture_size()` / `input_size()`).
  - Scroll uses direct wheel events: Quartz on macOS, `mouse_event` on Windows, Mutter `NotifyPointerAxisDiscrete` on GNOME.
  - pyautogui releases modifiers that are still held after key combos and before `type_text` (macOS: by flag state, with a warning; Windows: by `GetAsyncKeyState`).
- `agents/command_output.py` (new): `clean_command_output` strips ANSI/OSC codes, keeps the last carriage-return redraw, collapses 3+ identical lines and drops trailing blank lines. It runs on `execute_command` / `wait_for_command` / `send_command_input` output.
- Prompts:
  - Planner and the help/result-chat redesign rules: shell work is written as a command, never "in Terminal"; consecutive read-only steps are merged.
  - Executor: shell commands always go through `execute_command`; no opening screenshot for shell-only steps; predictable actions are chained in one turn; scroll/click_at are documented, and faking clicks with scripts is forbidden.

## Why
Two analysed runs (`docs/analysis/run-speed-findings.md`) lost most of their time to help pauses and timeouts caused by missing capabilities (no scroll, no coordinate click, stuck modifiers), to steps that had succeeded but timed out unanswered, and to the agent opening the Terminal GUI for shell work.

## Behaviour and contract changes
- Controller protocol: new `scroll` message and optional capability `scroll_v1`; `CLIENT_VERSION` is 0.7.0. An older runner gets a `scroll` tool error suggesting key presses; `click_at` works on every runner.
- Every pointer action on a 0.7.0 runner is scaled when the screenshot and input sizes differ (Retina Macs, Windows DPI mismatch). Equal sizes, and always on GNOME, change nothing.
- New setting `AGENT_FINAL_ANSWER_TIMEOUT_SECONDS` (default 90, 0 disables the final turn).
- A step that answers PASS in its final turn now counts as passed (it was a FAIL "Timed out").
- No migration.

## Notes for future changes
- Not yet run on real machines. The plan's Verification section lists what to check:
  - `tool_choice="none"` accepted after function items;
  - scroll signs on macOS, Windows and GNOME;
  - the Retina and Windows 125% mappings;
  - the macOS modifier warning, to answer whether stuck modifiers caused the emoji picker (findings Q4).
- Teardown and machine setup keep `final_answer=None` and the old timeout behaviour.
- `TEARDOWN_TOOL_NAMES` is unchanged; scroll/click_at are not teardown tools.
- Known cosmetic gap: an escape sequence split across two `wait_for_command` chunks can leave stray characters.
- Three files already on main fail black (two migrations and `machine_setup/services/case_result_context.py`); left untouched here.
