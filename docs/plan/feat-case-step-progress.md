# feat: Live step progress on the case page and per-step results in TestRail

Status: Done
Created: 2026-10-23
Request: Two parts. (1) While a case runs, the orchestrator splits the test case into sub-tasks ("steps"), and the only way to follow them today is the raw execution log. The case page (`/projects/<id>/test-runs/<run>/cases/<pivot>/`) should get a section that shows the plan, which step is running, and for each step its status (pending, running, passed, failed, interrupted, not run), its attempts and recoveries, and a short result. It updates live over the case WebSocket while the case runs. It is persisted, so it still shows after the run and survives Resume. (2) Results sent to TestRail (the "Send checked" push to the source run and the 018 "Push to TestRail") should include a per-step breakdown: step number, step text, status and short result. Today a comment can say "Steps 4–7 were not run" and the reader has no way to know what steps 4–7 were. Research whether TestRail's `custom_step_results` field should carry this for step-template cases, with plain comment text as the fallback.

## Context

- **What "Steps 4–7" means.** The numbers are the orchestrator's **sub-task** numbers, not TestRail step numbers (confirmed from code):
  - `agents/services/orchestrator._plan_sub_tasks` has the LLM decompose the case into up to `ORCHESTRATOR_MAX_SUBTASKS` (30) sub-tasks. `orchestrator_prompts.build_plan_system_prompt` tells it to split compound steps, add implicit steps, and skip preconditions already met. So sub-task N rarely equals TestRail step N.
  - `_build_verdict` → `build_verdict_prompt` → `format_attempt_lines` feeds lines such as `Steps 4-7: not run` to the LLM. The LLM copies that wording into `pivot.result`, and `pivot_result_text(pivot)` sends it to TestRail verbatim. The sub-task descriptions never leave the server: they exist only in the log and in the checkpoint.
- **The checkpoint already holds most of the panel's data** (`docs/changed/2026-10-22-resume-blocked-case.md`):
  - `agents/types.OrchestratorCheckpoint(plan, attempts, next_step, browser, total_iterations)`.
  - `SubTaskAttempt(step_num, kind step|recovery|resume, result: SubTaskResult(status, summary, iterations, error))`.
  - It is written by `_Progress.checkpoint()` in `orchestrator.py`:
    - after planning;
    - before each step;
    - after each attempt;
    - after each decision (continue/stop);
    - after a recovery.
  - It is serialized by `agents/checkpoint.checkpoint_to_json` (`CHECKPOINT_VERSION = 1`). The strict parser `_parse` reads keys with `.get` and ignores unknown keys.
  - It is stored through `projects/case_checkpoint_services.build_checkpoint_callback(pivot, generation)`, a generation-filtered UPDATE of `TestRunTestCase.execution_checkpoint`, which also sets the in-memory `pivot.execution_checkpoint`.
  - Resume keeps it and `truncate_checkpoint` drops attempts at or after `from_step` (`projects/run_resume_services._choose_resume_step`). `reset_test_run` clears it.
- **What the checkpoint lacks for the panel:**
  - **What is happening now.** After a failed step it sits in the same state while the LLM evaluates the failure, and again while a recovery sub-agent runs. A recovery is only checkpointed after it finishes. The resume-restore sub-task is not checkpointed before it starts.
  - **Why the case stopped or moved on.** `OrchestratorDecision.reason` (continue/stop/recover) is logged only.
  - **Durations and start times.**
  - **Which TestRail step a sub-task implements** (needed only for Part C).
- **Case page:**
  - View: `projects/views.test_run_case_detail`, which loads the pivot through `services.get_test_run_case_detail`, all fields included.
  - Template: `templates/projects/test_run_case_detail.html`. Its Alpine root `testRunCaseLive` is defined inline in `extra_scripts`. It has the cards Result, Why blocked, Cancelled, Resume history, Test case, Screenshots and Execution log.
  - WebSocket: the case page uses **`TestRunCaseConsumer`** (group `test_run_case_<pivot_id>`, `/ws/projects/<id>/test-runs/cases/<pivot_id>/`), not `TestRunConsumer`, which serves the run page. Its events:
    - `current_state` (status, result, blocked_reason(_label), logs, screenshots);
    - `log`, `screenshot` and `status`, sent from `projects/services._broadcast_log`, `_broadcast_screenshot` and `_broadcast_pivot_status_to_case`.
- **TestRail comments:**
  - `projects/testrail_mapping.build_result_comment(outcome_label, run_name, result_text, case_url)` is used by the 018 push (`testrail_results_services._plan_pivot`).
  - `build_checked_result_comment(...)` wraps it for the checked push (`testrail_checked_push_services._plan_pivot`). That push loads pivots with `.only(*_PLAN_PIVOT_FIELDS)`, which does not include `execution_checkpoint`.
  - The 018 push detects changes with `testrail_pushed_comment_hash` (`_is_changed`).
  - `TestRailClient.add_results_for_cases` sends only `case_id`, `status_id`, `comment` and `elapsed` (`_result_input_payload`).
- **TestRail steps are not stored structurally.**
  - `TestRailFieldMapper.to_test_case_data` flattens `custom_steps_separated` into `"1. …\n\n2. …"` text (`flatten_steps`) and sets `template = "Test Case (Steps)"`.
  - `TestRailCase.template_id` is parsed but not stored.
  - Step content can itself contain blank lines, so re-parsing the text is unreliable.
  - Users can also edit `TestCase.steps` freely in the edit dialog.
- **Design system:** `static/css/punk-hazard.css` already has these, which the panel can reuse:
  - `.badge-passed/-failed/-blocked/-running/-draft/-cancelled`;
  - `.checklist` and `.check-icon.is-pass/-fail/-pending`;
  - `.activity`;
  - `.live-dot`, `.pre-wrap`, `.hint`, `.muted`.
  - `.steps`/`.step` is the horizontal onboarding wizard stepper and does not fit a vertical list of up to 30 rows.
- Versions: Django ~=5.2.11, channels ~=4.3.2, celery ~=5.6.2, httpx ~=0.28.1, Alpine.js 3.14.9 (CDN).

## Goals

- A **Steps** card on the case page lists every planned sub-task, with:
  - the number, description and expected result;
  - its status, and which step is current and what it is doing (running, checking a failure, recovering, restoring state);
  - its attempts and recoveries, its final result summary and its error;
  - the orchestrator's decision reason when a step failed and the case continued or stopped;
  - the step duration.
- It updates live over `TestRunCaseConsumer`. On reload it renders from the persisted checkpoint, and it survives Resume.
- Both TestRail pushes include a readable per-step breakdown in the result comment, using the same numbering as the summary text. "Steps 4–7 were not run" then always comes with what steps 4–7 were, and why they were not run.
- The verdict summary names steps instead of using bare numbers, e.g. "Step 3 (run the production build)".
- A single derivation of step status is shared by the panel, the WebSocket and TestRail, so the three never disagree.

## Non-goals

- No new model or table. The checkpoint stays the single persisted source.
- No step progress on the run detail page, such as "step 3/7" per running row (see Q5).
- No change to how the verdict is decided (`_build_verdict` and `final_step_indices` are unchanged).
- No "skipped" status. The orchestrator never skips a planned sub-task, and preconditions it decides are already met are simply not planned. A "skipped" state only appears in Part C, for TestRail steps no sub-task covered.
- No history of attempts that a Resume truncated. That outcome is already in Resume history (`TestRunCaseResume.previous_result`).
- No controller protocol change, no setting and no Celery queue change.

## Design

### Data model

There is no migration for Parts A and B. The `execution_checkpoint` JSON gains **optional, additive** keys:

| Key | Shape | Written when |
|---|---|---|
| `attempts[].duration_seconds` | int \| null | after each sub-agent returns (wall clock, `time.monotonic` delta) |
| `decisions` | list of `{step_num, action: continue\|recover\|stop, reason}` | after `_evaluate_failure`, and for the synthetic stops ("max recovery attempts reached", "recovery requested but no task", "recovery failed") |
| `phase` | `step` \| `evaluating` \| `recovery` \| `resume` \| `verdict` | each checkpoint |
| `phase_started_at` | ISO-8601 UTC string | when the phase changes |

- `CHECKPOINT_VERSION` **stays 1**:
  - The keys are optional, and the parser defaults them when they are missing, so v1 checkpoints already stored keep working.
  - The parser that is deployed today ignores unknown keys, so a rollback keeps resumable checkpoints.
  - A version bump would make old code drop every checkpoint, so every case would resume from step 1. This decision is recorded in `agents/checkpoint.py`'s module docstring.
- Types in `agents/types.py`:
  - `SubTaskAttempt` gains `duration_seconds: int | None = None`.
  - New frozen `StepDecision(step_num, action, reason)`.
  - New `OrchestratorPhase = Literal[...]`.
  - `OrchestratorCheckpoint` gains `decisions: tuple[StepDecision, ...] = ()`, `phase: OrchestratorPhase = "step"` and `phase_started_at: datetime | None = None`.
- `truncate_checkpoint` also drops `decisions` with `step_num >= from_step`.
- `_rebuild_state_lines` and resume behaviour are unchanged; the decisions are display-only.
- **Part C only** (gated on Q1/Q2), migration `0024_testcase_testrail_steps`, additive:
  - `TestCase.testrail_steps` (JSON list of `{content, expected}`, default `[]`);
  - `TestCase.testrail_template_id` (PositiveInteger, null).
  - Both are filled on TestRail import and cleared when a user edits `steps` in the app (Q7).
  - `SubTask` gains `case_steps: tuple[int, ...] = ()`, persisted as an optional key `plan[].case_steps`.

### Services

- **`agents/services/orchestrator.py`** (it stays ORM-free and keeps its behaviour):
  - `_Progress` gains `decisions`, `phase` and `phase_started_at`, plus a `set_phase(phase)` helper that stamps the time and checkpoints.
  - New checkpoint points:
    - before the resume restore (`resume`);
    - before `_evaluate_failure` (`evaluating`), replacing today's bare checkpoint after a failed step;
    - before the recovery sub-agent starts (`recovery`);
    - before `_build_verdict` (`verdict`).
  - Durations are measured around each `run_sub_agent` call.
  - Each decision, including the synthetic stop reasons in `_attempt_recovery`, is appended to `progress.decisions`.
  - This adds about 2 small UPDATEs per failed step and 1 per case.
- **`agents/services/orchestrator_prompts.py`:** `build_verdict_prompt` gains one rule. When it mentions steps, the summary names each one briefly next to its number, for example "Steps 4–7 (open the app, …) were not run". `format_attempt_lines` passes each step's short description, so the LLM can do this. The function then needs the plan (`plan: Sequence[SubTask]`) as an extra argument, and `_deterministic_summary` gets the same.
- **`projects/case_step_progress.py`** (new, pure, no ORM; imports only `agents.checkpoint`, `agents.types` and `projects.models` enums). This is the single place that turns a checkpoint plus the case status into display rows:
  - `StepStatus(StrEnum)`: `pending`, `running`, `passed`, `failed`, `interrupted`, `not_run`.
  - `StepActivity`: `""`, `running`, `evaluating`, `recovering`, `restoring`, set only on the current row while the case is In progress.
  - `StepAttemptRow(label, status, summary, error, duration_seconds)`. The labels reuse the wording in `format_attempt_lines`: "Attempt 2", "Recovery", "State restored before this step".
  - `CaseStepRow(num, description, expected_result, status, activity, summary, error, attempt_count, recovery_count, duration_seconds, decision: StepDecision | None, history: tuple[StepAttemptRow, ...])`.
  - `CaseStepProgress(rows, current_step, phase_label, phase_started_at, passed, failed, not_run, total, resumed)`.
  - `build_case_step_progress(checkpoint_data: object, case_status: str, case_started_at: datetime | None) -> CaseStepProgress | None`. It returns `None` when there is no usable checkpoint.
  - The row status rules, with `c = next_step` and "final" meaning the step's last `kind="step"` attempt (`final_step_indices`):
    1. A final attempt passed → `passed`.
    2. A final attempt failed and `k < c` → `failed`. The row shows the `continue`/`stop` decision reason.
    3. `k == c` and the case is In progress → `running`, with an activity from `phase`: `step`→running, `evaluating`, `recovery`→recovering, `resume`→restoring.
    4. `k == c` and the case is Blocked or Cancelled → `interrupted`.
    5. No final attempt, and the case is In progress or Created → `pending`. Otherwise → `not_run`.
    6. A stale phase (`phase_started_at < case_started_at`, i.e. a resumed case still waiting for the runner) shows the label "Starting" instead.
  - `step_progress_payload(progress: CaseStepProgress | None, *, planning: bool) -> StepProgressPayload` builds a `TypedDict` that is JSON-safe for WebSocket and `json_script`. `planning` is True when the case is In progress with no checkpoint, so the card shows "Planning the steps…".
- **`projects/case_checkpoint_services.build_checkpoint_callback(pivot, generation, on_saved: Callable[[dict[str, object]], None] | None = None)`:**
  - It calls `on_saved(data)` only when the generation-filtered UPDATE changed a row, so a stale worker never broadcasts.
  - The callback is injected because this module cannot import `projects.services`: `services` imports it, and that would be a cycle.
- **`projects/services.py`:**
  - New `_broadcast_case_steps(pivot, checkpoint_data)` sends group event `test_run_case.steps` with the payload computed for `case_status=in_progress`.
  - `execute_test_run_test_case` passes it as `on_saved`.
  - `_broadcast_pivot_status_to_case` adds `steps` to `CaseStatusEvent`, computed from `pivot.execution_checkpoint` and the new status. Every caller already holds a fully loaded pivot: the worker's pivot, `abort_test_run`'s pivots and `broadcast_run_and_pivots` from Resume, so there are no extra queries. This lets the client flip `pending` to `not_run` and `running` to `interrupted` when the case ends.
- **`projects/testrail_mapping.py`** (pure):
  - New `format_step_breakdown(progress: CaseStepProgress) -> str`.
  - `build_result_comment` and `build_checked_result_comment` gain `step_breakdown: str` (empty means none), placed between the result text and the Details line.
  - The format is plain lines, with no characters that TestRail's Markdown renderer would reinterpret (no `[x]`, no leading `#`):

    ```
    Steps planned by the agent (7):
    1. Passed: Check that Sencha Cmd is installed — Sencha Cmd v8.1.0.50 reported.
    3. Failed: In Terminal, run the production build … — Exit status 65: Bad CPU type in executable.
       Stopped here: the bundled Node binary cannot run on this CPU.
    4. Not run: In Finder, open the build folder …
    ```

  - Caps: description 160 chars, result 200 chars, the decision reason 200 chars, and the whole block 6000 chars. If the block is over the limit, the trailing rows are collapsed into "… and N more steps, see Details".
- **`projects/testrail_results_services.py` and `projects/testrail_checked_push_services.py`:**
  - The `_plan_pivot` functions build the progress from `pivot.execution_checkpoint` and pass the breakdown.
  - `_PLAN_PIVOT_FIELDS` adds `"execution_checkpoint"`. The 018 plan already loads full pivots.
  - There are no new queries.
- **Part C** (only if Q1/Q2 say yes):
  - `TestRailClient.get_result_fields()`.
  - `TestRailResultInput.step_results: tuple[TestRailStepResult, ...] = ()`, serialized as `custom_step_results: [{content, expected, actual, status_id?}]`.
  - New pure `projects/testrail_step_results.py` maps the TestRail steps to statuses through `SubTask.case_steps`:
    - any covering sub-task's final attempt failed → Failed (5);
    - the case is Blocked and a covering step is at or after the cursor → Blocked (2);
    - all covering sub-tasks passed → Passed (1);
    - covered but not run → no `status_id` (Untested; verify);
    - not covered by any sub-task → no `status_id`, with `actual` set to "Not exercised by the agent".
  - Step results are sent only when the case has `testrail_steps`, and `get_result_fields` (fetched once per push) shows the `step_results` field active for the case's project and `testrail_template_id`. Otherwise the result carries the comment only.
  - The planner prompt asks each sub-task for `"case_steps": [n, …]`, the test-case step numbers it implements. The parse is lenient: anything that is not a list of positive ints becomes `()`.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_detail` `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/` (existing) | GET | `@project_membership_required` (unchanged) | none | `test_run_case_detail.html`. New context: `step_progress` (`StepProgressPayload`, serialized with `json_script` as `initial-steps`). | 404 unchanged |

- The new **Steps** card sits between "Result" and "Why this case was blocked". Its parts:
  - **Header:** a live dot while the case runs; "Steps" with "3 of 7" or "5 passed · 1 failed · 1 not run"; the current phase label and "since HH:MM:SS".
  - **Body:** an ordered list with one row per planned step. Each row has a number bubble, the description, a status badge (existing `.badge-*` classes), and a muted line with the expected result and the duration. It also shows the result summary, plus the error and the decision reason when present.
  - **Attempts:** a disclosure (`<details>`) "Attempts (3)" when there was more than one attempt, a recovery, or a restore.
- **Empty states:**
  - no checkpoint and not running: "No step breakdown was recorded for this execution" (cases run before 2026-10-22, or blocked before planning finished);
  - running with no checkpoint: "Planning the steps…".
- New CSS goes in `punk-hazard.css` as a `.step-list` component: `.step-list`, `.step-row`, `.step-num`, `.step-row.is-current`, `.step-meta` and `.step-attempts`. It reuses the tokens and the `.check-icon` tones. It must not reuse the wizard's `.steps`/`.step`.
- **XSS:** descriptions, summaries, errors and reasons are LLM output that may quote page content. They are rendered only with `x-text` / `{{ }}`, never with `x-html` or `|safe`. The initial data goes through `json_script`.

### Background work and WebSockets

- No new Celery task or queue. Checkpoint writes stay in the `execution` worker.
- New group event `test_run_case.steps` with `{"type", "steps": StepProgressPayload}`. `TestRunCaseConsumer.test_run_case_steps` relays it to the client as `{"type": "steps", "steps": …}`.
- `test_run_case.status` and the client message `status` gain `steps`.
- `current_state` gains `steps`. It is computed in `fetch_test_case_state`'s caller from the pivot it already loads, so there are no extra queries.
- `testRunCaseLive` gains `steps`, initialised from `initial-steps` and replaced wholesale on `current_state`, `steps` and `status`. The client does not derive anything.
- Payload size: at most 30 rows with capped text (each summary and error capped at 1000 chars in the payload), so well under 100 KB, and only on checkpoint changes (a few per step).
- No controller actions or protocol change: `CLIENT_VERSION` and the capabilities are unchanged.

### Settings / infrastructure

None. Deploy means restarting the `execution` workers, Daphne/web and the `upload` worker. A step-results field added for Part C needs `migrate`.

### Data flow

1. The worker calls `run_orchestrator(..., on_checkpoint=build_checkpoint_callback(pivot, generation, on_saved=_broadcast_case_steps-bound))`.
2. The orchestrator plans, then checkpoints at phase changes, attempts and decisions.
3. The callback runs a generation-filtered UPDATE of `execution_checkpoint`. If it changed a row, it broadcasts `test_run_case.steps` with `build_case_step_progress(data, "in_progress", started_at)`.
4. The consumer relays the event, and Alpine re-renders the Steps card.
5. When the case ends, `_finalize_pivot` / `_mark_pivot_blocked` / `_mark_pivot_cancelled` broadcast `status` with the final `steps`, so pending rows become Not run and the current row becomes Interrupted.
6. On page load, the view computes the same payload from the stored checkpoint and status.
7. On a TestRail push, each pivot's checkpoint becomes a `CaseStepProgress`, then `format_step_breakdown`, then the comment (plus `custom_step_results` in Part C).

## Alternatives considered

- **A dedicated `TestRunCaseStep` table**, one row per step:
  - It is queryable and could hold timestamps.
  - It duplicates the checkpoint, needs the same generation filtering and truncation on Resume, and adds a write path the orchestrator would have to keep in sync.
  - The checkpoint is already written at exactly the right moments, and is already generation-safe and Resume-aware. Rejected.
- **Deriving status in the browser** from raw checkpoint data:
  - It would put a second copy of the status rules in JS, and TestRail would need them in Python anyway. Rejected.
- **Parsing the log:** fragile, and rejected by the resume plan for the same reason.
- **Bumping `CHECKPOINT_VERSION`:**
  - It would discard every stored checkpoint on rollback.
  - The changes are additive and the parser is lenient about missing keys. Rejected.
- **Renumbering the summary to TestRail steps:**
  - That is impossible without the Part C mapping, because sub-tasks don't map 1:1 to TestRail steps.
  - Listing the sub-tasks in the comment fixes the reported confusion for every case, including free-text and manually created cases. Part C adds TestRail-native step results on top.
- **`custom_step_results` only, without comment text:**
  - It works only for step-template cases where the project has the Step Results field enabled.
  - The comment breakdown is always present. Step results are an addition, not a replacement.

## Risks and mitigations

- **018 re-push duplicates.** Adding the breakdown changes the comment hash of every pivot pushed before the deploy. The panel's pending count then shows those cases as changed, and pushing again adds a second result per case in TestRail.
  - Mitigation: the confirm dialog already shows the count, and the new comment really is more informative.
  - Alternative: hash only the pre-change fields for pivots pushed before the deploy. That is more code and more state. See Q3.
- **Stale phase during resume pickup.** The rule 6 `started_at` comparison shows "Starting" instead.
- **Extra UPDATEs and broadcasts.** About 3 more per failed step. They are tiny compared with the per-step LLM and screenshot traffic.
- **A stale worker broadcasting old progress.** `on_saved` fires only when the generation-filtered UPDATE hit a row.
- **XSS from LLM text.** Use only `x-text` and `json_script`, and the verifier must check this.
- **Comment length.** The breakdown is capped at 6000 chars. TestRail's comment field is large, but this keeps results readable.
- **Markdown rendering in TestRail.** Lines like `1. Passed: …` render as an ordered list, which is acceptable. Verify on the live instance (step 8 below).
- **Part C:**
  - `custom_step_results` keys (`content`, `expected`, `actual`, `status_id`) come from the TestRail Results API docs.
  - Not confirmed: whether a step may omit `status_id` (shown as Untested), what TestRail does when the field is not configured for the template, and the exact shape of `get_result_fields` `configs` and `template_ids`.
  - All three must be verified against the live instance before Part C ships. If the field is not active, the client sends nothing.
  - LLM `case_steps` tags can be wrong. They are clamped to `1..len(testrail_steps)`, and "Not exercised" is used for uncovered steps.
- **Checkpoint written while the orchestrator raises.** The phase stays at the interrupted activity, and rule 4 shows the row as Interrupted. The panel is correct without extra handling.

## Implementation steps

Steps 1 and 2 are the shared base and go first. Steps 3, 4 and 5 can then run as parallel sub-agents, because they touch disjoint files. Step 6 is the orchestrator's integration pass.

1. **Checkpoint shape** (files: `agents/types.py`, `agents/checkpoint.py`):
   - add `StepDecision` and `OrchestratorPhase`;
   - add the optional fields on `SubTaskAttempt` and `OrchestratorCheckpoint`;
   - serialize and parse the new keys with defaults, keeping version 1;
   - make `truncate_checkpoint` drop later decisions;
   - add a docstring note on the additive-keys rule.
2. **Pure derivation** (files: `projects/case_step_progress.py` new): `StepStatus`, the row and progress dataclasses, `build_case_step_progress`, `StepProgressPayload` and `step_progress_payload`.
3. **Orchestrator** (files: `agents/services/orchestrator.py`, `agents/services/orchestrator_prompts.py`):
   - phases, durations and decisions on `_Progress`;
   - the new checkpoint points;
   - the verdict prompt rule to name steps, with `format_attempt_lines` / `_deterministic_summary` taking the plan.
4. **Live panel** (files: `projects/case_checkpoint_services.py`, `projects/services.py` (broadcast helpers, `execute_test_run_test_case` wiring, `CaseStatusEvent`), `projects/consumers.py`, `projects/views.py` (`test_run_case_detail` context), `templates/projects/test_run_case_detail.html` (card, context-contract comment, `testRunCaseLive`), `static/css/punk-hazard.css` (`.step-list` component)).
5. **TestRail comments** (files: `projects/testrail_mapping.py`, `projects/testrail_results_services.py`, `projects/testrail_checked_push_services.py`): `format_step_breakdown`, the new comment parameter in both builders, and `_PLAN_PIVOT_FIELDS`.
6. **Integration:** check the seams (the `on_saved` signature, the payload keys used by the consumer, the template and the event), then run `pre-commit run --all-files`, `manage.py check` and `makemigrations --check`.
7. **Part C** (only after Q1/Q2), as its own reviewable step (files: `projects/models.py` + migration `0024`, `projects/testrail_mapping.py` (`to_test_case_data` keeps the structured steps), `projects/testrail_services.py` (upsert writes them), `projects/services.update_test_case` (clears them on a steps edit), `projects/testrail_client.py` (`get_result_fields`, `step_results` payload), new `projects/testrail_step_results.py`, both push services, `agents/types.py` / `agents/checkpoint.py` (`SubTask.case_steps`), `orchestrator.py` / `orchestrator_prompts.py` (planner output and parse), the case page row chip "TestRail step 2, 3").

## Verification

There are no automated tests in this project. Prove the change with these steps:

1. Run `pre-commit run --all-files` (black, isort, mypy strict for the server and for `controller_client/`), `python manage.py check` and `python manage.py makemigrations --check` (no migration for Parts A and B).
2. **Checkpoint round-trip:** in a `manage.py shell -c` smoke script, check that:
   - a v1 checkpoint without the new keys parses, and the new keys default;
   - a new checkpoint round-trips;
   - `truncate_checkpoint` drops decisions.
   - Also feed `build_case_step_progress` hand-built checkpoints for:
     - all pass;
     - a failure followed by continue;
     - a failure, recovery and pass on retry;
     - a stop, which leaves the tail Not run;
     - blocked at the cursor (Interrupted);
     - cancelled;
     - verdict-only resume;
     - a stale phase.
3. **Live run with a connected runner:** open the case page, start the run, and confirm that:
   - "Planning the steps…" appears, then the list;
   - the current row shows Running with the live dot;
   - a failed step shows "Checking the failure", then Recovering;
   - rows flip to Passed/Failed without a reload, and the log still streams.
4. **Finished case:** reload the page and confirm the same rows render from the database, and that rows that were not run show Not run.
5. **Block mid-case:** stop the runner to block the case, and check the current row shows Interrupted. Then Resume from that step and check that:
   - earlier rows keep their results;
   - the row's Attempts disclosure shows "State restored before this step";
   - the panel continues live.
   - Resume from an earlier step and check that the later rows reset to Pending.
6. **Reset the run:** the Steps card shows the empty state.
7. **Old case** (empty checkpoint): the card shows the empty state, and the page has no errors.
8. **TestRail, checked push:** on a failed case that stopped early, check the TestRail result comment shows:
   - the summary, then "Steps planned by the agent (N)";
   - every step with its status and text, and the stop reason;
   - then Details, "Checked by" and the screenshots line.
   - Check that it renders legibly in TestRail.
9. **TestRail, 018 push:** the same breakdown appears. Note the pending-count change for runs pushed before the deploy (Q3).
10. **XSS:** run a case whose sub-task summary contains `<img src=x onerror=alert(1)>` (for example, a page title). It renders as text in the card.
11. **Part C, if built:** a step-template case gets Step Results in TestRail when the field is enabled. With the field disabled, only the comment is sent. Probe how `status_id` is omitted on one result first.
12. Run the `reviewer` and `verifier` agents in parallel on the diff, giving the verifier the original request and this plan.

## Open questions

- **Q1. Agent steps or TestRail steps?** The "Steps 4–7" in summaries are the agent's own sub-tasks, which split, merge and add to the TestRail steps. Is it enough for this change that the panel and the TestRail comment list the **agent's steps** (with their text, status, result, and why the rest were not run)? Or do you need results mapped back onto the **TestRail case steps** now (Part C: the planner tags each sub-task with the TestRail step numbers it covers)? *Assumption in this draft: Parts A and B now; Part C is a follow-up step, built only if you say so.*
- **Q2. `custom_step_results`.** If Part C is built: send TestRail's step-by-step results only for cases imported with the "Test Case (Steps)" template, and only when the project's Step Results result field is enabled for that template, and fall back to comment text otherwise? Steps the agent never reached would be sent as Untested (no status). *Assumption: yes. The comment breakdown is always included as well.*
- **Q3. Re-push after deploy.** The 018 "Push to TestRail" sees a changed comment for every case already pushed, so pushing such a run again adds a second result per case (with the breakdown). Is that acceptable? *Assumption: yes. The dialog shows the count, and the new comment is more useful.*
- **Q4. Comment length.** List every agent step (up to 30) with the result trimmed to about 200 characters and the description to about 160? Or list only failed, interrupted and not-run steps plus a passed count? *Assumption: every step, capped at 6000 characters in total.*
- **Q5. Run page.** Should the run detail table also show live "step 3 of 7" for the running case? *Assumption: no, the case page only.*
- **Q6. Attempt detail.** One row per planned step, with earlier attempts, recoveries and state restores folded into an "Attempts" disclosure under the row, rather than one row per attempt? *Assumption: yes.*
- **Q7. Editing steps (Part C only).** If someone edits a TestRail-imported case's steps in the app, its structured TestRail steps no longer match. Should we clear them, so the case falls back to comment-only results until it is re-imported? *Assumption: clear them.*

## Changelog

- 2026-10-23: created (Draft).
- 2026-10-23: user answers. Q1: agent steps only, Part C dropped (not built; Q2/Q7 moot). Q3: re-push duplicates acceptable. Q4: every step, 6000-char cap. Q5: case page only. Q6: default (one row per step, attempts folded). Status In progress.
- 2026-10-23: review pass 1 fixes: client ignores late `steps` events once the case is no longer in progress (Abort does not bump the generation); display-only checkpoint keys parse leniently; `pivot_step_payload` lives in `projects/case_step_progress.py`, not legacy `services.py`; step-list CSS scoped under `.step-list`; inline style replaced by `.card-header-count`. Pass 2 clean. Status Done.
