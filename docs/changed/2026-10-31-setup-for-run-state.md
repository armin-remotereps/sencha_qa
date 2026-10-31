# Machine setup prepares for a run's current state

Date: 2026-10-31
Plan: docs/plan/feat-setup-for-run-state.md

## What changed
- `machine_setup/services/run_state.py`: `run_state_text` renders where one run is now: finished cases counted, open cases (running, Need help, Your turn) with preconditions, plan steps and the step they stopped at, not started cases grouped by precondition (paged). At most 5 queries, output bounded.
- `machine_setup/services/main_chat_tools.py`: new main-chat tool `get_test_run_state(run_id, page?)` (`GET_TEST_RUN_STATE_TOOL` in `machine_setup/run_proposal_types.py`).
- `machine_setup/services/setup_prompt.py`: main-chat job line, GOAL line and a `_RUN_STATE` section: when the user names a run, read its state first and prepare for it instead of S0.
- `machine_setup/services/run_catalog.py`: `_status_label` is now public `run_status_label` (keyword counts), shared with the new tool.

## Why
The setup agent always prepared S0 (every case's preconditions). When a user asks to set up for a run that is already part-way through, it should prepare for that run's current step instead.

## Behaviour and contract changes
- New setup agent tool `get_test_run_state` (main chat only; help chat, result chat and auto-fix unchanged). No UI, URL, migration or setting change.

## Notes for future changes
- Step N is `CaseStepProgress.current_step`, else the AWAITING_HELP / AWAITING_PERSON row, else the open `CaseHelpRequest.step_num`.
- Only current-screen cases get "passed steps' effects expected on the machine"; restore and from-scratch cases get preconditions only (the server rebuilds the rest).
- Needs re-test counts as finished. Setup sends are refused while a run is Started, so in practice this serves Paused runs.
- `pre-commit run --all-files` skips untracked files: run it with `--files` on new modules before committing.
