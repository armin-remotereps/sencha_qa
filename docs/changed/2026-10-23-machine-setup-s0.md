# Machine setup (S0) chat agent; versioned application context

Date: 2026-10-23
Plan: docs/plan/feat-machine-setup-s0.md

## What changed
- `agents/services/agent_loop.py`:
  - the loop body is now the public `run_tool_loop(messages, context, *, config, tool_definitions, dispatch, on_transcript)`;
  - `_run_agent_loop` (the tester) calls it unchanged.
- `agents/types.py`:
  - `ToolResult.verbatim` (skips the output summary);
  - `ToolCategory.PROJECT_DATA`;
  - `ToolDispatcher` and `TranscriptCallback`.
- `agents/transcript.py` (new, pure): `transcript_to_json` / `transcript_from_json` / `close_dangling_tool_calls`. The transcript is replayable and keeps the encrypted reasoning.
- `agents/services/prompt_parts.build_project_context(framing=)` is now public. The tester's wording is unchanged.
- New app `machine_setup`:
  - models `SetupConversation` (one active per project), `SetupTurn` (one live per project), `SetupMessage` (the displayed chat) and `SetupScreenshot`, with migration 0001;
  - `turn_state.py`: limits, `live_turn_exists` and `MACHINE_SETUP_RUNNING_MESSAGE`; `payloads.py`; `events.py`; `exceptions.py`; `turn_outcome.py`.
- `machine_setup/services/`:
  - `send_message`, `stop_turn`, `reset_conversation` and `live_turn` (stale and queued turn closing);
  - `chat_page`, `case_catalog` (read-only case queries), `setup_tools`, `setup_prompt` and `run_turn` (the worker use case).
- `machine_setup.tasks.run_setup_turn(turn_id)` on the `execution` queue.
- `machine_setup.consumers.MachineSetupConsumer` at `ws/projects/<id>/machine-setup/`, wrapped in `AllowedHostsOriginValidator`.
- Templates:
  - `templates/machine_setup/` (chat page and includes), with chat CSS in `punk-hazard.css`;
  - a "Machine setup" tab in `partials/project_tabs.html`.
- Application context (`projects`):
  - `Project.project_prompt_revision` and provenance fields; `ApplicationContextVersion` history (last 20); migration 0024;
  - `projects/application_context_services.py`: every write is versioned, plus restore and provenance;
  - the history card on the application context page, and a provenance line there and on the overview.
- Run gating: `start_test_run`, resume and `get_run_readiness` (new check `no_machine_setup`) refuse while a setup turn is live.

## Why
Bringing the test machine to "state 0" (what the cases' preconditions assume) took a person a long time. Now a member chats with an agent that has every tester tool plus test-case queries, and it prepares the machine before runs. When setup is done the agent writes the project's application context: the app, the machine state, and which preconditions are covered and which are not. Every test run then knows the machine.

## Behaviour and contract changes
- New pages and URLs, all `@project_membership_required`:
  - `machine_setup:chat` (GET `/projects/<id>/machine-setup/`);
  - `machine_setup:send` (POST `messages/`, field `text` ≤ 8000);
  - `machine_setup:stop` (POST `stop/`);
  - `machine_setup:reset` (POST `reset/`);
  - `projects:application_context_restore` (POST `/projects/<id>/application-context/versions/<version_id>/restore/`, field `next`, same-host).
- WebSocket `ws/projects/<id>/machine-setup/`. Client messages: `current_state`, `message`, `screenshot`, `turn`, `reset` and `agent_status`. The HTML page carries no chat rows; `current_state` delivers them.
- Agent tools: every tester tool, plus `list_test_case_preconditions`, `search_test_cases`, `get_test_case`, `get_application_context` and `update_application_context(text, change_summary, base_revision)`.
  - The context write is conditional on the revision the agent read.
  - User saves are last write wins, and the replaced text stays in history.
- Sending requires the Test Runner connected, the visual engine ready, no run Started and no live turn.
  - Runs cannot start or resume while a turn is live.
  - Reset archives the conversation (admin only) and stops a live turn. Sending stays blocked until that turn ends.
  - Stop on a queued turn closes it at once. A running turn stops at the next tool boundary.
- Notes limit `PROJECT_PROMPT_MAX_LENGTH` raised from 4000 to 12000 (now in `projects.models`). The refiner output is truncated to it.
- `save_project_prompt` / `save_application_context` take `author=` (and `source=`) and return None.
- New settings: `MACHINE_SETUP_MAX_ITERATIONS` (200) and `MACHINE_SETUP_TURN_TIMEOUT_SECONDS` (3600). Task route `machine_setup.tasks.run_setup_turn` → `execution`.
- Deploy:
  - run `migrate` (`machine_setup.0001`, `projects.0024`, both additive);
  - restart the execution and upload workers and Daphne/web.

## Notes for future changes
- The setup agent's text becomes PROJECT CONTEXT in every tester prompt. Injected case text could steer the setup agent into planting instructions there. Mitigations so far:
  - the prompt treats tool and case text as data;
  - a chat notice is posted on every write;
  - the provenance line, history and restore.
  The agent also has shell access driven by imported case text, with prompt-only safety (Q11).
- The transcript is saved on each assistant message and after each round's last tool result. `close_dangling_tool_calls` runs before every replay, so a killed worker cannot leave an unreplayable chat. The rule that every `function_call` must have an output (the API rejects it otherwise) is from community reports; it is not verified live.
- The Test Runner empties `~/Downloads` before every case, so S0 must not leave anything the tests need there. The prompt says so.
- A turn can hold an `execution` worker slot for up to about an hour (shared with runs of other projects).
- Setup screenshots are served from MEDIA without auth, like run screenshots (existing pattern).
- Follow-ups:
  - The older WebSocket routes still lack an origin check. A stack-wide `AllowedHostsOriginValidator` would break the Test Runner's socket, which sends no Origin.
  - `sub_agent_prompts._build_project_context` keeps its own wording.
- `case_catalog` uses `ArrayAgg` (Postgres only).
- Verified with:
  - pre-commit (mypy strict), `manage.py check` and `makemigrations --check`;
  - a throwaway-DB smoke script with a scripted LLM: gates, a full turn with a context write and notice, replay, stale revision, stop and reset, restore, IDOR/404, pruning, escaping;
  - a WebSocket check that a foreign origin is rejected.
  Not yet run against a live runner or the real model (plan Verification steps 2–12 and 16).
