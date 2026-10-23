# feat: Re-run selected cases in place on the same run

Status: Done
Created: 2026-10-23
Request: "Ability to re run some tests in a run without creating new run. I want to be able to check test cases in a run, and start running them again (keep the order as it is on the run)". On a finished run's page, a member selects some cases with the existing checkboxes and starts them again on the **same run**, on the same case rows. The selected cases run from step 1, one after the other, in the run's order (TestRail order) restricted to the selection. Cases that are not selected keep their results. The existing "Re-run failed" / "Re-run blocked in a new run" actions, which create a new run, stay.

## Context

- **Resume in place** (`docs/changed/2026-10-22-resume-blocked-case.md`) is the closest mechanism. `projects/run_resume_services.py`:
  - `_lock_and_check` locks the project and then the run, and applies the gates: run Done/Cancelled, no other run Started, no checked send live (`is_checked_push_live`), no 018 push PUSHING, runner connected and visual engine ready (`get_machine_state`), no live machine-setup turn (`machine_setup.turn_state.live_turn_exists`).
  - `_resume_pivots` snapshots each case into `TestRunCaseResume`, resets status/result/Checked/Sent with `bulk_update`, sets the run to STARTED, bumps `TestRun.execution_generation`, and calls `queue_test_run_cases` + `broadcast_run_and_pivots` in `on_commit`.
  - A resumed case keeps its log, screenshots and `execution_checkpoint`. The worker (`execute_test_run_test_case`, `projects/services.py`) continues from the checkpoint when `load_resume_checkpoint(pivot)` returns one, and plans from scratch when the checkpoint is `{}`.
  - `get_resume_panel` gives the run page its counts, gates and blocker text.
- **Run order** (`docs/changed/2026-10-22-testrail-case-order.md`): `projects/run_case_order.RUN_CASE_ORDERING`. `start_test_run` and `_lock_and_check` use it to build the chain. The run page lists **failures first**, then this order (`list_test_run_cases`).
- **Selection UI** (`docs/changed/2026-10-22-testrail-checked-results.md`):
  - `templates/projects/test_run_detail.html` has a checkbox column and a bulk bar, scoped by `runCaseChecks(checkIds, uncheckIds)` (`static/js/punk-hazard.js`, built on `bulkSelectState`).
  - A row's checkbox is enabled only when the pivot is checkable or uncheckable. Sent and Cancelled rows are disabled.
  - The bulk bar's Check and Uncheck forms post `pivot_ids` and `next` to `test_run_cases_check` / `test_run_cases_uncheck`. These are validated by `RunCaseSelectionForm(allowed_ids, data)` (`projects/forms.py`, max `RUN_CASE_SELECTION_MAX_IDS` = 500) in `_change_run_case_checks` (`projects/views.py`).
  - Selection is per page (the table is paginated).
- **Re-run into a new run** (`docs/changed/2026-10-22-blocked-result.md`): `rerun_cases(test_run, RerunScope)` in `projects/services.py`. Unchanged by this plan.
- **Evidence of an execution**:
  - `TestRunTestCase.logs` is appended by `_append_pivot_log` with a generation-filtered UPDATE.
  - `TestRunScreenshot` rows are files under `screenshots/trtc_<pivot_id>/`.
  - `execution_checkpoint` drives the case page's Steps card (`case_step_progress`) and the step breakdown in TestRail comments.
  - "Send checked" attaches **every** non-overlay screenshot of the pivot (`testrail_checked_push_services`, about line 123).
  - `reset_test_run` deletes screenshot rows and files, clears logs and the checkpoint, and deletes `TestRunCaseResume` rows.
- **Run completion**: `_update_test_run_status_if_needed` sets the run to DONE once no case is CREATED or IN_PROGRESS. `abort_test_run` cancels CREATED and IN_PROGRESS cases only. So an aborted re-run leaves the cases that were not selected untouched.
- **Live pages**:
  - Run page `testRunLive` reloads when the run reaches done or cancelled.
  - Case page `testRunCaseLive` updates on `status`/`log`/`screenshot`/`steps` events. It keeps the screenshots it has already received in client state.
- **018 whole-run push** dedupes on `testrail_pushed_comment_hash` (`testrail_results_services._is_changed`). A new verdict after a re-run is automatically pending again.
- Versions (`requirements.txt`): Django ~=5.2.11, channels ~=4.3.2, celery ~=5.6.2, Alpine.js 3.14. Django docs (5.2 QuerySet API): `update()` and `bulk_update()` call no `save()`, so `auto_now` (`updated_at`) must be set explicitly, as Resume already does.
- Latest migration: `projects/0024_application_context_history`.

## Goals
- On a Done or Cancelled run, select any cases (every status) on the run page and re-run them **on the same run**, from step 1.
- Execution order: `RUN_CASE_ORDERING` restricted to the selected cases.
- The same safety gates as Resume, enforced on the server under the same locks.
- Stale workers stand down (generation bump). The run goes back to Started and returns to Done/Cancelled through the normal path. The counters and the live pages update.
- The replaced outcome is not lost silently: who re-ran it, when, and the previous status, blocked reason, verdict text and finish time are kept in the case's history.
- Checked/Sent marks of the re-run cases are cleared, so the new verdict can be checked and sent.

## Non-goals
- Re-running cases while the run is still Started (appending to a live chain).
- Selecting across pages. The selection stays per page, as for Check/Uncheck. Per-page sizes already go up to the largest `ALLOWED_PER_PAGE`.
- A "Re-run this case" button on the case page. This is a cheap follow-up that reuses the same URL with one `pivot_ids`.
- Archiving the previous attempt's screenshots or step checkpoint (see Q1).
- Changing "Resume run", "Re-run failed", "Re-run blocked in a new run" or Reset behaviour. Only one label changes (step 6).
- Any controller protocol, Celery task signature, WebSocket event shape or setting change.

## Design

### Data model
- `TestRunCaseResume` gains `kind = CharField(max_length=10, choices=CaseRestartKind.choices, default=CaseRestartKind.RESUME)`.
  - New `CaseRestartKind(TextChoices)` in `projects/models.py`: `RESUME = "resume", "Resumed"` and `RERUN = "rerun", "Re-run"`.
  - The existing rows become `resume`. No backfill is needed.
- A re-run history row has `kind=rerun` and `from_step=None`. The `previous_*` fields are filled exactly as Resume fills them.
- Migration `0025_case_restart_kind`: one `AddField` with a default. Additive and cheap: Postgres 11+ adds a column with a constant default without rewriting the table.
- The model name is kept. Renaming `TestRunCaseResume` to something like `TestRunCaseAttempt` would touch the resume code, `reset_test_run` and the templates for no behavioural gain. Its docstring becomes "Snapshot of a case's outcome taken each time it is resumed or re-run in place".

### Services
1. **New `projects/run_reopen_services.py`**: the shared "put a finished run back to work" mechanics, extracted from `run_resume_services` so that resume and re-run cannot drift apart.
   - `REOPENABLE_RUN_STATUSES` holds Done and Cancelled.
   - `lock_project_and_run(test_run_id) -> tuple[Project, TestRun]` uses the project-then-run lock order. Callers must be inside `transaction.atomic`.
   - `list_reopen_blockers(test_run, project) -> list[str]` returns user-safe texts, in this order: another run Started, checked send live, 018 push PUSHING, machine not ready or visual engine failed, live setup turn. It is the same logic and the same texts as today's `_has_other_started_run`, `_push_blockers`, `_machine_blocker` and `live_turn_exists`, moved here. It does not check the run status; each caller does that with its own message.
   - `reopen_run(test_run, pivots) -> None` must run inside the atomic block, under the run lock. It sets the run to STARTED with `finished_at=None` and bumps `execution_generation`, then saves. In `on_commit` it calls `queue_test_run_cases(test_run, [p.pk for p in pivots])` and, on success, `broadcast_run_and_pivots(test_run, pivots)`. `pivots` must already be in run order and carry their reset in-memory values.
   - `run_resume_services` is refactored to call these three. Its behaviour, texts and query count stay the same.
2. **New `projects/run_rerun_services.py`** (the use case):
   - Every case of a Done or Cancelled run is re-runnable, whatever its status (Q3: "everything"), including a CREATED or IN_PROGRESS row left behind by a hard-killed worker (the generation bump makes that worker stand down).
   - `is_rerunnable(pivot) -> bool` is not needed: on a reopenable run every row is selectable.
   - `list_rerunnable_pivot_ids(test_run) -> list[int]`: one query. It supplies the form's allowed ids, and is empty unless the run is Done or Cancelled.
   - `RerunPanel(can_rerun: bool, blockers: tuple[str, ...])` and `get_rerun_panel(test_run, project) -> RerunPanel`:
     - when the run is not Done or Cancelled, it returns `(False, ())` and the button is hidden;
     - otherwise it returns `list_reopen_blockers`, which costs 2 queries.
   - `RerunOutcome(queued: int, skipped: int)`.
   - `rerun_test_run_cases(*, test_run: TestRun, pivot_ids: Sequence[int], user: CustomUser) -> RerunOutcome`. It runs in one `transaction.atomic`:
     1. `lock_project_and_run`. Refuse with `RunRerunError` if the run is not Done or Cancelled, or with the first blocker from `list_reopen_blockers`.
     2. Load the pivots of this run with `id__in=pivot_ids`, with `.defer("logs")` and `.order_by(*RUN_CASE_ORDERING)`. Filtering on `test_run_id` is the IDOR guard. If none match, raise `RunRerunError`.
     3. `bulk_create` one `TestRunCaseResume(kind=RERUN, resumed_by=user, from_step=None, previous_*)` per pivot.
     4. Collect the screenshot file names of these pivots (one `values_list` query), then delete the screenshot rows (one DELETE).
     5. Run one queryset `UPDATE` over the selected ids. It sets status CREATED, `blocked_reason=""`, `result=""`, `started_at=None`, `finished_at=None` and `execution_checkpoint={}`, clears every Checked/Sent field that Resume clears, and sets `updated_at=now`. It also sets `logs=Concat(F("logs"), Value(RERUN_LOG_SEPARATOR))`, where the separator is the constant `"=== Re-run from the start ===\n"` and contains no user text. The reset values are defined once, as a module-level mapping, and applied both to the UPDATE and to the in-memory pivots that are broadcast.
     6. `reopen_run(test_run, pivots)`.
     7. In `on_commit`, delete the collected files with the storage (`TestRunScreenshot.image.field.storage.delete(name)`), each in its own try/except with logging. Files are deleted only after a successful commit, so a rolled-back re-run keeps its evidence.
     8. Return `RerunOutcome(queued=len(pivots), skipped=len(set(pivot_ids)) - len(pivots))`.
   - Query budget: lock project, lock run, 2 gate queries, pivots, history insert, screenshot names, screenshot delete, pivot update, run save. That is about 10 queries, constant in N. The on_commit work adds the chain and one summary query for the broadcast.
   - `format_rerun_message(outcome) -> str`: for example "Re-running 3 cases · 1 skipped".
3. **`projects/exceptions.py`**: `RunRerunError(message)`, user-safe `.message`, the same shape as `RunResumeError`.
4. **Why re-run is from scratch, not a Resume variant**:
   - The checkpoint must be `{}`, otherwise the worker would continue the old plan. This is why it is cleared.
   - Screenshots are deleted because "Send checked" attaches every screenshot of the pivot. Old ones would be sent as evidence for the new verdict, and the gallery would mix two attempts.
   - The log is kept, with the separator line, so the textual record of the earlier attempt survives at zero schema cost. This matches the `=== Resumed from step N ===` precedent.
   - The worker needs no change: an empty checkpoint already means "plan from step 1".

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_cases_rerun` → `/projects/<id>/test-runs/<run_id>/cases/rerun/` | POST | `@project_membership_required` + `@require_POST` | `RunCaseSelectionForm(list_rerunnable_pivot_ids(test_run), request.POST)` (fields `pivot_ids`, plus a `next` POST param through `_next_or_default`) | Redirects to `next` or to the run detail page | Unknown run: 404. Invalid form: `messages.error(form_error_text)`. `RunRerunError`: `messages.error(exc.message)`. Success: `messages.success(format_rerun_message)` |
| `projects:test_run_detail` (existing) | GET | unchanged | unchanged | New context: `rerunnable_pivot_ids` (ids of every row on this page when the run is Done or Cancelled, else empty) and `rerun_panel` (`RerunPanel`) | none |
| `projects:test_run_case_detail` (existing) | GET | unchanged | unchanged | The `resumes` list now has rows of both kinds | none |

- `RunCaseSelectionForm`: update its docstring to cover Re-run. No field changes. The `invalid_choice` text ("can no longer be changed. Reload…") fits.
- View `test_run_cases_rerun` in `projects/views.py`. It stays thin: resolve the run, build the form, call `rerun_test_run_cases`, map errors, redirect. `_build_test_run_detail_context` adds the two keys above.
- **Run page** (`templates/projects/test_run_detail.html`):
  - `runCaseChecks` gains a third list (`rerunIds`). `allIds` becomes the de-duplicated union, because check and re-run ids overlap. New getter `toRerun`.
  - A row checkbox is enabled when its id is in any of the three lists. The disabled variants (Sent, Cancelled, not finished) apply only when the run is not reopenable. Sent rows become selectable on a finished run, but still show the "Sent to R…" badge.
  - The bulk bar gains a "Re-run (N)" button (`x-text` on `toRerun.length`), shown with `x-show="testRunStatus === 'done' || testRunStatus === 'cancelled'"`:
    - when `rerun_panel.can_rerun` is true, the button dispatches `open-rerun-selected`;
    - when there are blockers, it is disabled, with `title` and `aria-describedby` set to the blocker text, the same pattern as "Resume run".
  - The confirm dialog is markup copied from the partials dialog convention (`role="alertdialog"`). It sits **inside** the `runCaseChecks` scope, so its form can render `<template x-for="id in toRerun">` hidden inputs plus `next`. `confirm_dialog.html` cannot carry dynamic inputs.
  - The dialog body is static server text plus a client-side count: "N cases run again from step 1 on this run, in run order. Their screenshots and step progress are replaced; the previous result stays in each case's history and the log keeps the earlier attempt. Checked marks are cleared; cases already sent to TestRail get a new result there when sent again."
  - The "Case results" hint mentions re-running.
  - "Re-run failed" is relabelled "Re-run failed in a new run", matching "Re-run blocked in a new run", so the two kinds of re-run are not confused.
  - Update the template's context-contract comment.
- **Case page** (`templates/projects/test_run_case_detail.html`):
  - The "Resume history" card becomes "Earlier attempts". For `kind == "rerun"` the line reads "Re-run from the start by … on …"; the resume line is unchanged.
  - `testRunCaseLive`: on a `status` event that moves from a finished status to `created`, reload the page once. This drops screenshots the client still holds and shows the history row.
  - Update the contract comment.
- `static/js/punk-hazard.js`: `runCaseChecks(checkIds, uncheckIds, rerunIds = [])`, with an updated JSDoc.

### Background work and WebSockets
- There is no new task. The chain is `execute_test_run_case.si(pivot_id, generation)` on the `execution` queue, through `queue_test_run_cases`. If the broker refuses the chain, the run is cancelled and the reset cases become Cancelled. They remain re-runnable, and the history keeps the earlier outcome, as for Resume.
- Stale workers stand down on the generation bump. Their log, screenshot, checkpoint and status writes are generation-filtered (existing). The accepted residuals from Resume apply unchanged.
- WebSocket: the existing events only.
  - `test_run.status` carries STARTED and the summary.
  - Per pivot: `test_run.pivot_status` (created) and `test_run_case.status` (created, empty steps), through `broadcast_run_and_pivots`.
  - The run page needs no client change for these. The POST redirect re-renders it anyway.
- No controller protocol change, so no `CLIENT_VERSION` or capability change.

### Settings / infrastructure
- None. Deploy: run `migrate` (0025), then restart Daphne/web. Workers need no restart: there is no task change, but restarting them is harmless.

### Data flow
1. The member selects rows on a finished run's page and clicks Re-run (N). A dialog opens. Confirming POSTs the `pivot_ids` and `next` to `test_run_cases_rerun`.
2. The view validates the ids against `list_rerunnable_pivot_ids`, then calls `rerun_test_run_cases`.
3. The service locks the project and then the run, and checks the run status and the gates. It loads the selected pivots in `RUN_CASE_ORDERING`, writes the history rows, deletes the screenshot rows, resets the pivots (appending the log separator), and calls `reopen_run`: run STARTED, generation + 1.
4. On commit, the old screenshot files are deleted, the chain is queued in run order, and the run and pivot status is broadcast.
5. The view redirects. Workers execute the selected cases from step 1. The run reaches DONE through `_update_test_run_status_if_needed`, or is aborted as usual. Cases that were not selected are never touched.

## Alternatives considered
- **Copy into a new run** (`rerun_cases`): this exists already. The user explicitly asked for the same run.
- **Make re-run a Resume with `from_step=1`**: truncating the checkpoint to step 1 still runs the restore sub-task and keeps the plan and the screenshots. That is not what "run again" means, and it mixes two attempts in the TestRail evidence.
- **Archive screenshots per attempt** (an attempt FK or flag on `TestRunScreenshot`, then filtering in the case page, the consumer `current_state` and the checked push): this keeps all evidence, but it changes three query sites and the push contract. It is deferred to Q1.
- **Duplicating the gate code in the new module**: rejected. Two copies of the lock order and the gates would drift. A small extraction (`run_reopen_services`) is cheaper than that risk. No further abstraction is added: there is no generic "restart strategy" class.
- **Per-pivot `bulk_update` like Resume**: the reset values are identical for every pivot, so one queryset `UPDATE` is simpler and needs no per-row state.

## Risks and mitigations
- **Evidence loss**: screenshots and step progress of the replaced attempt are deleted.
  - The confirm dialog says so.
  - The verdict, reason and finish time go into the history, and the log text is kept.
  - Files are deleted only after commit. See Q1.
- **TestRail duplicates**: a re-run case that was already Sent gets a second result in its source run when it is checked and sent again. This is TestRail's normal history behaviour; the dialog says so. The 018 push re-pushes the changed verdict through its hash dedupe (existing behaviour).
- **Concurrency**: the project-then-run lock order is shared with Start and Resume, and the gates are re-checked under the lock. `uncheck_run_cases` and the checked push slot take the run lock (existing), so a send cannot start while a re-run resets the Sent fields.
- **Stale workers**: the generation bump handles them. The residual is the same as Resume (the machine is driven until the next cancellation check).
- **Refactor regression in Resume**: the extraction must keep the texts, gate order and query count. Verification repeats the Resume checks.
- **XSS**:
  - The log separator is a constant.
  - The blocker texts are server constants.
  - History rows render `previous_result` with autoescape (existing).
  - No `|safe` is added.
- **Run timing**: the run keeps its original `started_at`, as Resume does, so the shown duration spans the gap until the re-run. This is accepted for consistency.
- **Large selections**: the cap is 500 ids (form), and every database operation is a constant number of queries. The chain length equals the selection size, the same as Start.
- **Migration**: an additive column with a default. Rollback: `DELETE FROM projects_testruncaseresume WHERE kind='rerun'` if the history rows must not show up as resumes, then migrate back.

## Implementation steps
1. **Model, enum, exception and migration**: add `CaseRestartKind` and `TestRunCaseResume.kind`, then `makemigrations` to create `0025_case_restart_kind`. Add `RunRerunError`. If `TestRunCaseResume` is registered in the admin, add `kind` to its readonly fields. (files: `projects/models.py`, `projects/migrations/0025_*.py`, `projects/exceptions.py`, `projects/admin.py`)
2. **Extract the shared reopen mechanics** and switch Resume to them, with no behaviour change. (files: new `projects/run_reopen_services.py`, `projects/run_resume_services.py`)
3. **Re-run service**: `run_rerun_services.py` as designed. (files: new `projects/run_rerun_services.py`)
4. **View, URL and form docstring**: `test_run_cases_rerun`, the new context keys in `_build_test_run_detail_context`, the route `cases/rerun/` next to `cases/check/`, and the `RunCaseSelectionForm` docstring. (files: `projects/views.py`, `projects/urls.py`, `projects/forms.py`)
5. **Run page UI and JS**: the third id list in `runCaseChecks`, the checkbox enablement, the bulk-bar button, the confirm dialog, the hint, the "Re-run failed in a new run" label and the contract comment. (files: `templates/projects/test_run_detail.html`, `static/js/punk-hazard.js`, plus `static/css/punk-hazard.css` only if a class is missing)
6. **Case page**: the "Earlier attempts" card with per-kind wording, a reload when the status goes back to created, and the contract comment. The blocked-card copy mentions re-run from the run page. (files: `templates/projects/test_run_case_detail.html`)

Steps 1–2 come first, because they are shared. Steps 3–4 depend on them. Steps 5 and 6 touch separate files and can run in parallel with 3–4 against the agreed context keys (`rerunnable_pivot_ids`, `rerun_panel`), the URL name `projects:test_run_cases_rerun` and the event name `open-rerun-selected`.

## Verification
- `pre-commit run --all-files` (isort, black, mypy strict), `python manage.py check` and `python manage.py makemigrations --check` after migrating.
- Manual checks (dev stack, runner connected, visual engine ready):
  1. Finish a run with at least one case each Passed, Failed, Blocked and Cancelled. Select one Passed and one Blocked case on the page, in reverse page order, and re-run them. Check the following:
     - the run shows Started;
     - only those two show "Not started", then "Running";
     - they execute in TestRail order (worker log or `started_at`);
     - the other cases keep their verdicts;
     - the run returns to Done with correct counters.
  2. On the case page of a re-run case:
     - the old screenshots are gone, both from the gallery and from `MEDIA_ROOT/screenshots/trtc_<id>/`;
     - the log shows only the new attempt (cleared on re-run, see Changelog);
     - Steps shows only the new plan;
     - "Earlier attempts" lists "Re-run from the start by <you>" with the previous verdict.
  3. Keep a case page open while re-running it from the run page: the page reloads once and then streams the new attempt.
  4. Check a case, Send it to TestRail (Sent badge), then re-run it. The Checked and Sent marks are cleared. After it finishes, check it and send it again: a second result appears in the source TestRail run.
  5. Gates: with another run Started, the runner disconnected, a checked send live, or a setup turn live, the Re-run button is disabled with the reason. A forged POST is refused with the same message. On a Started run the button is hidden and a forged POST is refused ("finished or cancelled").
  6. IDOR: POST a pivot id from another run or project. The form is invalid ("can no longer be changed") and nothing changes.
  7. Abort during a re-run: only the selected cases that have not finished become Cancelled. The cases that were not selected keep their verdicts, and the run is Cancelled.
  8. Resume regression: run the Resume run / Resume case checks from `docs/plan/feat-resume-blocked-case.md` (Verification 2–5). Same messages and behaviour, and "Earlier attempts" shows "Resumed by …".
  9. Kill the broker (or point it at a bad URL) and re-run. The run becomes Cancelled, and the selected cases become Cancelled and remain selectable.
- Run the `reviewer` and `verifier` agents in parallel on the diff, with the original request and this plan.

## Open questions
- **Q1 – Previous attempt's evidence.** Should re-run keep the earlier attempt's screenshots and step progress viewable (archived per attempt, excluded from TestRail sends)? Or is "keep the log text plus the verdict in history, delete the screenshots" enough? *Assumption in this draft:* delete the screenshots and the checkpoint, keep the log with a separator, and record the previous status, reason, verdict text and finish time in the history. Archiving needs a screenshot attempt marker and filter changes in three places.
- **Q2 – "Keep the order as it is on the run."** The run page lists failures first. Execution has always followed TestRail order (`RUN_CASE_ORDERING`). *Assumption:* re-run follows the run's execution order (TestRail order) restricted to the selection, not the failures-first display order or the click order.
- **Q3 – Which cases can be re-run.** *Assumption:* any case with a finished status (Passed, Failed, Blocked, Cancelled), including cases already Sent to TestRail. Their Checked/Sent marks are cleared, and a later send adds a new TestRail result. Say so if Sent cases should be locked, or if Passed cases should not be re-runnable.
- **Q4 – Run state.** *Assumption:* only on a Done or Cancelled run, with the same gates as Resume. Re-running while the run is still in progress (appending to the live queue) is out of scope.

## Answers (2026-10-23)
- Q1: keep log + verdict in history; delete screenshots and step progress (as assumed).
- Q2: run execution order (`RUN_CASE_ORDERING`) restricted to the selection (as assumed).
- Q3: "everything": every case on the run is re-runnable, any status, including Sent ones.
- Q4: finished (Done/Cancelled) runs only, same gates as Resume (as assumed).

## Changelog
- 2026-10-23: created
- 2026-10-23: open questions answered; Q3 widened to every status; Status In progress
- 2026-10-23: review round 1 fixed: no `Any` reset dict (explicit `update()` kwargs + `_reset_in_memory`); reopen blockers computed once per run page (`get_resume_panel(test_run, *, blockers)` fed from `get_rerun_panel`); `rerunnable_ids_on_page` service; `TestRunCaseResume.is_rerun`; case page also reloads from `in_progress`.
- 2026-10-23: rejected: moving screenshot file deletion to a Celery task. Files are local-disk unlinks after commit, the same as `reset_test_run` does inline.
- 2026-10-23: accepted: Resume now also reloads an open case page (it needs the new history row too). Accepted residual, as for Resume: a stale worker past its generation check can still write one old-attempt screenshot after the delete.
- 2026-10-23: follow-up, not fixed: `add_cases_to_test_run` does not check the run status, so a case added to a Done run and not selected keeps a re-run Started until Abort. Resume has the same gap.
- 2026-10-23: `black` reformats `projects/migrations/0022_*` on main (pre-existing, left out of this change).
- 2026-10-23: user request after release: a re-run clears the case's log instead of appending a separator line (dialog text updated).
