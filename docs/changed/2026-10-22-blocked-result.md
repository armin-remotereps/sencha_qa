# Blocked result for cases that could not be executed or judged

Date: 2026-10-22
Plan: docs/plan/feat-blocked-result.md

## What changed
- `projects/models.py`: `TestRunTestCaseStatus.BLOCKED`, `BlockedReason`, `TestRunTestCase.blocked_reason`.
- `agents/exceptions.py`, `projects/exceptions.py`: new error types; the moved errors are imported from their new homes, not re-exported.
  - New base `InfrastructureError`, with `AIServiceError` (+ `AIServiceErrorKind`), `VisualEngineError` and `TestRunnerDisconnectedError` / `TestRunnerUnresponsiveError` under it.
  - Moved into these modules: `ControllerActionError` (from `projects/services.py`), `OrchestratorPlanError` and `OrchestratorParseError` (from `orchestrator.py`).
- `projects/case_blocking.py` (new, pure): `classify_case_exception`, `blocked_message`.
- `agents/services/llm_client.py`: every provider fault raises `AIServiceError`. The message has no URL, headers or key, and no body for 401/403.
- AI-side exception flow:
  - `agent_loop` no longer swallows LLM errors.
  - `safe_tool_call` re-raises `InfrastructureError` and `SoftTimeLimitExceeded`.
  - The summarizers re-raise the soft limit.
  - The orchestrator falls back to a deterministic verdict summary when the AI summary call fails.
- `projects/services.py`, error classification:
  - The visual-engine error codes, OmniParser failure and OmniParser wait timeout raise `VisualEngineError`.
  - "Connection lost again" raises `TestRunnerDisconnectedError`.
  - `runner_local=True` reply timeouts raise `TestRunnerUnresponsiveError`.
- `projects/services.py`, case outcome:
  - `execute_test_run_test_case` blocks the case via `_settle_pivot_after_error` / `_mark_pivot_blocked` and no longer re-raises `Exception`.
  - Blocked counts appear in summaries and annotations (`blocked`, `blocked_count`) and in the narrative (titles capped at 10).
  - `rerun_cases(test_run, RerunScope)` and `can_rerun_cases` replace `rerun_failed_cases` and `can_rerun_failed_cases`.
- `projects/views.py`, `projects/urls.py`: `test_run_rerun_blocked`.
- `projects/consumers.py`: the case events carry `blocked_reason` and `blocked_reason_label`.
- `projects/testrail_mapping.py`, `testrail_results_services.py`: Blocked → TestRail status 2, with "<reason>: <message>" in the comment.
- `dashboard/services.py`:
  - new `RunSummary.blocked_7d` and `ActivityItem.blocked`;
  - new attention kind `blocked_run`, from the same query as `failed_run`.
- Templates and `punk-hazard.css`:
  - `.badge-blocked`, `.grid-5`, plus warning tones;
  - run badge order Failed > Blocked > Passed;
  - Blocked stat and Reason column on run detail;
  - "Why this case was blocked" card on case detail;
  - overview alert, and the dashboard stat, attention and activity rows.

## Why
QE opens a Jira ticket for every Failed case. Infrastructure faults (runner gone or unresponsive, visual engine, AI service, time limit, server bugs) were recorded as Failed, so they turned into false tickets. Failed now only comes from the agent's verdict.

## Behaviour and contract changes
- New case status `blocked` with a reason.
  - Pass rate is passed / (passed + failed) everywhere, including run detail, which used to divide by all cases.
- One infrastructure error no longer stops the Celery chain. The run goes on to the next case and reaches Done.
- Runner lost past the grace period: the current case is Blocked, the remaining cases are Cancelled, and the run is Cancelled.
  - Abort and Force disconnect still cancel. The Abort dialog now says so.
- Soft time limit: the case is Blocked `time_limit`. Before, the limit could be swallowed inside a tool.
- Reruns:
  - New URL `projects:test_run_rerun_blocked`: `POST /projects/<id>/test-runs/<run_id>/rerun-blocked/`.
  - Both "Re-run failed" and "Re-run blocked" copy the run's TestRail project, suite and run id.
- WebSocket:
  - `summary` gains `blocked`;
  - the case status and `current_state` events gain `blocked_reason` and `blocked_reason_label`.
- Migration `0018`, additive. Before rolling it back, run `UPDATE … SET status='failed' WHERE status='blocked'`.
- Controller protocol unchanged.

## Notes for future changes
- Still Failed (decided, see plan Non-goals and Q2):
  - runner-side `EXECUTION_FAILED` errors (UIPI, screenshot), because telling them apart needs new `ErrorCode`s;
  - timeouts of commands, launch_app and browser actions;
  - a sub-agent running out of its step budget.
- A Celery hard time-limit kill still leaves the case In progress; it needs its own fix plan.
- No LLM retry or backoff yet. An AI outage quickly blocks every remaining case.
- Any new broad `except Exception` on the case path must re-raise `InfrastructureError` and `SoftTimeLimitExceeded`.
- Blocked messages reach the UI and TestRail. Visual-engine and internal errors show fixed text, and the details go to the server log only.
- Verified with mypy, pre-commit and a page-render smoke check against the dev DB. No live case was run through each fault yet (plan Verification steps 2–10).
