# fix: Close the apps and tabs a Failed or Blocked case left open

Status: Done
Created: 2026-10-24
Request: The user asked: "if a test failed or blocked, clean it up like when it
pass, clean the tab or apps, ...". A passing case closes the desktop apps and
terminal tabs it opened, because the planner adds a housekeeping clause ("Afterwards,
close <App> with close_app ...") to the last sub-task that uses each app. A case
that fails, stops, has a failed recovery, hits max recovery, times out on its last
step, or is Blocked by an exception never reaches those clauses. Its windows then
leak into the next case and clutter every OmniParser parse. The fix adds a
best-effort, bounded teardown for Failed cases and for Blocked cases whose machine
is still usable. The teardown closes what the case opened and leaves the case
outcome untouched. It lands in the same implementation cycle as
`docs/plan/fix-failed-attempt-action-digest.md` and reuses its digest.

## Context
- `projects/services.py` `execute_test_run_test_case`: its `finally` (when the
  execution is not stale) calls `_safe_cleanup(project.id, wait_for_runner=False)`
  for every outcome, then `_update_test_run_status_if_needed`. `_safe_cleanup` calls
  `controller_cleanup_environment`, which runs `controller_client/cleanup.py`
  `execute_cleanup` on the runner. That function:
  - closes the Playwright browser;
  - terminates interactive sessions;
  - kills the PIDs in `ProcessTracker` (only processes the runner launched itself);
  - clears the cleanup folder, last, "so processes that held files in the folder
    are already gone".

  Confirmed: GUI-opened apps (Dock, Start menu, clicks, `launch_app` through the OS)
  and terminal tabs are not covered.
- `agents/services/orchestrator_prompts.py` `build_plan_system_prompt`: the
  "CLOSING APPS" rule puts the close clause at the END of the description of the
  LAST sub-task that uses the app. `agents/services/prompt_parts.py`
  `build_app_lifecycle_rules` has the sub-agent close only after verifying the
  expected result. A failed step never closes, and later steps never run.
- How a case ends:
  - `_finalize_pivot` sets SUCCESS, FAILED or CANCELLED from `AgentResult`.
  - Exceptions escape `run_orchestrator`. `_settle_pivot_after_error` maps them
    through `projects/case_blocking.py` `classify_case_exception` to a
    `BlockedReason`.
  - `ControllerReconnectTimeoutError` goes to `_abort_run_after_reconnect_timeout`.
  - `AgentCancelledError` and `ControllerNotConnectedError` cancel the case.
- Checkpoint: `projects/case_checkpoint_services.py` `build_checkpoint_callback`
  writes each checkpoint generation-filtered and also sets
  `pivot.execution_checkpoint` in memory. So after `run_orchestrator` returns or
  raises, `load_resume_checkpoint(pivot)` yields the latest plan and attempts with
  no extra query. The case page step rows read only the checkpoint.
- `close_app`/`close_tab` (`agents/services/tools_controller.py`,
  `projects/services.py` `controller_close_app`/`controller_close_tab`):
  - optional capability `CLOSE_APP_V1`, checked with
    `projects/runner_capability_services.runner_supports`;
  - fail closed on the runner: they never close the runner, its terminal or the OS
    shell, and `close_tab` refuses unless the front tab is provably not the
    runner's.
- `run_sub_agent` (`agents/services/sub_agent.py`) always offers every tool
  (`_run_agent_loop` uses `get_all_tool_definitions()` and `dispatch_tool_call`).
  `run_tool_loop` already accepts `tool_definitions` and `dispatch`.
- Case task limits: `projects/tasks.py` `execute_test_run_case` has a soft limit of
  `1800 + outage` and a hard limit 60 s later. `CLEANUP_TIMEOUT_SECONDS` is 30.
- Resume (`docs/changed/2026-10-22-resume-blocked-case.md`):
  - a resumed case runs a restore sub-task that re-establishes the earlier steps'
    state "the machine may have been restarted or changed";
  - the cleanup folder is already emptied on Blocked;
  - every write must respect `execution_generation`.

## Goals
- When a case ends **Failed**, or **Blocked** with a still-usable machine, close the
  desktop apps and tabs the case opened whose housekeeping never ran.
- The outcome stays untouched:
  - no `SubTaskAttempt`;
  - no checkpoint write;
  - no change to status, `blocked_reason`, `result` or the TestRail payload;
  - nothing raises out of the teardown.
- Bounded: one setting caps the time; it is skipped when there is nothing to close.
- It respects abort, stale generation and the close_app/close_tab safety rules.

## Non-goals
- **Passed cases.** A passed step whose own close failed already says so in its
  summary, and the rule says housekeeping never fails a step. Not worth a second
  agent run on every pass.
- **Cancelled (Abort) cases.** The user stopped the run on purpose. The
  cancellation check would also stop a teardown immediately.
- **Blocked cases without a usable machine** (see the policy below).
- **Apps open before the case started, or opened with no close clause and no
  recorded `launch_app`.** The teardown only closes what it has evidence the case
  opened.
- **No controller protocol change**: the existing `close_app`/`close_tab` actions
  are used.
- No model, migration, URL, template or WebSocket change.

## Design

### Mechanism: options compared
1. **Deterministic, server-only.** Regex the app names out of the close clauses and
   call `controller_close_app`/`controller_close_tab` directly, with no LLM.
   Rejected as the sole mechanism:
   - The clauses are free-form planner text.
   - `close_tab` closes the app's *front* tab, so without first clicking the tab
     the case opened, it could close the user's or another case's tab.
   - A save or confirm dialog makes a plain `close_app` fail, so it would need
     `force=true` blindly.
   - It cannot tell whether a step that never ran had opened the app at all. It
     would close a pre-existing instance of an app the case meant to open later.
2. **Teardown sub-agent only.** Flexible, but it would spend an LLM run on every
   Failed case, including web-only cases that have nothing to close.
3. **Chosen: a deterministic gate plus a restricted teardown sub-agent.**
   - **Gate.** The server collects the close clauses of the steps that never
     completed, plus the failed attempts' action digests (from the digest plan).
     When there are no clauses and no digest shows a `launch_app(` call, no agent
     runs. This covers web-only cases and cases whose apps were all closed by
     passed steps.
   - **Agent.** It closes the targets on a short budget with an enforced tool
     allowlist, and it can look at the screen and dismiss dialogs.

### Data model
None. Nothing is persisted: the teardown is never an attempt and never touches the
checkpoint, so resume and the step progress rows are unaffected.

### Agents layer
- **`agents/services/orchestrator_prompts.py`** (meets the digest plan, which adds
  `format_failed_attempt_state` and the evaluate block here):
  - `CLOSE_CLAUSE_PREFIX: Final = "Afterwards, close"`. The planner's "CLOSING
    APPS" rule is rewritten to interpolate this constant for both the close_app
    and the close_tab sentence, so the wording the planner emits and the wording
    the extractor matches cannot drift apart. The rule text is otherwise
    unchanged.
  - `pending_close_clauses(plan: Sequence[SubTask], attempts: Sequence[SubTaskAttempt]) -> tuple[str, ...]`
    is pure. For each planned step without a *passing final attempt* (it uses the
    existing `final_step_indices`, so unrun steps are included), it returns the
    description text from `CLOSE_CLAUSE_PREFIX` (case-insensitive) to the end,
    with whitespace collapsed and capped at 300 chars. Steps without the prefix
    contribute nothing.
  - `failed_attempt_digests(attempts: Sequence[SubTaskAttempt], *, limit: int = 3) -> tuple[str, ...]`
    is pure. It returns the non-empty `result.actions_digest` of the last `limit`
    attempts with `status == "fail"` (step and recovery kinds), oldest first.
  - `build_teardown_sub_task(close_clauses: Sequence[str], digests: Sequence[str], timeout_seconds: int) -> SubTask`
    writes the description and expected result:
    - The test case is already decided. Do not test, judge or redo anything; only
      close what this case left open.
    - Targets: the listed close instructions. The action logs show what the case's
      failed attempts actually opened (`launch_app`, clicks).
    - Take one screenshot first. Close only apps and tabs that are listed or that
      the logs show the case opened, and that are still open. Skip an app the
      case's steps never reached.
    - Tools: `close_app` for apps, and `close_tab` after clicking the case's tab to
      bring it to the front. `force=true` is allowed because unsaved work from a
      finished case may be discarded. Use `click` only to focus a tab or answer a
      save/quit dialog with "Don't save" / "Discard".
    - Never close the Test Runner, its terminal, the OS shell, the Playwright
      browser (closed automatically) or anything not listed.
    - Expected result: "The listed applications and tabs opened by this case are
      closed; nothing else was closed." The step's `RESULT:` is only logged.
- **`agents/services/tool_registry.py`**:
  - `tool_definitions_named(names: frozenset[str]) -> tuple[ToolDefinition, ...]`
    filters `get_all_tool_definitions()`.
  - `allowlisted_dispatcher(names: frozenset[str]) -> ToolDispatcher` wraps
    `dispatch_tool_call` and returns an error `ToolResult` ("Tool not available in
    this task") for any other tool name. This is defence in depth, beyond the
    definitions the model sees.
- **`agents/services/agent_loop.py` `_run_agent_loop`**: gets two new keyword
  parameters, `tool_definitions: tuple[ToolDefinition, ...] | None = None` and
  `dispatch: ToolDispatcher | None = None`. `None` keeps today's defaults.
- **`agents/services/sub_agent.py` `run_sub_agent`**: gets a new keyword parameter,
  `tool_names: frozenset[str] | None = None`. When it is set, the call goes through
  `tool_definitions_named` and `allowlisted_dispatcher`. Default behaviour is
  unchanged.

  This file meets the digest plan: that plan changes `_parse_sub_task_result`, this
  one changes `run_sub_agent`'s signature. Apply them in order.
- **New `agents/services/case_teardown.py`**, the "how":
  - `TEARDOWN_TOOL_NAMES: Final = frozenset({"take_screenshot", "close_app", "close_tab", "click", "wait"})`.
    These are excluded on purpose:
    - `key_press`: quit shortcuts hit whatever window has focus and can quit the
      runner (the `build_app_lifecycle_rules` warning).
    - typing, shell, launch and browser tools: they are test actions.
  - `_TEARDOWN_MAX_ITERATIONS: Final = 30` is a module constant.
  - `run_case_teardown(checkpoint: OrchestratorCheckpoint, project_id: int, *, timeout_seconds: int, on_log: LogCallback | None, cancellation_check: CancellationCheck | None, system_info: dict[str, object] | None, project_prompt: str | None) -> None`
    runs these steps:
    1. Compute `pending_close_clauses` and `failed_attempt_digests`. If there are no
       clauses and no digest contains `launch_app(`, log
       `[Teardown] Nothing left open to close` and return.
    2. Log `[Teardown] Closing what this case left open (budget Ns): <clauses>`.
    3. Build an `AgentConfig`:
       - `llm` and `vision_llm` from `build_llm_config()`;
       - `max_iterations=_TEARDOWN_MAX_ITERATIONS` and
         `timeout_seconds=timeout_seconds`;
       - the injected `on_log` and `cancellation_check`;
       - `on_screenshot=None`, so teardown screenshots are not case evidence.
    4. Call `run_sub_agent(build_teardown_sub_task(...), "", project_id, config=...,
       browser_selection=BrowserSelection(current=checkpoint.browser),
       tool_names=TEARDOWN_TOOL_NAMES, system_info=..., project_prompt=...)`.
    5. Log `[Teardown] Result: <status> — <summary>`.
    6. **Never raises an `Exception`.** `AgentCancelledError` logs
       `[Teardown] Stopped: the run was aborted or taken over`.
       `ControllerReconnectTimeoutError`, `AIServiceError`,
       `SoftTimeLimitExceeded`, `InfrastructureError` and any other `Exception`
       are logged as `[Teardown] Skipped: <type name>`, plus `logger.warning`
       with `exc_info`. `BaseException` (worker shutdown) propagates.

  Not in `orchestrator.py` (see Placement). `orchestrator.py` is **not changed by
  this plan**. Its only touch point is indirect: the teardown reads the attempts
  and `actions_digest` that the orchestrator records and checkpoints.

### Projects layer: the "when"
- **New pure module `projects/case_teardown_policy.py`**, no ORM, like
  `case_blocking.py`:
  - `TEARDOWN_BLOCKED_REASONS: Final = frozenset({BlockedReason.BROWSER_UNAVAILABLE, BlockedReason.RUNNER_OUTDATED, BlockedReason.RESUME_STATE_LOST, BlockedReason.INTERNAL_ERROR})`.
    These leave the runner, the visual engine and the AI service working.
  - `needs_teardown(status: str, blocked_reason: str | None) -> bool` is True for
    `FAILED`, and for `BLOCKED` with a reason in the set above.
  - Skipped, with the reasons recorded in the module docstring:
    - `RUNNER_DISCONNECTED` and `RUNNER_UNRESPONSIVE`: no runner.
    - `VISUAL_ENGINE`: no screen understanding.
    - `AI_SERVICE`: no LLM.
    - `TIME_LIMIT`: the hard kill is 60 s away and must go to `_safe_cleanup`.
    - `CANCELLED` and `SUCCESS`.
- **`projects/services.py`**:
  - A new private helper `_safe_teardown(pivot, project, generation,
    cancellation_check)` sits next to `_safe_cleanup`. It belongs to the existing
    case-execution use case in this legacy module, not to a new use case. It cannot
    live in a new `projects/*_services.py` module, because importing
    `agents.services` at module level there would close the
    `agents.services` -> `projects.services` cycle.
  - It returns without doing anything when any of these holds:
    - `settings.CASE_TEARDOWN_TIMEOUT_SECONDS <= 0` (kill switch);
    - `needs_teardown(...)` is false, after
      `pivot.refresh_from_db(fields=["status", "blocked_reason"])`;
    - `load_resume_checkpoint(pivot)` is `None` (planning never finished);
    - the project is not `agent_connected`, after a refresh of that one field;
    - `runner_supports(project.id, ClientCapability.CLOSE_APP_V1)` is false;
    - `list_ready_workers()` is empty.

    Each skip is logged at info level in the server log only, so the case log is
    not cluttered.
  - Otherwise it calls `run_case_teardown(...)` with the generation-filtered
    `_build_log_callback(pivot, generation)` and the case's `cancellation_check`.
    It wraps the call in `try/except Exception` as a second guard.
  - **`execute_test_run_test_case`**:
    - Import `run_case_teardown` at the existing local import site next to
      `run_orchestrator`. That site is the existing, commented cycle-break
      exception to the top-level-import rule. Move the import to the top of the
      `try` so the `finally` can use it, or import both in the `finally` path.
      Pick whichever keeps the import single.
    - Add a local `allow_teardown = True` and set it `False` in the `except
      BaseException` branch, so a worker shutdown never starts an agent.
    - The `finally` order, still under `if not _is_stale_execution(...)`:
      1. `if allow_teardown: _safe_teardown(...)`
      2. `_safe_cleanup(...)`
      3. `_update_test_run_status_if_needed(...)`

      Teardown goes before cleanup so that apps holding files in the cleanup
      folder are closed before the folder is cleared, matching `execute_cleanup`'s
      own ordering rationale.

### Pages and forms
None. The case page shows the final status as soon as it is written. The teardown
appears only as `[Teardown] ...` lines and the sub-agent's tool lines in the live
case log.

### Background work and WebSockets
Runs inside the existing `execute_test_run_case` task (`execution` queue). It adds
no new task, event or controller action. The run stays STARTED until the teardown
and cleanup finish, so the next chained case and any Resume wait for it (the
Resume gate requires the run to be Done or Cancelled).

### Settings / infrastructure
- `CASE_TEARDOWN_TIMEOUT_SECONDS: int` (default `120`) is read in
  `auto_tester/settings.py` with `config(..., cast=int)` and added to `example.env`
  with a comment: "Budget for closing apps a Failed/Blocked case left open; 0
  disables".
- Deploy: restart the execution workers.

### Placement: orchestrator or projects service
- **Orchestrator (`try/except/finally` in `run_orchestrator`).** Rejected:
  - Blocked exceptions would need the orchestrator to know which ones leave a
    usable machine. That is `BlockedReason` policy, which lives in `projects`.
  - It would delay writing the verdict and status until after the teardown.
  - Wrapping exceptions there risks changing what escapes.
- **Projects service, after the status is written (chosen).**
  - It knows the final status and Blocked reason.
  - It already owns the generation and stale rules and the cleanup ordering.
  - The latest checkpoint is in memory on `pivot`.
  - The agents-side teardown stays a pure "how", given a checkpoint.

### Resume interaction (decided: tear down Blocked cases too)
- A Blocked case is resumable. Closing its apps means the restore sub-task reopens
  them. That is acceptable and consistent:
  - the restore sub-task is already written for a machine that "may have been
    restarted or changed by a person";
  - on Blocked, `_safe_cleanup` already closes the browser, kills runner-launched
    processes and empties the cleanup folder;
  - a person usually fixes the infrastructure between Block and Resume anyway.
- The teardown writes no checkpoint, so the resume cursor, attempts and step
  options are unchanged.

### Data flow
1. `run_orchestrator` returns a fail (or raises).
2. `_finalize_pivot` (or `_settle_pivot_after_error`) writes FAILED or BLOCKED, and
   the UI updates.
3. `finally` runs, and the execution is not stale.
4. `_safe_teardown` checks the policy, the kill switch, the checkpoint, the runner
   connection, CLOSE_APP_V1 and a ready worker.
5. `run_case_teardown` computes the pending close clauses and failed digests. With
   no targets it stops here.
6. Otherwise a restricted sub-agent takes a screenshot, then focuses and closes the
   tabs and apps, logging `[Teardown]` lines through the generation-filtered log.
7. `_safe_cleanup` then closes the browser, kills processes and empties the folder.
8. `_update_test_run_status_if_needed` runs.

## How this meets the action-digest plan
- `agents/types.py`: the teardown reads `SubTaskResult.actions_digest` (added by
  the digest plan). No further change.
- `agents/services/sub_agent.py`: the digest plan changes `_parse_sub_task_result`,
  this plan adds `tool_names` to `run_sub_agent`. Same file, so one implementer or
  in sequence.
- `agents/services/orchestrator_prompts.py`:
  - the digest plan adds `format_failed_attempt_state` and the evaluate block;
  - this plan adds `CLOSE_CLAUSE_PREFIX` (and its use in the planning prompt),
    `pending_close_clauses`, `failed_attempt_digests` and
    `build_teardown_sub_task`.

  These are disjoint functions in one file, so implement in sequence.
- `agents/services/orchestrator.py`: changed only by the digest plan. The teardown
  only consumes the attempts it checkpoints.
- Implement the digest plan first: `failed_attempt_digests` is meaningless without
  `actions_digest`.

## Risks and mitigations
- **Closing something the case did not open** (a user's window, the app under
  test kept open by the project prompt). Mitigations:
  - targets come only from the plan's close clauses and the recorded `launch_app`
    calls;
  - the prompt says to skip apps the case never reached;
  - the runner's fail-closed rules protect the runner, its terminal and the shell;
  - `key_press` is excluded.

  Residual: a step that never ran had a clause for an app that was already running
  before the case. The agent is told to check the logs and skip it, but this
  relies on its judgement. It is accepted and named in the change note.
- **Time.** Up to `CASE_TEARDOWN_TIMEOUT_SECONDS` plus one LLM round per Failed
  case that has targets. A soft time limit inside the teardown is caught, and
  `_safe_cleanup` (30 s) still fits in the 60 s before the hard kill.
- **Outcome tampering.** None:
  - no status or result writes;
  - the teardown result only goes to the log;
  - `on_screenshot=None` keeps the case evidence clean;
  - nothing is added to the checkpoint, the verdict or TestRail.
- **Stale or aborted.** The whole block is under `_is_stale_execution`. The
  `cancellation_check` stops on abort or a newer generation, and the log callback
  is generation-filtered.
- **Old runner** without CLOSE_APP_V1: the teardown is skipped, not blocked.
- **Clause drift:** a single `CLOSE_CLAUSE_PREFIX` constant is used for both the
  planner wording and the extraction.
- **Secrets:** the digests in the teardown prompt carry the same exposure as in the
  digest plan (already in the case log and the provider transcript).
- **Lazy import:** it reuses the existing documented cycle-break import site and
  adds no new one.

## Implementation steps
Do this after the digest plan's steps 1-6.
1. Add the setting. (files: `auto_tester/settings.py`, `example.env`)
2. Add `tool_definitions_named` and `allowlisted_dispatcher`. (files:
   `agents/services/tool_registry.py`)
3. Add the `_run_agent_loop` overrides and the `run_sub_agent(tool_names=...)`
   keyword. (files: `agents/services/agent_loop.py`,
   `agents/services/sub_agent.py`)
4. Add `CLOSE_CLAUSE_PREFIX` in the planner rule, `pending_close_clauses`,
   `failed_attempt_digests` and `build_teardown_sub_task`. (files:
   `agents/services/orchestrator_prompts.py`)
5. Add `run_case_teardown` and `TEARDOWN_TOOL_NAMES`. (files:
   `agents/services/case_teardown.py`)
6. Add `needs_teardown` and `TEARDOWN_BLOCKED_REASONS`. (files:
   `projects/case_teardown_policy.py`)
7. Add `_safe_teardown`, the `allow_teardown` flag and the `finally` ordering in
   `execute_test_run_test_case`. (files: `projects/services.py`)
8. Add a short paragraph on the teardown under "Closing apps". (files:
   `agents/CLAUDE.md`)
9. Run `pre-commit run --all-files`.

## Verification
- `mypy .` and `pre-commit run --all-files` are clean, and
  `python manage.py check` passes.
- Shell smoke checks (`python manage.py shell -c`):
  - `pending_close_clauses`, with a plan of 4 steps (step 2's clause on a passed
    step, step 3 failed with a clause, step 4 unrun with a close_tab clause), gives
    only the step 3 and step 4 clauses. An all-passed plan gives `()`.
  - `failed_attempt_digests` respects `limit` and the order.
  - `needs_teardown` is True only for FAILED and the four Blocked reasons.
  - `tool_definitions_named(TEARDOWN_TOOL_NAMES)` has exactly 5 entries.
  - `allowlisted_dispatcher` refuses `type_text`.
  - The planning prompt still contains "Afterwards, close" twice.
- Manual, with a live runner and vision worker:
  1. **Failed with stop.** Use a case that opens a desktop editor (TextEdit or
     Notepad) in step 2, with the close clause on step 3, and an impossible
     expected result in step 2. After Failed, check that the log shows `[Teardown]`
     closing the editor and that the editor is gone before the next case.
  2. **Web-only failed case.** Check that the log shows "Nothing left open to
     close" and that no agent run happens.
  3. **Blocked, usable machine.** Open the editor, then ask for a browser that is
     not installed (`browser_unavailable`). Check that the case is Blocked with an
     unchanged reason and message, and that the editor is closed. Then Resume the
     case and check that the restore sub-task reopens the editor.
  4. **Abort during teardown.** Check that the teardown stops, the case keeps its
     status and cleanup still runs.
  5. **Runner disconnected (Blocked `runner_disconnected`).** Check that no
     teardown is attempted.
  6. With `CASE_TEARDOWN_TIMEOUT_SECONDS=0`, check that no teardown runs.
  7. **Passed case.** Check that no teardown runs.
- Run the `reviewer` and `verifier` agents in parallel with the user's request and
  both plan paths.

## Open questions
None blocking. Decisions taken (change them in review if wanted):
- **Mechanism:** a deterministic gate plus a restricted teardown sub-agent (not a
  server-only close, and not an agent on every failure).
- **Placement:** projects service `finally`, after the status write and before
  `_safe_cleanup`; the orchestrator is unchanged.
- **Eligibility:** FAILED, and BLOCKED for `browser_unavailable`,
  `runner_outdated`, `resume_state_lost` and `internal_error`. Not Passed, not
  Cancelled, and not runner, visual engine, AI service or time-limit blocks.
- **Blocked cases are torn down even though they are resumable;** the restore
  sub-task re-establishes state.
- **Tools:** `take_screenshot`, `close_app`, `close_tab`, `click` and `wait`, with
  `force=true` allowed. No `key_press`.
- **Budget:** a new setting `CASE_TEARDOWN_TIMEOUT_SECONDS=120` (0 disables), and
  30 iterations as a constant.

## Changelog
- 2026-10-24: created (companion to fix-failed-attempt-action-digest.md)
- 2026-10-24 (review 1):
  - Teardown helpers moved from `orchestrator_prompts.py` to the new
    `agents/services/case_teardown_prompts.py`. Only `CLOSE_CLAUSE_PREFIX` stays
    shared with the planner.
  - `build_sub_agent_system_prompt(tool_names=)`: a restricted sub-agent is told
    only its tools plus the app-lifecycle rules, not the full taxonomy, persona
    rules and QA rules, so it does not waste iterations on refused tools.
  - The target gate runs before any readiness query. `run_case_teardown` takes a
    `machine_ready` callback, and `_machine_can_close_apps` reads
    `agent_connected` and capabilities in one query, then checks the ready
    workers. `runner_supports` is no longer used here.
  - `execute_test_run_test_case` checks for staleness again after the teardown,
    so `_safe_cleanup` and the run status update never touch a run that a
    Resume or Start took over during the teardown.
  - The second local import (`run_case_teardown`) shares the documented
    cycle-break site with `run_orchestrator`. Both now sit at the top of the
    function, so an import failure propagates before the case is marked In
    progress instead of being recorded as Blocked. Accepted: it is a deploy bug,
    not a case outcome.
  - Accepted residuals, named in the change note:
    - `click` is allowed and could press a close button that the runner-side
      close guards never see. Only the prompt prevents this. Normal sub-agents
      already have `click`, `key_press` and `execute_command`.
    - The 120 s budget is checked between LLM calls, so one slow request
      (`OPENAI_REQUEST_TIMEOUT`) can overrun it, up to the case soft limit.
    - A clause from a step that never ran may name an app that was open before
      the case started.
    - Typed text (150 chars at most per value) is stored in the checkpoint
      digest, which is one more copy of what the case log already holds.
- 2026-10-24 (review 2): the restricted system prompt drops the environment
  section, which pointed at browser, typing and key_press tools. The teardown
  passes a "case has finished" state instead of "" ("This is the first step").
