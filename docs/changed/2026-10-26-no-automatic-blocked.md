# Only a person marks a case Blocked; faults ask for help

Date: 2026-10-26
Plan: docs/plan/refactor-no-automatic-blocked.md

## What changed
- `projects/services.py`: `execute_test_run_test_case` sends every fault through the new `_pause_case_after_fault` → `_pause_run_for_help(fault=, continue_mode=)`.
  - The faults covered: runner lost past grace, infrastructure errors, soft time limit, internal error, worker shutdown (as `internal_error`).
  - `_mark_pivot_blocked`, `_settle_pivot_after_error` and `_abort_run_after_reconnect_timeout` are deleted.
  - A fault during the verdict names the last step.
- `projects/case_faults.py` (replaces `case_blocking.py`):
  - `classify_case_fault`, `fault_message` and `fault_ask` (fixed server text per fault);
  - `help_continue_mode(fault, checkpoint, restore_pending=)`.
- `projects/models.py`:
  - new `CaseHelpRequest.fault` (reuses `BlockedReason`);
  - new `CaseHelpRequest.continue_mode` and `HelpContinueMode` (`current_screen` / `restore` / `from_scratch`);
  - label changes: `help_skipped` → "Marked blocked by a person", Skip → "Mark blocked".
- `projects/case_help_services.py`:
  - `skip_after_help` → `mark_blocked_after_help`, which is now the only writer of Blocked and always records `help_skipped`;
  - `_write_help_key` follows `continue_mode`;
  - `HelpPanel.continue_mode`.
- `projects/case_teardown_policy.py`: only Failed cases get a teardown.
- `agents/services/orchestrator*.py`: `CheckpointHelp.fault`. A fault that interrupted a step before any attempt was recorded still gets its help line (`_interrupted_by_fault`). The help line and verdict note name the fault.
- `machine_setup`:
  - the auto-fix brief and person brief name the fault and the continue mode;
  - `_build_auto_fix_thread` gains an INFRASTRUCTURE FAULTS section. The agent decides itself what it can fix, with no static list, and must never restart the Test Runner;
  - the help payload gains `fault_label` and `continue_mode`.
- Templates (case help card, help thread panel and `chat.html`, run banner and Reason column, case page blocked card): fault badge, "Mark blocked", and Continue hints per mode.

## Why
The agent or the worker could put a case in Blocked on its own whenever infrastructure failed. Now a stopped case always asks for help (in Night mode the auto-fix agent tries first), and only a person who decides it cannot be fixed now marks it Blocked.

## Behaviour and contract changes
- URL: `projects:test_run_case_help_skip` (`help/skip/`) is replaced by `projects:test_run_case_help_block` (`help/block/`). There is no redirect.
- A runner lost past its grace period now pauses the run (the remaining cases stay Not started) instead of cancelling it.
- A visual-engine or AI blip now pauses the whole run until someone, or the Night auto-fix, responds. There are no retries.
- How Continue goes on:
  - `restore` (cleanup and restore sub-task first) applies to runner disconnected or unresponsive, and to any fault that hit before a due restore finished.
  - `from_scratch` applies when no checkpoint was saved: the checkpoint is cleared, so the note does not reach the agent.
  - `current_screen` applies otherwise.
- Migration `projects.0031_help_fault` is additive: two fields plus label-only `AlterField`s. Existing Blocked rows, Resume run, Re-run blocked, TestRail (status 2) and the counts are unchanged.
- The setup socket `help` payload gains `fault_label` and `continue_mode`. There is no controller protocol or settings change.
- Deploy: run `migrate`, then restart Daphne/web and every Celery worker.

## Notes for future changes
- `grep "status = TestRunTestCaseStatus.BLOCKED"` must keep exactly one hit, in `mark_blocked_after_help`. The other `BlockedReason` values now only classify `CaseHelpRequest.fault`, plus older Blocked rows.
- `continue_mode` is decided once, at pause time. Requests the agent opened, and older rows, are `current_screen`.
- Accepted residual: after a fault during the verdict, the help line and verdict note say the last step "was interrupted" and "retried", although only the verdict was.
- Verified with pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, template compilation, and three reviewer and verifier passes. Not yet exercised against a live runner, a vision worker or the real model; the plan's Verification lists manual steps 1–12.
