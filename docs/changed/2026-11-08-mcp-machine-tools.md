# MCP machine tools: the agent's tools inside a claimed Manual case

Date: 2026-11-08
Plan: docs/plan/feat-mcp-runner-tools.md

## What changed
- `mcp_gateway`: 36 new tools (51 in all), added after the 15 existing ones, which are unchanged:
  - the table `machine_tools.py` (`MACHINE_TOOLS`, `effective_kind`);
  - handlers in `machine_handlers.py`, with the shared `tool_runner.py`;
  - the gate `services/machine_gate.py`: the caller's own live MCP claim, connected, phone online, right target, no readiness Scan/Check running, and a visual engine for the tools that find elements by description;
  - agent dispatch in `services/run_agent_tool.py` (`allowlisted_dispatcher`, no callbacks), with `run_web_search` for search (snippets only, no page fetch);
  - `services/machine_screenshot.py` (`take_screenshot`);
  - the command tools in `services/command_sessions.py`, `command_registry.py` (Redis, per claim), `stop_claim_commands.py` and `session_replies.py`;
  - `services/machine_errors.py` (`[code]`s, `neutral_machine_text`);
  - `capture_lock.py` renamed to `services/machine_round_trip.py` (one machine lock shared with capture);
  - new `CallKind`s CONTROL / AI / COMMAND / SCREEN, a `machine` block in `get_project`, a workflow paragraph and `untrusted_text.fence_data`.
- Tasks: `mcp_gateway.tasks.stop_claim_commands` (on commit after an MCP hand-in or release) and `sweep_ended_claim_commands` (beat every 60 s), both on `assist`.
- `projects`:
  - `controller_command_sessions.py` (kept start, read, list);
  - `call_deadline.py`, a scoped deadline that caps controller reply waits (`_send_controller_action_once`), the LLM request (`agents/http_deadline.py`, `llm_client`) and the visual-engine parse;
  - `no_reconnect_wait()` in `controller_reconnect_services.py`;
  - `KeptSessionLimitError`;
  - pure capability helpers in `runner_capability_services.py`.
- `agents/services/tools_search.py`: `web_search_snippets` (SearXNG results only, no page fetched by the server; used by MCP `web_search`), and the SearXNG request bounded by a call deadline. The agent's own search is unchanged.
- `controller_client` (CLIENT_VERSION 0.21.0, `KEPT_SESSIONS_V1`):
  - kept command sessions next to the agent's foreground session (`interactive_session.py`, `kept_output_buffer.py`, process `drain` / `kill_tree` / `at_eof`);
  - `list_interactive_sessions`;
  - the `TOO_MANY_SESSIONS` error code.
- `auto_tester/mcp_asgi.py`: a bounded default executor (`MCP_EXECUTOR_THREADS`).

## Why
MCP is the AI that helps a person doing a manual test. The user asked for every tool the server agent has, so the client can drive the test machine while it performs its claimed case. The user's decisions are D0–D8 in the plan.

## Behaviour and contract changes
- **New MCP tools.** Each takes `project_id`, `pivot_id` and `claim_token`, and works only inside the caller's own live MCP claim:
  - Commands: `execute_command(command, cwd?, detach?)`, `read_command_output`, `send_command_input`, `stop_command`, `list_commands`.
  - Screen and input: `take_screenshot`, `click`, `double_click`, `click_at`, `type_text`, `key_press`, `hover`, `drag`, `scroll`.
  - Apps: `launch_app`, `close_app`, `check_app_installed`, `close_tab`.
  - Browser: the `browser_*` tools except `browser_take_screenshot`.
  - Phone: `long_press`, `swipe`, `press_button`, `rotate_device`, `hide_keyboard`, `open_url`, `install_app`.
  - No machine: `web_search`, `wait`.
- **`take_screenshot`** returns a preview only. It is never staged and never evidence; `capture_screenshot` / `add_screenshot` stay the only evidence tools.
- **`get_project`** gains `machine: {target, connected}`. This is additive; connected clients re-read the tool list.
- **No opt-in.** Connection keys and OAuth grants may both use the tools. Remote code execution through `execute_command` is accepted (D1).
- **Commands.** On 0.21.0 runners a command starts as a kept session: up to 4 per runner, 4 h, a 1 MiB buffer.
  - `detach` returns its id at once.
  - Command ids belong to the claim. They are stopped at claim end: at once after an MCP hand-in or release, otherwise within about 60 s.
  - Older runners: `detach` gives `[machine_outdated]`; a plain command uses the single foreground session (one at a time, 300 s).
- **Controller protocol.** Optional fields `keep`, `max_seconds`, `initial_read_ms` and `max_kept` on `start_interactive_cmd`; `output_dropped` on `interactive_output`; the new messages `list_interactive_sessions` / `interactive_sessions`; error code `TOO_MANY_SESSIONS`.
  - `REQUIRED_CLIENT_CAPABILITIES` is unchanged.
  - Runners need Update Test Runner before detach works.
- **Settings.** New Celery routes and a beat entry (`mcp-gateway-sweep-ended-claim-commands`).
- **No migrations.**

## Notes for future changes
- **Not yet run anywhere real:** no real runner, phone, LLM, Redis or MCP client, and `scripts/verify_server_image.sh` was not run (no Docker here). Walk the plan's Verification list on staging, including Windows kept sessions (`taskkill /T` cannot reach `start /b` children) and the person window, which hides on every command read.
- **MCP `web_search` never fetches pages on the server** (no SSRF through result URLs); its errors are a fixed `[search_failed]`.
- **Nothing user-visible names AI, MCP or an agent.** The rewrite is best effort: it skips file names, paths and "user agent", but a spaced path segment with the word not first (`Acme AI Tools`) is still rewritten. Machine text is fenced as data and helper words are rewritten by `neutral_machine_text`. Successful replies that only echo the caller's input keep their words.
- **Attribution rule (D5) for any future user-visible text:** "<member> via Test Runner" (OAuth) or "a person at the Test Runner" (key). The manual tools still say "a tester"; aligning them is a follow-up.
- **The agent's existing debug log lines can contain argument text.** This is kept on purpose: the user wants logs for debugging.
- **Pre-existing, not fixed:** on POSIX, `ShellProcess.read` returns all output since the start on every call (the agent path). The MCP code keeps only the last read for non-kept sessions.
- **Every machine lock TTL must stay ≥ its call deadline + margin.** `limits.py` asserts this. Keep new waits under `call_deadline`.
