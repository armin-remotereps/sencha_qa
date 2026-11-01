# Pause and Discuss for Manual runs

Date: 2026-11-01
Plan: docs/plan/feat-manual-pause-and-discuss.md

## What changed
- `projects/manual_case_pause_services.py` (new): `pause_manual_case` / `continue_manual_case` (runner with claim token, or a writable member on the web), `get_manual_pause_panel` (no queries). Pause closes the case's duration segment, Continue opens one; the case stays In progress and the run Started.
- `projects/models.py` + `0055_manual_pause_discuss`: `TestRunTestCase.manual_paused_at/_by/_label/_via` inside `MANUAL_CLAIM_FIELDS` / `MANUAL_CLAIM_CLEARED` (every claim-ending path ends the pause); `ManualCaseProgress` (runner's marks so far, fenced by claim token).
- `projects/manual_case_progress_services.py` (new): `record_manual_progress`, `load_manual_progress`.
- `projects/manual_run_policy.py`: `is_claimed_manual_case` (the one rule), pause/discuss refusal strings.
- `projects/manual_case_claim_services.py`, `manual_case_submit_services.py`: claim, release and hand-in refused (retryable) while a manual discussion turn is live, so the agent and the tester never share the machine.
- `projects/runner_manual_services.py`, `runner_manual_state_services.py`, `controller_consumer.py`, `runner_window_state.py`: pause/continue actions, `manual_case_progress` (own 60/min throttle), `manual_chat_send`, pause fields and `active.discussion` in the snapshot.
- `projects/runner_case_services.get_runner_cases`: manual runs no longer get the agent RUNNING role (removes a Pause button that always failed).
- `machine_setup` + `0011_manual_case_thread`: thread kind `manual_case` (`SetupConversation.manual_case`, one active per case), role `ThreadRole.MANUAL_CASE` (project data, KB, `get_manual_case`, `run_setup_task` only while the case is paused), `open_manual_chat`, send gates (own STARTED manual run exempt, no READY gate, refused once the claim ends), runner discussion read/send, thread header.
- Web: Manual test card Pause / Continue testing, Discuss dialog or Go to discussion, "Progress reported by the Test Runner"; run page "Paused · X" live.
- Test Runner 0.16.0: Pause / Continue banner (marks and captures disabled while paused, step timer stopped), Discuss section (chat extracted to `person_window_chat.py`), debounced progress flushed before pause and send.

## Why
Manual testers need to take a break mid-case without losing their marks or inflating the case duration, and to talk the case through with the AI while testing.

## Behaviour and contract changes
- Migrations `projects.0055_manual_pause_discuss`, `machine_setup.0011_manual_case_thread` (additive). Deploy: migrate, rebuild the server image, restart web/Daphne and `execution` workers, then update runners.
- Controller protocol 0.16.0, capability `manual_pause_discuss_v1`: `manual_case_action` `pause` / `continue` (claim token required), `manual_case_progress` (no reply), `manual_chat_send` (answered by `manual_case_action_result`); `manual_run_state.active` gains `paused`, `paused_by`, `paused_at`, `can_pause`, `can_continue`, `pause_blocker`, `discussion`; list status `paused`. Older runners get the new keys at defaults and keep working; their actions are refused without the capability.
- URLs `projects:test_run_case_manual_pause`, `projects:test_run_case_manual_continue` (POST), `machine_setup:manual_chat_open` (POST `text`).
- Socket: `test_run.pivot_status` and `test_run_case.status` gain `manual_paused`, `manual_paused_by`.

## Notes for future changes
- Paused time is excluded from `active_duration`, so from TestRail elapsed.
- No expiry of a pause; a writable member can still Release a stuck claim (refused while a discussion turn is live).
- The case page does not reload when a discussion turn ends: Continue stays shown disabled until a reload (the server check decides).
- `MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES` is asserted with `MANUAL_RUN_STATE_SAFE_BYTES` against the WebSocket and window IPC caps; about 0.5 MiB of IPC headroom is left.
- Not yet tried against a database, a live runner, Windows/Linux Tk or a browser.
