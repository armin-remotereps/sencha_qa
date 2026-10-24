# Failed and Blocked cases close the apps they left open

Date: 2026-10-24
Plan: docs/plan/fix-cleanup-after-failed-case.md

## What changed
- `projects/services.py`:
  - `execute_test_run_test_case`'s `finally` runs `_safe_teardown` before `_safe_cleanup`;
  - staleness is checked again after the teardown;
  - a `BaseException` (worker shutdown) skips the teardown;
  - `_machine_can_close_apps` checks runner connected + `close_app_v1` (one query) + a ready vision worker.
- `projects/case_teardown_policy.py` (new, pure): `needs_teardown` is true for FAILED, and for BLOCKED with `browser_unavailable`, `runner_outdated`, `resume_state_lost` or `internal_error`.
- `agents/services/case_teardown.py` (new): `run_case_teardown` runs a sub-agent limited to `TEARDOWN_TOOL_NAMES` (take_screenshot, close_app, close_tab, click, wait). It never raises.
- `agents/services/case_teardown_prompts.py` (new):
  - `pending_close_clauses` takes the planner's close clauses from steps without a passing final attempt;
  - `failed_attempt_digests`;
  - `build_teardown_sub_task`.
- `agents/services/orchestrator_prompts.py`: `CLOSE_CLAUSE_PREFIX`, shared by the planner wording and the extraction.
- `agents/services/tool_registry.py`: `tool_definitions_named`, `allowlisted_dispatcher` (refuses other tools).
- `agents/services/agent_loop.py` `_run_agent_loop` and `sub_agent.run_sub_agent(tool_names=)`: optional tool restriction. `sub_agent_prompts.build_sub_agent_system_prompt(tool_names=)` then describes only those tools.

## Why
A passing case closes its desktop apps and tabs through "Afterwards, close …" clauses on its last steps. A case that failed or was blocked never reached them, so its windows leaked into the next case and cluttered every screen parse.

## Behaviour and contract changes
- New setting `CASE_TEARDOWN_TIMEOUT_SECONDS` (default 120; 0 disables), in `example.env`.
- No teardown for:
  - Passed or Cancelled cases;
  - Blocked runner/visual engine/AI service/time limit cases;
  - a case with no plan;
  - an old runner;
  - no ready vision worker;
  - nothing to close. With nothing to close there is no LLM call and no query.
- The outcome, checkpoint, verdict, screenshots and TestRail payload are untouched. The teardown shows only as `[Teardown] …` lines in the case log.
- The run stays Started until the teardown finishes, so the next case or a Resume waits up to about the budget.
- No migration, URL or controller protocol change. Deploy: restart the execution workers.

## Notes for future changes
- Accepted residuals:
  - `click` is allowed and is not covered by the runner's close guards, so only the prompt stops a click on the runner's window;
  - the budget is checked between LLM calls, so one slow request can overrun it;
  - a clause from a step that never ran may name an app that was open before the case.
- A resumed Blocked case finds its apps closed; the restore sub-task reopens them.
- `pre-commit run --all-files` reports black on `projects/migrations/0022_blocked_reason_runner_outdated.py`. That predates this change (it is already unformatted on main).
- Not exercised against a live runner yet (plan Verification, manual steps 1-7).
