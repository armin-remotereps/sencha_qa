# Agent double-click, 300 iterations, closing apps and tabs

Date: 2026-10-22
Plan: docs/plan/feat-agent-double-click-iterations-close-app.md

## What changed
- `agents/services/tools_controller.py`: new `double_click` (shares the verified click flow through `_verified_click`; `click` unchanged), `browser_double_click`, `close_app`, `close_tab`; definitions in `tool_definitions.py`, handlers in `tool_registry.py`.
- `agents/services/prompt_parts.py` / `orchestrator_prompts.py` / `sub_agent_prompts.py`: planner appends "Afterwards, close <App> with close_app" (or close_tab) to the last sub-task using an app; sub-agent closes only after verifying, and a failed close never fails a step.
- `projects/services.py`: `controller_double_click`, `controller_browser_double_click`, `controller_close_app`, `controller_close_tab`; events in `controller_protocol.py` / `controller_consumer.py`.
- `projects/runner_capability_services.py`: `runner_supports`, `ensure_runner_supports_double_click` -> `RunnerOutdatedError`.
- `controller_client/executor.py` `execute_double_click` (macOS posts Quartz events with click state 1/2), `browser_executor.py` `execute_browser_double_click` (`dblclick`), new `app_closer.py`, `app_closer_macos.py`, `app_closer_windows.py`, `app_close_policy.py`.
- `auto_tester/settings.py`, `agents/types.py`, `example.env`: `AGENT_MAX_ITERATIONS` and `SUB_AGENT_MAX_ITERATIONS` default to 300.

## Why
The agent could not double-click; 15 turns per step (the cap test runs actually use) was too few; apps left open clutter every later OmniParser parse. Only a tab must be closed when its app also hosts the Test Runner (its terminal).

## Behaviour and contract changes
- Controller protocol 0.5.0: messages `double_click {x, y}`, `browser_double_click {element_index, browser}`, `close_app {app_name, force}`, `close_tab {app_name}`. Optional capabilities `DOUBLE_CLICK_V1`, `CLOSE_APP_V1`; 0.4.0 runners still connect.
- New `BlockedReason.RUNNER_OUTDATED` ("Test Runner needs an update", migration 0022, choices only): a double-click on an old runner blocks the case. close_app/close_tab on an old runner is only a tool error.
- **Manual step:** set `AGENT_MAX_ITERATIONS=300` and `SUB_AGENT_MAX_ITERATIONS=300` in every deployed `.env` (the local one is done). The sub-task time budget (150-900 s) is now the binding limit.
- Controller requirements: `psutil`, `types-psutil` pinned. Test machines need the new runner.

## Notes for future changes
- close_app/close_tab fail closed: never the runner, its ancestors, any known terminal host (Windows Terminal, iTerm2, Warp, ...) while the runner has a console/tty, or the OS shell. close_tab on Terminal/iTerm2 compares the front tab's tty via osascript; unknown means refuse. Windows Terminal tabs can't be closed by close_tab (agent is told to type `exit`).
- macOS AppKit's running-app list does not refresh without a run loop; the closer uses the window server list (`CGWindowListCopyWindowInfo`).
- Not live-tested: Quartz double-click in Finder/TextEdit, macOS Automation prompts for `terminate()`/osascript, all Windows ctypes paths.
- OmniParser images were checked: same PNG `input_image` data URL, no `detail`, no server resize as every other screenshot; the runner downsizes only above 8 MiB.
