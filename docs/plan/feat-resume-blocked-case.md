# feat: Resume blocked cases in place, from the step where they stopped

Status: Done
Created: 2026-10-22
Request: "Ability to resume blocked test. Sometimes tests blocked because of infra issues that human will fix, we need to be able to continue them from when they stop." A case that is Blocked (runner lost, visual engine, AI service, time limit, ...) should be resumable after a human has fixed the infrastructure. Resuming happens on the same run and the same case row. Steps that already passed keep their results, screenshots and log, and execution continues at the step that was interrupted, without re-running the whole case in a new run. When the runner was lost, the rest of the run was Cancelled with it, so the run as a whole should also be resumable.

## Context

- **Blocked today** (`docs/changed/2026-10-22-blocked-result.md`):
  - `execute_test_run_test_case` (`projects/services.py`) turns any infrastructure fault into `_mark_pivot_blocked(pivot, reason, message)`, with the `BlockedReason` coming from `projects/case_blocking.classify_case_exception`.
  - The only way to retry is `rerun_cases(test_run, RerunScope.BLOCKED)` via `projects:test_run_rerun_blocked`. That creates a **new** run with fresh pivots, and every case re-plans and re-runs from step 1.
- **Runner lost past grace** (`docs/changed/2026-10-22-controller-reconnect.md`): `_abort_run_after_reconnect_timeout` blocks the current case (`runner_disconnected`), then `abort_test_run(..., revoke_task=False)` cancels every CREATED/IN_PROGRESS pivot and sets the run CANCELLED. Later chain tasks early-exit on CANCELLED.
- **Orchestrator state is not persisted**:
  - `agents/services/orchestrator.run_orchestrator` plans with `_plan_sub_tasks`, runs `_execute_sub_tasks` and keeps `attempts: list[SubTaskAttempt]`, `state_lines`, `recovery_counts` and the per-case `BrowserSelection` in memory only.
  - When an `InfrastructureError` or `SoftTimeLimitExceeded` propagates out of `run_sub_agent`, all of that is lost. Only the free-text `pivot.logs` (which includes the "Sub-task i (budget ...)" lines), the `TestRunScreenshot` rows and `pivot.result` (the blocked message) survive.
  - The interrupted step has no attempt recorded, because the exception escapes before `attempts.append`.
  - So resume-from-step-N is impossible today without new persistence. Parsing the plan back out of the log text is not an option.
- **Verdict** (`docs/changed/2026-10-22-recovered-step-verdict.md`): `_build_verdict` passes only if every planned step has a final `kind="step"` attempt and each one passed (`orchestrator_prompts.final_step_indices`). Recoveries never decide the outcome. `format_attempt_lines` labels the attempts and assumes steps run in order from step 1.
- **Sub-agents have no memory** (`orchestrator_prompts.build_plan_system_prompt`). Each sub-task depends on machine state left by earlier steps ("in the already-open VS Code window"), and its only context is `state_description` (lines like "Step n: summary").
- **Case start runs `_safe_cleanup`** (empties the runner's cleanup dir, `~/Downloads` by default) before the case, and again in `finally` after the case, including after a block when the runner is still reachable.
- **Checked / Sent state** (`docs/changed/2026-10-22-testrail-checked-results.md`):
  - Blocked is checkable (`run_case_check_services.CHECKABLE_STATUSES`).
  - `testrail_source_result_id` is the dedupe key, "at most once per execution".
  - `reset_test_run` locks the run, refuses while `is_checked_push_live`, and clears the Checked/Sent state.
  - The 018 whole-run push dedupes on `testrail_pushed_comment_hash`, so a changed verdict is re-pushed automatically.
- **Execution order** (`docs/changed/2026-10-22-testrail-case-order.md`): `start_test_run` chains `execute_test_run_case.si(pid)` in `RUN_CASE_ORDERING`. The task is on the `execution` queue with soft/hard limits of 1800/1860 + `max_outage_seconds_per_case()`.
- **Start gating**:
  - `start_test_run` only checks for WAITING status, no other STARTED run, and at least one case. It takes no lock.
  - Machine/visual-engine readiness (`get_run_readiness`, `get_machine_state`) is enforced only by the UI. At execution time, `wait_for_controller` and `_wait_for_omniparser_ready` wait for it.
- **UI**:
  - `templates/projects/test_run_detail.html`: Alpine `testRunLive`. The "Re-run failed" and "Re-run blocked" forms and "Reset run" show for done/cancelled. Statuses update over the WS `test_run_status` and `pivot_status` events.
  - `templates/projects/test_run_case_detail.html`: Alpine `testRunCaseLive`. Its "Why this case was blocked" card points to "Re-run blocked". Logs and screenshots stream over WS.
- **Versions**: Django ~=5.2.11, celery ~=5.6.2, channels ~=4.3.2 (`requirements.txt`). Latest migration is `projects/0022_blocked_reason_runner_outdated`.

## Goals
- Resume a Blocked case **in place** (same run, same pivot). It keeps its earlier step attempts, screenshots and log, and continues at the step that was interrupted.
- Resume a finished (Done or Cancelled) **run** in place:
  - every Blocked case continues from its interrupted step;
  - every Cancelled case continues from its interrupted step if it had started, or from the start if it never started.
- The final verdict and summary cover the whole case: earlier attempts plus resumed attempts.
- Before the resumed step runs, re-establish the machine state that the earlier steps left behind. If that is not possible, the case becomes Blocked again, not Failed. No false Jira tickets.
- Keep a short history of each resume (what blocked it, who resumed it, from which step) on the case page.
- Same safety gates as starting a run, enforced on the server.

## Non-goals
- Resuming a case left In progress by a Celery hard-time-limit kill. That still needs its own fix plan (noted in the blocked-result change note).
- Resuming Passed or Failed cases. "Reset run" and "Re-run failed" cover those.
- Persisting the orchestrator's failure-evaluation conversation (`evaluate_messages`) or recovery counters across a resume. A resumed case starts with fresh counters.
- Automatic resume when the runner reconnects. Resume is always a human action.
- Removing or changing "Re-run blocked". It stays as the "fresh run" option.
- Controller protocol changes. None are needed.
- Bulk per-row resume in the run table's checkbox bar. That bar belongs to Check/Uncheck. This plan covers the run-level and case-level actions only.

## Design

### Data model
Migration `0023_case_resume`, additive only:

1. **`TestRunTestCase.execution_checkpoint`**: `JSONField(default=dict, blank=True)`. It holds the serialized orchestrator checkpoint (see Services):
   - `version`;
   - `plan` (list of `{description, expected_result, timeout_seconds}`);
   - `attempts` (list of `{step_num, kind, status, summary, iterations, error}`);
   - `next_step` (1-based cursor of the step currently running or next to run);
   - `browser` (the `BrowserSelection.current` value);
   - `total_iterations`.

   An empty dict means there is nothing to resume, and execution plans from scratch.

   **Why a JSONField and not step rows:** one writer, always read and written as a whole, no queries per step, nothing in the UI filters on it. A step table would add a model, bulk writes and joins for no current consumer.
2. **New model `TestRunCaseResume`** (the resume history, one row per resume):

   | Field | Type |
   |---|---|
   | `test_run_test_case` | FK CASCADE, `related_name="resumes"` |
   | `resumed_by` | FK user, SET_NULL, null |
   | `created_at` | auto_now_add |
   | `from_step` | PositiveSmallIntegerField, null; null means "from the start", there was no checkpoint |
   | `previous_status` | CharField with `TestRunTestCaseStatus` choices |
   | `previous_blocked_reason` | CharField with `BlockedReason` choices, blank |
   | `previous_result` | TextField; the blocked message, already user-safe |
   | `previous_finished_at` | DateTimeField, null |

   `Meta.ordering = ["created_at", "id"]`.
3. **`BlockedReason.RESUME_STATE_LOST`** = `"resume_state_lost"`, "Could not restore the state to resume". This is a choices-only `AlterField`.
4. **Reset and rerun**:
   - `reset_test_run` also clears `execution_checkpoint` and deletes the run's `TestRunCaseResume` rows (one `DELETE` via the run filter).
   - `rerun_cases` / `create_test_run_with_cases` need no change, because new pivots default to `{}`.

Rollback: before reversing the migration, run `UPDATE ... SET status='blocked', blocked_reason='internal_error' WHERE blocked_reason='resume_state_lost'` (same pattern as 0018).

### Services

**`agents/` (no ORM, typed):**
- `agents/types.py`:
  - **`OrchestratorCheckpoint`** (frozen dataclass), with fields:
    - `plan: tuple[SubTask, ...]`;
    - `attempts: tuple[SubTaskAttempt, ...]`;
    - `next_step: int`;
    - `browser: BrowserName`;
    - `total_iterations: int`.
  - **`CheckpointCallback`**: `Callable[[OrchestratorCheckpoint], None]`.
  - **`SubTaskAttempt.kind`** gains `"resume"`, the pre-step state restore. `final_step_indices` already ignores any kind other than `"step"`, so the verdict is unaffected.
- `agents/services/orchestrator_checkpoint.py` (new, pure): `checkpoint_to_json(cp) -> dict[str, object]` and `checkpoint_from_json(data) -> OrchestratorCheckpoint | None`.
  - The parser is strict. A wrong version, a malformed shape or an empty plan returns `None`, which means "plan from scratch", and logs a warning.
  - It also holds `truncate_checkpoint(cp, from_step) -> OrchestratorCheckpoint`. It drops every attempt with `step_num >= from_step` and sets `next_step = from_step`. The resume service uses it when the user picks an earlier step.
- `agents/services/orchestrator.py`: `run_orchestrator` gains two keyword args.
  - **`resume: OrchestratorCheckpoint | None = None`.** When set:
    - skip `_plan_sub_tasks` and use `resume.plan`;
    - seed `attempts` and `total_iterations`;
    - rebuild `state_lines` from the seeded attempts with the same wording `_execute_sub_tasks` uses ("Step n: summary", "Step n: FAILED — ...", "Recovery for step n: ...");
    - initialize `BrowserSelection(current=resume.browser)`;
    - if `next_step > 1`, run one **resume-preparation sub-task** (below);
    - start the step loop at `i = next_step - 1`.
  - **`on_checkpoint: CheckpointCallback | None = None`.** Called:
    - after planning (`next_step=1`);
    - before each planned step starts (cursor = that step);
    - after every appended attempt (step, recovery or resume).

    Each call is one small UPDATE, a handful per case. The orchestrator stays ORM-free; persistence is the callback's job.
  - **Resume-preparation sub-task.** It is built deterministically from the persisted plan, with no extra planning LLM call:
    - The description lists the descriptions and outcomes of steps 1..N-1, followed by: "The test was interrupted and the test machine may have been restarted or changed by a person. Make sure the machine is in the state those steps left it in (applications open, pages loaded, files present, logged in), redoing whatever is no longer in effect. Do not judge the application; only restore state."
    - The expected result is "The machine is in the state described above, ready for step N: <step N description>". The budget is `bounds.maximum`.
    - It is recorded as `SubTaskAttempt(step_num=N, kind="resume", ...)`.
    - On `pass`, a state line "Resumed: state restored before step N — <summary>" is added and the loop continues.
    - On `fail`, the orchestrator raises the new **`ResumeStateLostError(InfrastructureError)`** (`agents/exceptions.py`) carrying the sub-agent summary. The existing block path then marks the case Blocked `resume_state_lost`; it is not judged Failed.
  - Prompt and label wording for the new kind:
    - `orchestrator_prompts.format_attempt_lines` labels it "Resume: state restored before step N" and does not count it as a run step;
    - the verdict prompt mentions that the case was resumed.

**`projects/` services:**
- **`projects/case_checkpoint_services.py`** (new), the persistence glue for one pivot:
  - `build_checkpoint_callback(pivot) -> CheckpointCallback`: writes `execution_checkpoint` with `update_fields`.
  - `load_resume_checkpoint(pivot) -> OrchestratorCheckpoint | None`.
  - `list_resume_steps(pivot) -> list[ResumeStepOption]`: a frozen dataclass of `(step_num, description, passed)` for steps 1..`next_step` on the case page. It reads only the loaded pivot and runs no query.
- **`projects/services.py`, `execute_test_run_test_case`** (legacy module; the change is small and on the existing path):
  - load the checkpoint with `load_resume_checkpoint`;
  - pass `resume=` and `on_checkpoint=build_checkpoint_callback(pivot)` to `run_orchestrator`;
  - when resuming, write one log separator line "=== Resumed from step N ===" before execution.

  Everything else is unchanged: pre-case wait, OmniParser gate, cleanup, the error and block mapping, and `finally`.
- **`projects/case_blocking.py`**: map `ResumeStateLostError` → `RESUME_STATE_LOST`, with a fixed user-safe message plus the sub-agent summary truncated through the existing `_truncate`. The summary is model-written text about the screen; it is never `|safe` and is rendered escaped.
- **`projects/run_resume_services.py`** (new; one use case, following the `run_case_check_services.py` pattern). It imports `projects.tasks.execute_test_run_case` at the top. `tasks.py` does not import this module, so there is no cycle.
  - `RESUMABLE_STATUSES = (BLOCKED, CANCELLED)`.
  - `get_resume_panel(test_run) -> ResumePanel`: a frozen dataclass with the `blocked` / `cancelled_started` / `cancelled_unstarted` counts, `can_resume` and the `blockers` messages. It uses one aggregate query, plus the readiness of `get_machine_state(project)`.
  - `resume_test_run(*, test_run, user) -> int` resumes every resumable pivot of the run.
  - `resume_test_run_case(*, pivot, user, from_step: int | None) -> None` resumes one case. `from_step` must be in 1..`checkpoint.next_step`; `None` means the cursor.
  - Both delegate to one private `_resume_pivots(test_run, pivot_ids, user, from_steps)` inside `transaction.atomic`:
    1. `select_for_update()` the **Project** row. This serializes against another start or resume in the same project; `start_test_run` has the same check-then-act race today, see Risks. Then lock the **TestRun** row.
    2. Refuse with `RunResumeError` (new, in `projects/exceptions.py`, with a `.message` safe to show the user) if:
       - the run is not DONE or CANCELLED;
       - another run in the project is STARTED;
       - `is_checked_push_live(run, now)`;
       - the 018 push is `PUSHING`;
       - `get_machine_state(project) != READY` ("Connect the Test Runner and wait for the visual engine");
       - no selected pivot is in `RESUMABLE_STATUSES`.
    3. Per pivot:
       - `bulk_create` one `TestRunCaseResume` snapshot;
       - truncate the checkpoint if an earlier `from_step` was chosen;
       - reset the execution fields: `status=CREATED`, `blocked_reason=""`, `result=""`, `finished_at=None`;
       - clear the Checked/Sent fields exactly as `reset_test_run` does (`checked_at/by`, `testrail_source_result_id/_sent_at/_attachment_count`, `testrail_source_skip_reason`). The new verdict is a new execution, so it can be checked and sent again; TestRail keeps the earlier result in its history.
       - **Keep** `logs`, `screenshots`, `started_at` and `execution_checkpoint`.
       - Do this with `bulk_update`. The few chosen-step truncations are per-row assignments in the same `bulk_update`.
    4. Run: `status=STARTED`, `finished_at=None`, keep `started_at`.
    5. `transaction.on_commit`: build `chain(execute_test_run_case.si(pid) for pid in ordered_ids)`, with `ordered_ids` in `RUN_CASE_ORDERING`, then `apply_async()`, then save `celery_task_id` so Abort can revoke it, then `_broadcast_test_run_status` plus a pivot status broadcast per resumed pivot.

       The broadcast helpers are private in `services.py`. Expose one public `broadcast_run_and_pivots(test_run, pivots)` wrapper there rather than importing `_` names.
- `_mark_pivot_in_progress` is unchanged. It sets `started_at` to the resume time; the history row keeps when the earlier attempt ended.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_resume` → `/projects/<id>/test-runs/<run_id>/resume/` | POST | `@project_membership_required` | none (CSRF only) | redirect to run detail; message "Resumed N cases" | `RunResumeError.message` → `messages.error`, redirect back; 404 for a foreign run |
| `projects:test_run_case_resume` → `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/resume/` | POST | `@project_membership_required` | `CaseResumeForm(step_choices)`: `from_step` TypedChoiceField(int), choices from `list_resume_steps`, initial = cursor; omitted when there is no checkpoint | redirect to case detail | invalid form or `RunResumeError` → `messages.error`; 404 if the pivot is not in the run or project |

Run detail (`_build_test_run_detail_context` + template):
- New context key `resume_panel` (`ResumePanel`).
- A **"Resume run"** primary button shows for done/cancelled (`x-show`, like the rerun buttons) when `resume_panel.can_resume`. It opens a confirm dialog (`partials/confirm_dialog.html`) whose server-built body says: "N blocked cases continue from the step where they stopped (the agent first restores the machine state); M cancelled cases continue or start. Results stay on this run; Checked marks on these cases are cleared."
- When the only blocker is machine readiness, show the button disabled with the blocker text.
- "Re-run blocked" stays; its label hint changes to "in a new run".

Case detail (`test_run_case_detail` view + template):
- New context keys: `can_resume` (bool), `resume_form` (`CaseResumeForm` or None), `resume_steps`, `resumes` (the pivot's `TestRunCaseResume` rows plus `resumed_by`, `select_related`, at most 1 query).
- The "Why this case was blocked" card gets a "Resume case" button that opens a dialog. The dialog has a step select listing each step's description and a "passed" mark, defaulting to the interrupted step, plus a hint that the agent first restores the state left by earlier steps. Shown also for Cancelled cases that have a checkpoint.
- The card's hint text is updated so it no longer only says "use Re-run blocked".
- A new "Resume history" list shows, for each resume row: "Blocked: <reason label> · <message> — resumed by <name> on <date> from step N". All values are autoescaped.

### Background work and WebSockets
- **Celery**: reuse `execute_test_run_case(pivot_id)` on the `execution` queue. Its signature and time limits are unchanged; the pivot's checkpoint decides whether it resumes, so tasks already queued at deploy time keep working. No new task.
  - A resumed case gets a fresh full case budget (1800s + outage allowance). The resume-preparation sub-task counts against it.
- **WebSockets**: no new event types.
  - The run page already flips on `test_run_status` and `pivot_status`; the case page flips on `status`, and new log lines and screenshots append over the existing events.
  - Verify that the `testRunLive` handler for a transition from `done`/`cancelled` back to `started` does not trigger its "justFinished" reload. It does not, because it only reloads on transitions *to* done/cancelled. The Abort button appears again through `x-show`.
- **Controller**: no protocol change; `CLIENT_VERSION` and capabilities are untouched. Resume uses the existing actions only.

### Settings / infrastructure
- No new settings. The resume-preparation budget reuses `build_timeout_bounds().maximum`.
- On deploy: run `migrate`, then restart the `execution` workers (the orchestrator changes) and Daphne/web.

### Data flow
1. A human fixes the infrastructure, sees the runner connected and the visual engine ready, and clicks **Resume run** (or **Resume case**, choosing a step).
2. The view validates the form, calls `resume_test_run` / `resume_test_run_case` and maps `RunResumeError` to a message.
3. The service locks the project and the run, checks the gates, snapshots each pivot into `TestRunCaseResume`, resets execution and Checked fields (keeping checkpoint, log and screenshots), sets the run STARTED, and on commit chains `execute_test_run_case` and broadcasts.
4. The worker runs `execute_test_run_test_case`: wait for the controller, the OmniParser gate, pre-case cleanup, `load_resume_checkpoint`, then `run_orchestrator(resume=cp, on_checkpoint=...)`.
5. The orchestrator skips planning, seeds attempts, state and browser, runs the resume-preparation sub-task (fail → `ResumeStateLostError` → Blocked `resume_state_lost`), then runs steps N..end, persisting the checkpoint as it goes, and builds the verdict over all attempts.
6. `_finalize_pivot` sets Passed or Failed, or the case blocks again on a new infrastructure error (and is resumable again). `_update_test_run_status_if_needed` sets the run DONE when nothing is CREATED or IN_PROGRESS.

## Alternatives considered
- **Re-run the whole case in place, with no checkpoint.**
  - Simplest: reset the pivot to CREATED and re-plan.
  - But it does not "continue from when they stop". It also re-does long setup steps and loses the earlier attempt's evidence in the verdict.
  - Kept as a fallback when no checkpoint exists, and reachable by choosing step 1.
- **Resume at step N with no state-restore step.** Cheaper, but a person fixing the infrastructure often restarts the machine, the runner or the app. Step N then fails on missing state and gives a false Failed, which brings back the Jira-ticket problem that Blocked was built to remove.
- **Re-plan on resume, with the earlier summaries in the prompt.** The steps would get new numbers, `final_step_indices` and the summary would mix two plans, and it costs an extra LLM call. Reusing the persisted plan keeps numbering and the verdict rules intact.
- **Resume into a new run** (extending "Re-run blocked"). That breaks the 1:1 between a run and its source TestRail run push and the "Checked" workflow, and drops the steps already done. In-place resume keeps the run coherent.
- **A step-results table instead of the JSONField.** More schema and writes for no current reader; it can be done later if a per-step UI is wanted.
- **Persisting via the log text.** Fragile parsing of human text. Rejected.

## Risks and mitigations
- **The machine state may not match after step N-1.** Mitigations:
  - the mandatory resume-preparation sub-task;
  - a failure blocks the case (`resume_state_lost`) instead of failing it;
  - the user can pick an earlier step, or step 1.
  - Residual risk: the restore sub-agent "redoes" a step with a side effect (for example, submits a form twice). Its prompt says to restore state, not to repeat actions already in effect.
- **Downloads are gone.** The pre-case cleanup and the post-block `finally` cleanup empty the cleanup dir, so files from earlier steps are not there on resume. The restore sub-task has to re-download them. Skipping the cleanup would not help, since the post-block cleanup already ran.
- **Duplicate side effects in TestRail.** Clearing `testrail_source_result_id` on resume means a re-send adds a second TestRail result for the case. This is intended: the latest result wins, and TestRail keeps history.
  - Refused while a checked send is live (row lock + `is_checked_push_live`, same as reset) and while the 018 push is PUSHING.
- **Concurrency.**
  - The project-row lock serializes a resume against another resume. `start_test_run` does not take that lock today, so a simultaneous Start on another run could still race. Recommended small follow-up in this plan: make `start_test_run` take the same project lock (step 4). It is a one-line, low-risk change to the same check.
  - A double-click on Resume: the second request sees STARTED and is refused.
- **Stale or invalid checkpoint JSON** (a schema change in the future): it is versioned, the parser returns `None`, and the case runs from scratch with a log line.
- **Size and write load.** The checkpoint is small: at most `ORCHESTRATOR_MAX_SUBTASKS` steps plus their attempts, as summaries. It is written a few times per step with `update_fields`, on the same pivot row the log callback already updates once per log line, so the extra load is negligible.
- **A lost evaluate conversation and reset recovery counts.** A resumed step gets a fresh recovery budget. Accepted, see Non-goals.
- **XSS.** The previous result, restore summary and step descriptions are LLM or TestRail text. They are rendered with autoescape, never `|safe`. The confirm-dialog body is built server-side from counts only.
- **Run duration.** The run keeps its original `started_at`, so the displayed duration includes the human downtime. This is acceptable and noted in Open questions.
- **Migration.** It is additive on populated tables (one JSONField with a default, one new table, one choices change). Django's `AddField` with a constant default sets the default and then drops it. On Postgres 11+ that is a metadata-only change, so the table is not rewritten.

## Implementation steps
1. **Shared types, exceptions and migration**. Written first, before any parallel work.
   - Files: `agents/types.py` (`OrchestratorCheckpoint`, `CheckpointCallback`, `SubTaskAttempt.kind` gains `"resume"`); `agents/exceptions.py` (`ResumeStateLostError`); `projects/exceptions.py` (`RunResumeError`); `projects/models.py` (`execution_checkpoint`, `TestRunCaseResume`, `BlockedReason.RESUME_STATE_LOST`); `projects/migrations/0023_case_resume.py`; `projects/admin.py` (read-only inline for the resumes, optional).
2. **Orchestrator resume and checkpoint**.
   - Files: `agents/services/orchestrator_checkpoint.py` (new); `agents/services/orchestrator.py` (`resume`, `on_checkpoint`, seeding, resume-preparation sub-task); `agents/services/orchestrator_prompts.py` (labels for the `"resume"` kind and the verdict-prompt note).
3. **Case execution wiring**.
   - Files: `projects/case_checkpoint_services.py` (new); `projects/services.py` (`execute_test_run_test_case` passes `resume` / `on_checkpoint` and the log separator; `reset_test_run` clears checkpoints and resumes; public `broadcast_run_and_pivots`); `projects/case_blocking.py` (`ResumeStateLostError` mapping and message).
4. **Resume use case**.
   - Files: `projects/run_resume_services.py` (new); `projects/services.py` (`start_test_run` takes the project row lock, small).
   - Steps 2, 3 and 4 own disjoint files, except `projects/services.py`, which steps 3 and 4 both touch. Give `services.py` to one sub-agent, or do step 4's `start_test_run` change in step 3.
5. **Views, forms, URLs**.
   - Files: `projects/forms.py` (`CaseResumeForm`); `projects/views.py` (`test_run_resume`, `test_run_case_resume`; `resume_panel`, `can_resume`, `resume_form`, `resume_steps` and `resumes` in the context builders); `projects/urls.py`.
6. **Templates and CSS**.
   - Files: `templates/projects/test_run_detail.html` (Resume run button, dialog, re-run hint and the context comment block); `templates/projects/test_run_case_detail.html` (Resume dialog with step select, Resume history, updated blocked-card hint, context comment block); `static/css/punk-hazard.css` (only if a list style is missing).
7. **Integration pass** by the orchestrating agent: `pre-commit run --all-files`, `mypy .`, `python manage.py makemigrations --check --dry-run`, `python manage.py check`.

## Verification
Manual checks (the project has no test suite):
1. `mypy .`, `pre-commit run --all-files`, `python manage.py check` and `makemigrations --check` are clean. `migrate` applies 0023 on the dev DB.
2. **Runner lost mid-case.** Start a multi-step run and kill the runner during step 3 of case 2. Wait out the grace period. The run is Cancelled, case 2 is Blocked `runner_disconnected`, and the remaining cases are Cancelled. In the dev DB, `execution_checkpoint` for case 2 shows `next_step=3` with two passing attempts.
3. Restart the runner and wait for the visual engine to be ready, then click **Resume run**. The run is STARTED and case 2 is Running. The log shows the separator, the "Resume: state restored before step 3" attempt and steps 3..end. Earlier screenshots remain, and the cases that never started run from step 1. The final summary lists steps 1–2 from the first attempt.
4. **Resume case with an earlier step.** On a Blocked case, choose step 1. The attempts for steps >= 1 are dropped, no restore sub-task runs, and the case runs from step 1 with the same plan.
5. **Restore fails.** Resume with the app made unreachable on purpose (for example, a wrong URL in the context). The case is Blocked `resume_state_lost` with an escaped message and stays resumable.
6. **Gates.**
   - Resume while another run is STARTED, with the runner disconnected, with the visual engine loading, or while a checked send is live: each gives a clear error and changes nothing.
   - Resume on a WAITING or STARTED run gives an error.
   - A second click right after the first is refused.
7. **Checked / Sent.** Check a Blocked case and send it, then resume it. The Checked/Sent marks are cleared, the new verdict can be checked and sent again, and the 018 push shows it as changed.
8. **Reset run** after a resume clears the checkpoint and the resume history.
9. **No checkpoint** (a case blocked during planning, for example AI service down): Resume runs it from scratch and the resume history row shows "from the start".
10. A Done run with one Blocked case among passes: Resume run re-runs only that case, and the run returns to Done.
11. Query counts: the run detail adds 1 query (the panel aggregate); the case detail adds 1 (resumes plus user).
12. Run the `reviewer` and `verifier` agents (give `verifier` the original request and this plan path).

## Open questions
- **Q1 Resume point and state.** Resume at the interrupted step, after an automatic "restore state" sub-task built from the earlier steps, with an option on the case page to choose an earlier step (step 1 = whole case again, same plan)? *Assumption in this draft: yes. The restore sub-task always runs when resuming past step 1, and a failed restore marks the case Blocked `resume_state_lost` rather than Failed.*
- **Q2 Run-level scope.** Should **Resume run** also continue the run's Cancelled cases, both the ones the runner loss cancelled and the ones a user Abort cancelled, or only Blocked cases? *Assumption: every Blocked and Cancelled case. Started ones continue from their checkpoint, never-started ones run from the start. A user who aborted on purpose can simply not resume.*
- **Q3 In place versus new run.** Resume always happens in place on the same run (keeps the TestRail source-run push and Checked flow coherent), and "Re-run blocked" (new run, from scratch) stays available beside it. Keep both, or replace "Re-run blocked"? *Assumption: keep both. "Re-run blocked" is relabelled "in a new run".*
- **Q4 Checked/Sent on resume.** Resuming clears the case's Checked mark and, if its Blocked result was already sent to the source TestRail run, its Sent mark, so the new verdict can be checked and sent as a new TestRail result. Is that OK, or should resume be refused for cases already sent? *Assumption: clear and allow, because TestRail keeps the result history.*
- **Q5 Readiness gate.** Resume refuses unless the runner is connected **and** the visual engine is ready (stricter than Start, which only gates in the UI). *Assumption: enforce on the server for Resume. Tighten Start to the same server rule only if you want; this draft only adds the project lock to Start.*
- **Q6 Timing shown.** The run keeps its original start time (its duration includes the downtime), and the resumed case's start time becomes the resume time (the history row keeps when the earlier attempt ended). *Assumption: as described.*
- **Q7 Recovery budget.** A resumed case gets a fresh case time budget and fresh per-step recovery counters. *Assumption: yes.*

## Changelog
- 2026-10-22: created (Draft).
- 2026-10-22: Q1–Q4 answered by the user as recommended; Q5–Q7 taken as the stated assumptions. Ready, then In progress.
- 2026-10-22: implementation deviations: the pure checkpoint (de)serializer is `agents/checkpoint.py` (not `agents/services/orchestrator_checkpoint.py`) because importing anything under `agents.services` runs its `__init__`, which imports `projects.services` (cycle). `start_test_run` now runs inside `transaction.atomic` and queues its chain in `on_commit`, so workers see the STARTED run.
- 2026-10-22: review round 1 fixes:
  - Abort-then-Resume race (two chains on one pivot): new `TestRun.execution_generation`, bumped by Start and Resume. `execute_test_run_case(pivot_id, generation=None)` skips a stale task; the cancellation check also fires on a generation change; a stale worker writes no verdict, block, cancel, run status or checkpoint (`build_checkpoint_callback(pivot, generation)` filters its UPDATE on the generation). Residual: the stale worker may still drive the machine and append log lines or screenshots until its next cancellation check, the same latency as a plain Abort.
  - Broker failure on enqueue: shared `queue_test_run_cases` cancels the run (`abort_test_run(revoke_task=False)`) instead of leaving it STARTED.
  - `abort_test_run` re-reads the status under a row lock.
  - Case dialog: when every step already ran, the default option is "After the last step: only write the verdict" (`ResumeStepOption.verdict_only`) instead of re-running the last step.
  - `get_resume_panel(test_run, project)`; unused `ResumePanel` fields removed; lean `get_run_case_for_resume` for the POST.
  - Rejected: the disabled "Resume run" button shows for any blocker (not only readiness), with the blocker text; it is more informative than hiding it. Rejected: typing the channel layer as `BaseChannelLayer` in `_send_pivot_status_to_run`; channels is untyped and the module uses `Any` for the layer everywhere.
- 2026-10-22: review round 2 fixes:
  - Stale worker: the `finally` cleanup and run-status update are skipped; log lines are appended in the database (`_append_pivot_log`, `Concat(F("logs"), ...)` filtered on the generation) instead of rewriting the whole `logs` field, also in `_mark_pivot_blocked`; screenshots are dropped once stale. This replaces the round-1 residual note: a stale worker can still drive the machine until its next cancellation check (same latency as Abort), but writes nothing.
  - `queue_test_run_cases` runs before the broadcast and returns whether it queued; broadcast happens only on success (channel layer and broker share Redis).
  - Tasks queued before the deploy (no generation) count as generation 0 (`tasks.py`); `execute_test_run_test_case(pivot_id, generation=None)` treats None as the run's current generation (debug `test_agent` command).
  - `abort_test_run`: one transaction (the decorator) instead of a nested savepoint.
  - Orchestrator: "continue" and "stop" decisions move the cursor (stop → past the last step), so a case blocked while building its verdict never reruns a decided failed step.
- 2026-10-22: review round 3: accepted residual (not fixed): a stale worker's terminal write (`_finalize_pivot`, `_mark_pivot_cancelled`, `_mark_pivot_blocked`) is check-then-write, so a Resume committing in the milliseconds between the check and the save could be overwritten; the new task re-marks the case In progress. Logs, screenshots and checkpoints are fully gated.
