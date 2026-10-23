# Machine setup delegates machine work to sub-tasks

Date: 2026-10-23
Plan: docs/plan/feat-machine-setup-sub-tasks.md

## What changed
- `machine_setup/services/setup_tools.py`:
  - `main_tool_definitions()`: the six project-data tools plus the new `run_setup_task`. Any other tool name is a "not available, delegate" tool error.
  - `machine_tool_definitions()` / `build_machine_dispatcher()` are the sub-agent's tool set. `setup_tool_definitions` is removed.
- `machine_setup/services/setup_prompt.py`: the main prompt is now a lead that plans and delegates. Tool taxonomy and examples are gone, and new DELEGATION, REPORTS and SAFETY rules are in.
- New `machine_setup/services/run_setup_task.py` (with `setup_task_prompt.py`):
  - one sub-task is a fresh in-memory `run_tool_loop` with the tester's tools and an executor prompt;
  - it returns only a report (`RESULT` DONE / FAILED / NEEDS_USER, SUMMARY, FACTS, ERROR);
  - the report is parsed by the pure `machine_setup/subtask_report.py`. A missing RESULT gives UNCLEAR. A timeout or step limit gives FAILED.
- New `machine_setup/services/activity_recorder.py`:
  - the row recording pulled out of `run_turn._TurnRunner`, used by the main loop and by each sub-task (whose rows get `parent`);
  - `fail_running_rows` closes nested rows and marks the delegation `interrupted`.
- `machine_setup/services/run_turn.py`:
  - builds `SetupTaskRuntime` and allows one sub-task per assistant message;
  - the dangling-call note is now "Interrupted… check the machine".
- New `machine_setup/subtask_types.py`, `subtask_budget.py` (budget clamped to the turn's remaining time minus 60 s) and `message_text.py`.
- `machine_setup/services/chat_page.py`: the page loads the last 300 top-level rows, plus the last 50 children per delegation in one windowed query. `live_turn.py` uses `fail_running_rows`.
- Templates:
  - `templates/machine_setup/chat.html`, `includes/_subtask.html` (new), `_transcript.html`;
  - `punk-hazard.css` gains `.chat-subtask*`;
  - nested collapsible block: "goal · N steps · badge", plus the task details and the report.

## Why
The setup chat kept every shell output and page read in one transcript, replayed on every turn with no ceiling. Now each sub-task's context is thrown away once it reports. The main transcript holds only delegation calls and reports of up to 8 KB, and the page still shows every step.

## Behaviour and contract changes
- **Migration `machine_setup.0002`** (additive):
  - `SetupMessage.parent` (self-FK, indexed with `id`);
  - `SetupMessage.subtask_result`;
  - a partial top-level index.
- **Main agent tool:** new `run_setup_task(goal, done_when, known_state?, timeout_seconds?, read_only?)`. The main agent no longer has any machine tool, screenshot or `web_search`.
- **Settings** (in `example.env`): `MACHINE_SETUP_SUBTASK_TIMEOUT_SECONDS` (900), `_MIN_TIMEOUT_SECONDS` (150), `_MAX_TIMEOUT_SECONDS` (1800) and `_MAX_ITERATIONS` (100). `MACHINE_SETUP_MAX_ITERATIONS` now counts main-agent rounds only.
- **Message payload:** new fields `parent_id`, `subtask_result`, `subtask_result_label` and `hidden_child_count`. Delegation arguments are capped per field, not as one JSON string.
- **Deploy:** run `migrate`, then restart the execution worker and web/Daphne.

## Notes for future changes
- **Prompt injection:** case text reaches the main agent as data, and the main agent writes the goals that a sub-agent with shell access follows. Mitigations:
  - goals must be in the main agent's own words;
  - the user approves destructive work first;
  - the executor returns NEEDS_USER for any destructive action its goal doesn't name;
  - KNOWN STATE is labelled as data;
  - the sub-agent has no project-data tools and cannot write the context.

  Some risk remains, of the same class as before.
- **Prompt-only rules:** `read_only` is not enforced on the server. "One sub-task per round" is enforced in `run_turn`, because the model's parallel tool calls are on by default.
- **Old transcripts:** they still hold direct machine tool calls. Replay without those tool definitions has not been checked against the live API (plan Q5). If it fails, Reset clears the chat.
- **`hidden_child_count`** is only set on page load. The client keeps it across live updates of the delegation row.
- **Verified with:**
  - pre-commit (mypy strict). black still reports an unrelated, pre-existing reformat of `projects/migrations/0022`, which was left untouched.
  - `manage.py check` and `makemigrations --check`;
  - a throwaway-DB smoke script with a scripted LLM, 23 checks: nesting, the transcript holding no child output, the one-at-a-time guard, the tool refusal, UNCLEAR / FAILED, the budget, Stop marking the delegation interrupted plus a clean replay, the page window, and long arguments.
  - Not yet run against a live runner and the real model (plan Verification, manual 1–10).
