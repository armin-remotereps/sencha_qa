# fix: Case verdict ignores a step's superseded failure after a successful recovery

Status: Done
Created: 2026-10-22
Request: When a planned sub-task fails, the orchestrator can run a recovery sub-task
and then retry the step. Today every attempt (the failed first try, the recovery, the
retry) goes into one flat list, and the case passes only if every entry passed. A case
whose step failed once, recovered and then passed on retry is therefore reported
Failed. The fix bases the verdict on each planned step's final attempt. Recovery
results stay in the summary as context but do not count toward pass/fail. The error
comes from the final attempt of a failed planned step. Stop, failed recovery, max
recovery reached and "continue" past a failed step must stay FAIL. The change stays
inside `agents/`.

## Context
- `agents/services/orchestrator.py`
  - `_execute_sub_tasks` (l.205-303) appends every planned-step attempt to `results`
    (l.250). The recovery path re-enters the loop with the same `i` (`continue`,
    l.297), so the retry is appended as a second entry.
  - `_attempt_recovery` (l.306-365) appends the recovery result to the same list
    (l.352).
  - `_build_verdict` (l.368-397) uses `all(r.status == "pass" for r in results)`,
    which is the bug.
  - `_deterministic_summary` (l.400-411) labels list entries "Step N" by list index.
  - `_to_agent_result` (l.466-485) uses `failed[-1].summary` as `AgentResult.error`,
    which may be a superseded failure.
- `agents/services/orchestrator_prompts.py`: `build_verdict_prompt` (l.128-139)
  numbers results 1..n without labels and does not tell the LLM the decided status.
- `agents/types.py`: `SubTaskResult`, `OrchestratorResult(sub_task_results=...)`.
- Default `ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS=1` (`auto_tester/settings.py:311`), so
  a step has at most two attempts by default.

### Consumers outside the orchestrator (checked)
- `OrchestratorResult` and `sub_task_results` are used only inside
  `agents/services/orchestrator.py`. Nothing else imports them.
- `AgentResult` from `run_orchestrator` is consumed only by
  `projects/services.py::_finalize_pivot` (l.2654):
  - status comes from `stop_reason` (`TASK_COMPLETE` gives SUCCESS, anything else
    gives FAILED);
  - `pivot.result` comes from `_extract_agent_summary`, which returns the assistant
    summary message and falls back to `error` only if there is no assistant string
    message. The orchestrator always adds one, so `error` is effectively unused
    downstream today.
- Blocked vs Failed (`docs/changed/2026-10-22-blocked-result.md`) is decided by
  exception type in `execute_test_run_test_case` / `_settle_pivot_after_error`. It is
  not decided from `AgentResult`. This fix raises no new exceptions and changes no
  exception flow, so Blocked classification is unaffected. The `AIServiceError`
  fallback in `_build_verdict` stays as it is.

## Root cause
Confirmed by reading the code. The pass/fail aggregate runs over every attempt,
including superseded first attempts and recovery sub-tasks, instead of over the final
attempt of each planned step. No step identity is recorded with a result, so
`_build_verdict`, `_deterministic_summary`, `build_verdict_prompt` and
`_to_agent_result` cannot tell a superseded attempt or a recovery from a final step
result.

## Goals
- Verdict PASS if and only if every planned step has a final attempt and each final
  attempt passed.
- Recovery sub-task results never affect pass/fail.
- `AgentResult.error` is the summary of the final attempt of the last failed planned
  step (same field as today, `summary`).
- The LLM verdict summary and the deterministic summary label each entry (step
  number, attempt number, recovery, superseded) and state the decided status, so the
  wording cannot contradict the status.

## Non-goals
- Changing recovery or retry policy, `ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS`, or the
  evaluate prompt.
- Changing `_finalize_pivot`, Blocked classification, TestRail mapping or the UI.
- Persisting per-step results.

## Design
### Data model
No DB changes. In `agents/types.py`:
- New frozen dataclass `SubTaskAttempt`:
  - `step_num: int`: the 1-based planned step this attempt belongs to. A recovery
    carries the number of the step it recovers.
  - `kind: Literal["step", "recovery"]`
  - `result: SubTaskResult`
- `OrchestratorResult`:
  - `sub_task_results: tuple[SubTaskResult, ...]` becomes
    `attempts: tuple[SubTaskAttempt, ...]`. The rename is safe because the only users
    are in `orchestrator.py`.
  - Add `error: str | None`, decided in `_build_verdict`, so `_to_agent_result`
    holds no verdict logic.
- `SubTaskResult` is unchanged. It stays the sub-agent's output and knows nothing
  about orchestration.

### Services (`agents/services/orchestrator.py`)
- `_execute_sub_tasks`:
  - keeps a `list[SubTaskAttempt]`;
  - wraps each planned-step result as `kind="step"` with `step_num`;
  - passes `planned_steps=len(sub_tasks)` to `_build_verdict`.
  - Loop control is unchanged.
- `_attempt_recovery`: its `results` parameter becomes `list[SubTaskAttempt]`, and it
  appends `kind="recovery"` with the recovered `step_num`. Nothing else changes.
- New private helper `_final_step_results(attempts) -> dict[int, SubTaskResult]`:
  - takes the last `kind="step"` attempt per `step_num`;
  - is pure, has a single source of truth (the attempt list), and needs no parallel
    dict to keep in sync.
- `_build_verdict(orchestrator_llm, attempts, planned_steps, total_iterations)`:
  - Status is `pass` iff `len(finals) == planned_steps` and all finals pass. The
    length check is a guard: the current loop only exits early via `break` after a
    failed final attempt, but the guard makes "unrun steps imply FAIL" explicit
    rather than incidental.
  - `error` is the `summary` of the highest-numbered failed final step, else `None`.
  - Passes attempts, status and planned step count to `build_verdict_prompt`, and
    the same to `_deterministic_summary`.
- `_deterministic_summary`: renders the same labelled lines as the prompt, plus
  "Steps X-Y not run" when steps never ran.
- `_to_agent_result`: reads `orchestrator_result.error` and no longer filters
  results.
- Keep one shared line formatter for prompt and deterministic summary, so the two do
  not drift. Put it in `orchestrator_prompts.py` as `format_attempt_lines(...)` and
  import it in `orchestrator.py`.

### Prompt (`agents/services/orchestrator_prompts.py`)
`build_verdict_prompt(attempts, overall_status, planned_steps)` lists entries in
execution order with labels such as:
- `Step 2 (attempt 1, superseded by retry) [FAIL] ...`
- `Recovery for step 2 [PASS] ...`
- `Step 2 (attempt 2, final) [PASS] ...`

It appends "Steps N-M were not run" when applicable, and replaces "State whether
the test case PASSED or FAILED overall" with an instruction that names the decided
result. For example: "The overall result is PASSED. It is decided from the final
attempt of each step, and recovery steps do not count. Do not contradict it." The
attempt number is the count of `kind="step"` attempts for that `step_num` so far. An
attempt is "final" when it is the last step attempt for its `step_num`.

### Pages and forms
None.

### Background work and WebSockets
None. Log lines are unchanged.

### Settings / infrastructure
None.

### Data flow
1. The planned step runs and its attempt is recorded.
2. On failure, the evaluate decision is one of:
   - continue: no retry, so the fail is that step's final attempt;
   - recover: a recovery attempt is recorded, then the same step runs again and a
     new step attempt is recorded;
   - stop or max reached: break.
3. `_build_verdict` reduces the attempts to the final attempt per step and decides
   status and error.
4. The prompt or the deterministic text describes all attempts with labels.
5. `_to_agent_result` maps status to `stop_reason` and copies `error`.

### Required FAIL paths
| Path | Final attempt of the failing step | Verdict |
|---|---|---|
| stop | first (failed) attempt, then break | FAIL |
| recovery failed | the failed step attempt (recovery does not count), then break | FAIL |
| max recovery reached | the latest failed retry, then break | FAIL |
| recovery requested without a task | failed attempt, then break | FAIL |
| continue past a failed step | failed attempt, loop advances | FAIL |
| break leaves later steps unrun | stopping step failed, and `len(finals) < planned_steps` | FAIL (two independent reasons) |
| recover, then retry passes | the retry passed | PASS (the fix) |

`AgentCancelledError` and `InfrastructureError` still propagate out of the loop as
today, and no verdict is built.

## Alternatives considered
- **Add `kind`/`step_num` to `SubTaskResult`**: rejected. It mixes sub-agent output
  with orchestration bookkeeping, and `sub_agent.py` would have to fill in fields it
  cannot know.
- **Keep a separate `dict[int, SubTaskResult]` of finals next to the flat list**:
  rejected. There would be two sources of truth to keep in sync in two functions.
  Deriving the finals from the labelled attempts is equally cheap.
- **Drop superseded attempts from the summary entirely**: rejected. The user wants
  recovery context kept, and seeing the original failure helps QE triage flaky steps.

## Risks and mitigations
- **LLM still mislabels the outcome**: the decided status is now stated explicitly
  in the prompt. Status itself never comes from the LLM, so the worst case is
  wording, as today.
- **More PASS outcomes**: cases that recovered now pass, and so produce no Jira
  ticket. This is the intended change. The summary still shows that a recovery
  happened.
- **Rename of `sub_task_results`**: all uses are in one file, and mypy strict catches
  any missed one.

## Implementation steps
One reviewable change, small enough for a single implementer with no sub-agents
(the files depend on each other line by line).
1. Add `SubTaskAttempt`, rename `OrchestratorResult.sub_task_results` to `attempts`,
   and add `OrchestratorResult.error` (files: `agents/types.py`).
2. Add `format_attempt_lines` and change the `build_verdict_prompt` signature and
   text (files: `agents/services/orchestrator_prompts.py`).
3. Wire the attempts through `_execute_sub_tasks` and `_attempt_recovery`. Add
   `_final_step_results`, rewrite the `_build_verdict` status and error logic, update
   `_deterministic_summary`, and simplify `_to_agent_result` (files:
   `agents/services/orchestrator.py`).
4. Run `pre-commit run --all-files` (mypy strict, black, isort).

## Verification
- `mypy .` and `pre-commit run --all-files` are clean.
- Read-only reasoning walk-through of the FAIL-path table above against the final
  code.
- Optional throwaway check, not committed: in `python manage.py shell -c`, build
  `SubTaskAttempt` lists for each table row and call `_final_step_results` plus the
  status rule. With the LLM call mocked out or the deterministic path forced, also
  `_build_verdict`. Confirm the PASS/FAIL, `error` and labelled text.
- Live smoke check: run a case on a test machine where step 1 is set up to fail once
  (for example, the app is not open), with the evaluate step choosing recover. Then
  confirm:
  - the case shows Passed;
  - the summary mentions the recovery;
  - there is no "PASSED" summary on a Failed case.
- Run a case that hits stop and confirm that it stays Failed and that the summary
  names the failing step.
- Run `reviewer` and `verifier`.

## Open questions
Resolved 2026-10-22: the user confirmed all three assumptions (Q1 yes, Q2 `summary`, Q3 full text labelled).

- Q1: Should the verdict prompt tell the LLM the decided status explicitly and tell
  it not to contradict it? (Assumption used in this draft: yes.)
- Q2: For `AgentResult.error`, should it use the failed final attempt's `summary`
  (as today) or its `error` field (the raw agent error, often `None`)? (Assumption
  used in this draft: `summary`. Downstream `error` is only a fallback that is
  effectively never used.)
- Q3: Should superseded attempts appear in the summary with their full text, or only
  as a one-line "failed first, recovered" note? (Assumption used in this draft: full
  text, labelled "superseded by retry".)

## Changelog
- 2026-10-22: created
- 2026-10-22: open questions resolved (all draft assumptions confirmed); implementation started
- 2026-10-22: review pass 1. Fixed: the "final attempt per step" rule now lives once in
  `final_step_indices` (orchestrator_prompts.py) and drives both the verdict and the
  labels; added a comment stating that unrun steps are the plan's tail. Not changed:
  a failed recovery's iterations are not added to `total_iterations` (pre-existing,
  out of scope); an all-non-dict plan yields zero sub-tasks and a vacuous PASS
  (pre-existing, same as old `all([])`, out of scope); "Steps N-M: not run" wording
  instead of the plan's "were not run" (cosmetic).
- 2026-10-22: review pass 2. Fixed: verdict prompt opener no longer claims all
  sub-tasks ran ("Sub-task execution finished."), since stopped runs list unrun
  steps. Not changed: moving `final_step_indices` out of the prompts module into
  `agents/types.py` or its own module (reviewer marked it optional; one rule, two
  callers in the same package).
- 2026-10-22: review pass 3 clean (reviewer + verifier); documented in docs/changed/2026-10-22-recovered-step-verdict.md
