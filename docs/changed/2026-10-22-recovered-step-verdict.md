# Recovered steps no longer fail the case

Date: 2026-10-22
Plan: docs/plan/fix-recovered-step-verdict.md

## What changed
- `agents/types.py`: new `SubTaskAttempt` (step_num, kind step/recovery, result); `OrchestratorResult.sub_task_results` renamed to `attempts`, plus `error`.
- `agents/services/orchestrator.py`: `_execute_sub_tasks` / `_attempt_recovery` record labelled attempts; `_build_verdict` passes only if every planned step has a final attempt and each passed; it also picks `error` (summary of the last failed step's final attempt), which `_to_agent_result` now just copies.
- `agents/services/orchestrator_prompts.py`: `final_step_indices` (the one "final attempt" rule) and `format_attempt_lines` (labels such as "superseded by retry", "Recovery for step N", "not run") shared by the LLM verdict prompt and `_deterministic_summary`; the prompt states the decided outcome and says not to contradict it.

## Why
A step that failed, recovered and passed on retry left its first failure in a flat result list, and the verdict required every entry to pass, so recovered cases were always Failed.

## Behaviour and contract changes
- Cases whose steps recover and then pass are now Passed (no Jira ticket); the summary still shows the recovery.
- Stop, failed recovery, max recovery, recovery without a task, continue past a failure and unrun steps stay Failed.
- `AgentResult.error` after a failed recovery is the failed step's summary, not the recovery's.

## Notes for future changes
- Unrun steps are assumed to be the plan's tail (steps run in order); skipping steps would need `final_step_indices` callers and the `len(finals) == planned_steps` guard revisited together.
- Pre-existing, left alone: a failed recovery's iterations are not added to `total_iterations`; a plan whose items are all non-dicts yields zero sub-tasks and a vacuous pass.
