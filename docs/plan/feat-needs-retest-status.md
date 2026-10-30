# feat: Needs re-test status

Status: Done
Created: 2026-10-30
Request: "Add new `need to re-test` status, which can be marked to any test even in middle of a run." A writable project member can flag any case of a run as **Needs re-test**. This works in any run status and on any case status, including a case that is running now, one that waits for help, and one that waits for a person's step. The flag is a person's verdict: "this result cannot be trusted, test it again". It shows wherever case statuses appear. It maps to TestRail's built-in **Retest** status. Later work picks it up through Re-run.

## Context

- Case statuses: `TestRunTestCaseStatus` (`projects/models.py:103`). `FINISHED_CASE_STATUSES` (`:119`) drives `shown_duration`, test-session freezes and the session duration.
- Run completion:
  - `projects/services.py` `_update_test_run_status_if_needed` (`:3888`) treats a run as settled when no case is CREATED / IN_PROGRESS / NEED_HELP / AWAITING_PERSON;
  - `finish_test_run` (`:3927`) is the only path to DONE;
  - `run_reopen_services.queue_pending_cases` wakes or finishes a PAUSED run.
- Worker: `execute_test_run_test_case` (`services.py:3062`).
  - Its only stop signal is run-level, `_build_cancellation_check`: the run is CANCELLED or its generation changed.
  - `_mark_pivot_in_progress` writes IN_PROGRESS unconditionally.
  - `_finalize_pivot` writes the verdict with no lock and no status check.
  - `_pause_run_for_help` and `_pause_run_for_person` re-check `status == IN_PROGRESS` under the run lock.
  - On `AgentCancelledError`, any status other than CANCELLED becomes CANCELLED.
  - A PAUSED run makes leftover chain tasks stand down.
- Person resolutions of waiting cases:
  - `case_help_services.mark_failed_after_help` / `mark_blocked_after_help` take locks in the order project → run → help request → open person steps → case. They then call `_resolve`, close person steps, `supersede_pending_revisions`, and `_queue_or_wait`, which waits for a live auto-fix turn to stop and then wakes or finishes the run.
  - `person_step_services.mark_failed_person_step` / `mark_blocked_person_step` do the same through `_after_action`.
- Verdict-changing precedent:
  - `case_result_change_services.apply_result_change` locks project → run → case, refuses while `list_testrail_push_blockers`, clears Checked/Sent (`clear_checked_and_sent`) and records `CaseActivity.RESULT_CHANGE`.
- Restart paths:
  - Resume (`run_resume_services`, `RESUMABLE_STATUSES` = Blocked, Cancelled) continues from the checkpoint;
  - Re-run in place (`run_rerun_services`, any status, Done/Cancelled runs) starts over from step 1;
  - "Re-run failed/blocked in a new run" (`services.rerun_cases`, `RerunScope`);
  - Reset (`services.reset_test_run`).
- TestRail:
  - `projects/testrail_mapping.py` `_PIVOT_STATUS_TO_TESTRAIL` / `_TESTRAIL_STATUS_TO_LABEL` (1/2/5 only);
  - whole-run push `testrail_results_services._plan_pivot`;
  - Send checked `testrail_checked_push_services._plan_pivot` plus `run_case_check_services.CHECKABLE_STATUSES`;
  - early case send `testrail_case_send_services.CASE_SEND_STATUSES` (Failed only);
  - evidence `testrail_evidence.select_evidence` (Failed / Blocked only).
  - TestRail system status 4 = Retest and is valid in `add_result(s)`; 3 = Untested is not ([TestRail Results API](https://support.testrail.com/hc/en-us/articles/7077819312404-Results)).
- Status displays:
  - `templates/partials/badge_case_status.html` and `badge_run_status.html`;
  - the JS badge maps in `test_run_detail.html` (`pivotBadgeClass/Label`) and `test_run_case_detail.html` (`badgeClass/Label`, `reloadOn`);
  - the hard-coded first-render labels in both pages;
  - `get_test_run_summary`, `_annotate_run_counts`, `get_latest_runs_by_project`, `list_test_run_cases` ordering, `build_run_narrative`;
  - `case_step_progress.build_case_step_progress` (`interrupted`);
  - `run_finished_notifications._case_counts`, `notifications/services/night_digest._load_runs`;
  - test sessions: `case_outcome` (`FINISHED_CASE_STATUSES`, labels from choices), `record_case_activity` (`VERDICT_DISCARDING`), and `session_queries._STATUS_ORDER` and the CSV export (both read from the choices).
- Things that need no code change:
  - the Failures page, the failed widget and the inbox filter on `status=FAILED`, so a Failed case marked for re-test drops out of them;
  - attention items read open help requests, so they clear when the request resolves;
  - `machine_health` labels resolutions from `HelpResolution` choices.

## Goals

- New case status `needs_retest` ("Needs re-test"). A writable member sets it on one case (case page) or many (run page bulk bar), on any non-draft run, from any case status except Needs re-test itself.
- A running case stops at its next cancellation check. A Need help or Your turn case is closed the way Mark failed / Mark blocked close it, and the run goes on.
- The previous verdict and its evidence (log, screenshots, checkpoint, result text) are kept. The service records who marked it, when, with what note, and from which status.
- The run treats Needs re-test as settled: it finishes Done, and the run badge does not read Passed.
- Re-run in place (any status) and Reset clear it. Undo restores the earlier verdict when there was one.
- TestRail: maps to Retest (status_id 4) in the post-run Push, in Send checked, and in the early case send.

## Non-goals

- No automatic re-queue of a marked case in the current run (see Q3).
- Resume does not pick Needs re-test cases up (see Q4).
- No Test Runner window or controller protocol change. The runner's person window only drops the closed step through the existing nudge.
- No change to the TestRail comment layout (`build_result_comment` / `build_checked_result_comment`) or the case page "Open in TestRail" link, which parallel work owns. Only the status maps and the result-text lead change.
- No agent tool for proposing Needs re-test. The result chat's `propose_result_change` keeps Passed / Failed / Blocked.
- Draft (WAITING) runs cannot be marked: nothing has been tested yet.

## Design

### Data model

`projects/models.py`, migration `projects.0045_needs_retest`, additive only:

- `TestRunTestCaseStatus.NEEDS_RETEST = "needs_retest", "Needs re-test"` (12 chars, fits `max_length=20`).
  - Generates choice-only `AlterField`s for `TestRunTestCase.status`, `TestRunCaseResume.previous_status` and `CaseResultChange.from_status` / `to_status`.
- `FINISHED_CASE_STATUSES` gains `NEEDS_RETEST`. Its `active_duration` is final: the segment is closed when it is marked.
- New `RETEST_VERDICT_STATUSES = (SUCCESS, FAILED, BLOCKED)`: the "from" statuses that Undo can restore.
- `TestRunTestCase` new fields, meaningful only while the status is `NEEDS_RETEST` (same convention as `failed_by`):
  - `retest_from_status` CharField(20, choices, blank)
  - `retest_marked_at` DateTimeField(null)
  - `retest_marked_by` FK user, `SET_NULL`, `related_name="+"`
  - `retest_note` TextField(blank, `max_length=RETEST_NOTE_MAX_LENGTH` = 2000)
- `blocked_reason` and `failed_by` are **left in place** while Needs re-test, so Undo restores them for free. Every reader already gates them on the status: `get_blocked_reason_fields`, `case_outcome.failed_by_label` / `blocked_reason_label`, the Failures filters and the run page Reason column. The `blocked_reason` comment changes to "meaningful only while BLOCKED".
- `HelpResolution.MARKED_RETEST = "marked_retest", "Marked for re-test"` (fits 16).
- `PersonStepClose.MARKED_RETEST = "marked_retest", "Marked for re-test"` (fits 24).
- `test_sessions/case_activity.py`:
  - `CaseActivity.RETEST = "retest", "Marked for re-test"`;
  - add it to `VERDICT_DISCARDING`, so other sessions freeze the verdict they saw.
  - Migration `test_sessions.0002`: choice-only `AlterField`s on `first_activity` / `last_activity`.
- No data migration and no backfill.

### Services

New leaf module **`projects/case_retest_marking.py`** (no imports from `projects.services`, help or person modules; imported by all of them):

- `RETEST_FIELDS`: the four `retest_*` fields.
- `apply_retest_mark(pivot, *, user, note, now) -> list[str]`: in memory, on a locked pivot.
  - Sets `retest_from_status` (the current status), `retest_marked_*` and `retest_note`.
  - Sets status `NEEDS_RETEST`.
  - Keeps `result` and `finished_at` for verdict statuses; sets `finished_at = now` for the others.
  - Clears Checked/Sent (`run_reopen_services.clear_checked_and_sent`; `run_reopen_services` imports `projects.services` but not the help modules, so no cycle).
  - Returns the update fields.
- `clear_retest_fields(pivot)` / the field values for `QuerySet.update`, shared by Re-run and Reset.

New use-case module **`projects/run_case_retest_services.py`**:

- `mark_cases_for_retest(*, test_run, pivot_ids, user, note) -> RetestOutcome(marked, skipped, run_continues_blocker)` in two phases.
  - **Phase 1**, one transaction, covering every selected case not in NEED_HELP / AWAITING_PERSON:
    - `lock_project_and_run`; refuse a WAITING run;
    - when any selected case has a verdict (`RETEST_VERDICT_STATUSES`), refuse while `list_testrail_push_blockers` (same rule as Apply). In-flight and waiting cases cannot be in a send, so a live early send does not block marking them (the `list_wake_blockers` invariant);
    - lock the selected cases' open `CasePersonStep` rows: a HITL case being confirmed is IN_PROGRESS with a `reported` step. Then lock the pivots with `select_for_update().defer("logs")`, filtered to the run and to status ≠ NEEDS_RETEST;
    - per case: `close_case_segment` if IN_PROGRESS, then `apply_retest_mark`; one `bulk_update`;
    - close those person steps (CANCELLED, `MARKED_RETEST`), one UPDATE;
    - supersede pending result changes and review redesign drafts of the cases (one UPDATE each: add id-list variants `supersede_pending_result_changes(pivot_ids)` in `case_result_change_services` and `supersede_cases_review_drafts(pivot_ids)` in `case_review_redesign_store`);
    - `record_case_activity(..., CaseActivity.RETEST)` **before** the write, since the activity discards a verdict;
    - on commit: `broadcast_run_and_pivots`, which also nudges the runner's person window, plus `broadcast_project_attention_changed`;
    - PAUSED run: when no NEED_HELP / AWAITING_PERSON case is left and no auto-fix turn is live, wake it as "Continue run" does (savepoint `queue_pending_cases`; a gate refusal keeps the marks and leaves "Continue run"). This covers marking the last CREATED cases of a run whose help was already resolved.
  - **Phase 2**, only when the run's single waiting case is selected (a run has at most one), in its own transaction after phase 1 commits:
    - NEED_HELP → `case_help_services.mark_retest_after_help`;
    - AWAITING_PERSON → `person_step_services.mark_retest_person_step`.
    - A refusal there, for example a request already resolved by someone else, is counted as skipped with its message. Phase 1 marks stay.
- `undo_retest(*, pivot, user) -> None`.
  - Locks project → run → case. Allowed only when the status is NEEDS_RETEST and `retest_from_status ∈ RETEST_VERDICT_STATUSES`.
  - Refused while `list_testrail_push_blockers`, since a Retest may be being sent.
  - Restores the status, keeps `finished_at`, clears `RETEST_FIELDS`, and broadcasts.
  - Not recorded in test sessions. Checked/Sent stay cleared.
- Read side (no extra query per row):
  - `is_retest_open(test_run)`: not WAITING;
  - `retestable_ids_on_page(test_run, pivots)`;
  - `list_retestable_pivot_ids(test_run)`: 1 query, used as form choices;
  - `get_case_retest_panel(pivot)`: who/when/note/from-label, `can_undo`, undo blocker;
  - `format_retest_message(outcome)`.

`projects/case_help_services.py`: **`mark_retest_after_help(*, pivot, user, note) -> HelpActionResult`**.

- Same shape as `mark_failed_after_help`: lock project → run → help request → open person steps → case.
- Closes person steps (`MARKED_RETEST`), `supersede_pending_revisions`, and `_resolve(..., HelpResolution.MARKED_RETEST, ...)`, which records `CaseActivity.HELP`. The `RETEST` activity is recorded by the caller first, so the order matches "verdict-discarding first".
- Writes the mark with `apply_retest_mark`, then `_queue_or_wait`: a live auto-fix is asked to stop and the run continues when it ends, or the run is woken or finished.
- `finish_case_auto_fix` already handles a request a person resolved during the auto-fix.

`projects/person_step_services.py`: **`mark_retest_person_step(*, pivot, actor, note) -> PersonStepActionResult`**.

- Locks the paused run, then the case's actionable open step and the case (it finds the step itself, with no round check: marking is decisive).
- Closes the step (CANCELLED, `MARKED_RETEST`), applies the mark, then `_after_action`.
- Web only. No runner-window action.

`projects/services.py` (worker and read side):

- `_build_cancellation_check(test_run_id, generation, pivot_id)`: still one EXISTS query per poll, now true when the run is CANCELLED, the generation moved, **or** this pivot is NEEDS_RETEST. Teardown uses the same check, so a mark during a Failed case's teardown stops it.
- Pick-up guard: `_mark_pivot_in_progress` becomes a short transaction that locks the pivot row. A pivot that is NEEDS_RETEST is skipped: the task returns after `_update_test_run_status_if_needed`, so a skipped last task still finishes the run. This prevents the race where the worker read CREATED before the mark committed.
- Verdict guard: `_finalize_pivot` locks the pivot row and writes only while IN_PROGRESS. It returns whether it wrote, and `notify_case_failed` is sent only then. This also stops a late verdict from overwriting an aborted case's CANCELLED, a small strictness gain.
- `except (AgentCancelledError, ControllerNotConnectedError)`: cancel the case only while it is still IN_PROGRESS, never a NEEDS_RETEST one.
- After an interrupted case: `paused_for_help` is False and the status is not FAILED, so the code skips the teardown, runs `_safe_cleanup`, then `_update_test_run_status_if_needed`, and the chain goes on with the next case. The run generation is not bumped.
- `_pause_run_for_help`, `_pause_run_for_person` and `_pause_case_after_fault` need no change: they already do nothing unless the case is IN_PROGRESS under the run lock.
- `get_test_run_summary` gains `needs_retest`.
- `_annotate_run_counts` and `get_latest_runs_by_project` gain `retest_count`.
- `list_test_run_cases` order: Failed 0, Blocked 1, Needs re-test 2, rest 3.
- Narrative:
  - "finished" counts include Needs re-test;
  - a finished run adds "N need re-test: titles." via `list_retest_case_titles`;
  - the view only queries the titles when the count is above 0.
- `reset_test_run` clears `RETEST_FIELDS`.
- `STATUS_FILTER_CHOICES` / `VALID_STATUS_FILTERS` pick the new value up from the enum.

`projects/run_rerun_services.py`: the reset UPDATE and `_reset_in_memory` clear `RETEST_FIELDS`. The Earlier attempts snapshot records `previous_status="needs_retest"` and keeps the result text.

Unchanged by design:

- Resume (`RESUMABLE_STATUSES`) and `reset_case_for_restart`.
- `abort_test_run`: Needs re-test is not in its lists, so it stays.
- `case_result_change_services.RESULT_CHANGEABLE_STATUSES`: Discuss result is hidden on a Needs re-test case. An existing result thread still opens, and its proposals are refused as not changeable.
- `case_teardown_policy`.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_cases_retest` `/projects/<id>/test-runs/<run_id>/cases/retest/` | POST | `@project_membership_required` (read-only → 403) | `RunCasesRetestForm(RunCaseSelectionForm)` + `note` (CharField, optional, `max_length=RETEST_NOTE_MAX_LENGTH`, strip). Choices = `list_retestable_pivot_ids` | redirect to `next` (`_next_or_default`): run page or case page | `RunCaseRetestError` / `RunHelpError` / `PersonStepError` → `messages.error`; form errors via `form_error_text` |
| `projects:test_run_case_retest_undo` `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/retest/undo/` | POST | same | none (empty POST, CSRF) | redirect to case page | `RunCaseRetestError` |

- Views go in a new `projects/run_case_retest_views.py`, following `person_step_views.py`, to keep `views.py` from growing. They are thin: form, then service, then flash.
- **Run page** (`test_run_detail.html`):
  - new context key `retestable_pivot_ids`;
  - `runCaseChecks` in `static/js/punk-hazard.js` takes a 5th list `retestIds` / `toRetest`;
  - the bulk bar gets "Needs re-test (N)" in every non-draft run status;
  - shared confirm dialog `templates/projects/includes/_retest_dialog.html`: selected ids as hidden inputs, the note, and server-written text: "A running case stops now; a case waiting for help or for a person's step is closed and the run continues. Results, logs and screenshots are kept. Checked marks are cleared." There is no `|safe`;
  - the row checkbox enables for retestable rows;
  - a stat card "Needs re-test", shown when the count is above 0;
  - live `summary.needs_retest`;
  - badge maps and the first-render label gain `needs_retest` → `badge-retest` / "Needs re-test";
  - the Reason column shows "Marked by X · note" (escaped, truncated).
- **Case page** (`test_run_case_detail.html`):
  - a header button "Mark for re-test" for writable members when `can_mark_retest`, opening the same dialog with one id and `next` = the case page;
  - a "Needs re-test" card: who, when, previous result label, the note, "Undo" when `retest_panel.can_undo`, and the hint "Re-run it from the run page when the run has finished.";
  - badge maps updated; `reloadOn` gains `needs_retest`, so the server-rendered card, help card and Your turn card refresh.
- Help card and Your turn card: no new button. The header button covers both, and the service dispatches by status. This keeps the card templates, which parallel features touch, unchanged.
- Badges and CSS:
  - `badge_case_status.html` gets the new branch;
  - `badge_run_status.html` gets `retest_count`, ordered Failed > Blocked > Needs re-test > Passed, a "Needs re-test" badge, and updated includes (`_overview_runs`, `_project_table`, `_test_run_table` with the text "· N need re-test", `_overview_stats`);
  - `templates/partials/README.md` contracts;
  - `.badge-retest`, `.stat-value.is-retest` and `.card-tone-retest` in `static/css/punk-hazard.css`, from existing tokens.
- Alpine is used only for the selection and the dialog.

### Background work and WebSockets

- No new task, queue or socket event. Pages update through `test_run.status` / `test_run.pivot_status` / `test_run_case.status`, whose summary now carries `needs_retest`. The runner gets the existing `person_step_state` nudge.
- No controller protocol change, so no `CLIENT_VERSION` / capability work.
- Notifications:
  - `run_finished_notifications` counts and labels "N need re-test";
  - the night digest adds `needs_retest` to its counts and lines;
  - marking sends no push.

### TestRail

- `testrail_mapping.py`:
  - `TESTRAIL_STATUS_RETEST: Final = 4`;
  - `_PIVOT_STATUS_TO_TESTRAIL[NEEDS_RETEST] = 4`;
  - `_TESTRAIL_STATUS_TO_LABEL[4] = "Retest"`;
  - the `map_pivot_status_to_testrail` docstring.
  - Only the maps change; the comment builders are not touched.
- `testrail_results_services.pivot_result_text`: a Needs re-test case leads with "Marked for re-test by <name>[: <note>]", then the earlier result (as Blocked leads with its reason). One branch.
- `run_case_check_services.CHECKABLE_STATUSES` += NEEDS_RETEST. Send checked then sends it as Retest. The whole-run Push sends it through the map.
- `testrail_case_send_services.CASE_SEND_STATUSES` += NEEDS_RETEST, for the early send (Q7).
- `testrail_evidence.select_evidence`: no screenshots for Needs re-test (unchanged `else` branch).

### Settings / infrastructure

- No new setting.
- Deploy:
  - run `migrate` (`projects.0045`, `test_sessions.0002`);
  - restart Daphne/web and **every execution worker together**. An old worker has neither the per-case cancel nor the pick-up/finalize guards: it would run a marked CREATED case or overwrite a mark with its verdict.

### Data flow

1. A member selects cases (or uses the case page button), types an optional note and confirms. The request is a POST to `test_run_cases_retest`.
2. The view validates `RunCasesRetestForm` against `list_retestable_pivot_ids` and calls `mark_cases_for_retest`.
3. Phase 1 runs under the project → run → person step → case locks. Each case becomes NEEDS_RETEST with its "from" status, and any segment is closed. Session activity is recorded and pages are broadcast on commit.
4. A running case: the worker's next `cancellation_check()` sees NEEDS_RETEST and raises `AgentCancelledError`. The worker leaves the case as is, cleans up, and the chain moves on. If it reaches `_finalize_pivot` first, the locked status check drops the verdict.
5. A CREATED case still in the chain: its task skips it at pick-up.
6. A waiting case (phase 2): the help request or person step is closed as "Marked for re-test" and a live auto-fix is told to stop. The run then queues the remaining cases, or finishes Done.
7. When every case is settled, the run is DONE. Later, Re-run (N) in place re-tests the case from step 1, or Undo restores the earlier verdict.

## Alternatives considered

- **Refuse on running or waiting cases.** Simplest, but the user asked explicitly for "even in middle of a run". Kept as Q1/Q2 options.
- **Defer the mark until the running case reaches a verdict** (a pending flag that `_finalize_pivot` applies). This needs more state and keeps the machine busy on a test the person already distrusts. Stopping uses the cancellation path that already exists.
- **Bump the generation and re-queue the rest, as Resume does.** This would also stop the running case, but it restarts the whole chain and touches every other worker. The per-case check is narrower.
- **A separate flag next to the status** (keeping Passed/Failed and adding a "retest" boolean). The user asked for a status, and every status surface and the TestRail map would still need a second dimension.
- **A history model for marks.** `TestRunCaseResume` already snapshots at Re-run, and four fields on the case cover the current mark and Undo. A table is not needed until the full mark history matters.
- **New help-card and person-card buttons.** Rejected in favour of one header button that dispatches by status. It needs fewer template changes in areas parallel work touches.

## Risks and mitigations

- **Worker races** (mark against pick-up, verdict, help pause or person pause): each worker write that could clobber the mark now runs under a pivot row lock or the run lock with a status re-check. Deploy all execution workers together.
- **Stop latency:** a running case stops at the next check. An in-flight LLM call or controller action finishes first, from seconds up to one tool timeout. The page shows Needs re-test at once. An interrupted case gets no teardown, only the cleanup, and the next case starts with its usual cleanup.
- **Cancellation check cost:** still one query per poll, now with a join on one pivot row by primary key.
- **Paused-run wake:** phase 2 reuses `_queue_or_wait` / `_after_action`. Phase 1's wake follows `continue_paused_run`'s rules (no live auto-fix, `list_wake_blockers`). A gate refusal leaves "Continue run".
- **TestRail:**
  - a customised TestRail may hide Retest. The send then fails for that case through the existing per-case error path, so nothing is lost;
  - a case already sent (as Failed) loses Sent when marked, and the next send adds a Retest result. This is the same as Apply and Re-run.
- **Verdict loss for sessions:** `CaseActivity.RETEST` is verdict-discarding, so other sessions freeze first.
- **XSS:** the note and names are rendered escaped everywhere, the dialog text is server-written, and there is no `|safe`.
- **Authorization:** both views are POST with `@project_membership_required`, so read-only members get 403. Pivot ids are validated against the run through the form choices and again in the service filter.
- **N+1:** the bulk path is a constant number of queries: locks, one `bulk_update`, one UPDATE per side table and at most 5 for the session. Page context adds no per-row query; the retestable ids come from the page's already-loaded rows.

## Implementation steps

Each step is one commit. **Parallel:** after step 1, steps 2, 3, 6 and 7 touch disjoint files and can run as parallel sub-agents. Step 4 needs step 3, step 5 needs steps 2 and 4, and step 8 comes last.

1. **Shared model and contracts.**
   - Enum values, `FINISHED_CASE_STATUSES`, `RETEST_VERDICT_STATUSES`, `RETEST_NOTE_MAX_LENGTH` and the four fields in `projects/models.py`, plus migration `0045`.
   - `HelpResolution` / `PersonStepClose` values.
   - `CaseActivity.RETEST` + `VERDICT_DISCARDING`, plus `test_sessions` migration `0002`.
   - `RunCaseRetestError` in `projects/exceptions.py`.
   - New `projects/case_retest_marking.py`.
   - `RunCasesRetestForm` in `projects/forms.py`.
   - Files: `projects/models.py`, `projects/migrations/0045_needs_retest.py`, `test_sessions/case_activity.py`, `test_sessions/migrations/0002_*.py`, `projects/exceptions.py`, `projects/case_retest_marking.py`, `projects/forms.py`.
2. **Worker guards and read side in `projects/services.py`.** Per-case cancellation check, pick-up guard, `_finalize_pivot` guard, the cancel branch, summary/annotation keys, ordering, narrative and titles, Reset clearing. (files: `projects/services.py`)
3. **Waiting-case resolutions.** `mark_retest_after_help` and `mark_retest_person_step`. (files: `projects/case_help_services.py`, `projects/person_step_services.py`)
4. **Use case.**
   - `projects/run_case_retest_services.py` (mark in two phases, undo, read side);
   - id-list supersede helpers in `projects/case_result_change_services.py` and `projects/case_review_redesign_store.py`;
   - Re-run clearing in `projects/run_rerun_services.py`.
5. **Pages.**
   - `projects/run_case_retest_views.py` and `projects/urls.py`;
   - context keys in `projects/views.py`: `retestable_pivot_ids`, `retest_titles`, `can_mark_retest`, `retest_panel`, `retest_form`;
   - `templates/projects/test_run_detail.html`, `templates/projects/test_run_case_detail.html`, new `templates/projects/includes/_retest_dialog.html`;
   - `static/js/punk-hazard.js` (`runCaseChecks`), `static/css/punk-hazard.css`;
   - `templates/partials/badge_case_status.html`, `badge_run_status.html`, `README.md`;
   - the run badge includes (`_overview_runs`, `_project_table`, `_test_run_table`, `_overview_stats`).
6. **Counts elsewhere.** `projects/case_step_progress.py` (`interrupted`), `projects/run_finished_notifications.py`, `notifications/services/night_digest.py` (and its message text).
7. **TestRail.** `projects/testrail_mapping.py` (maps and constant only), `projects/testrail_results_services.py` (`pivot_result_text`), `projects/run_case_check_services.py`, `projects/testrail_case_send_services.py` (the latter only if Q7 = include).
8. **Docs.** A CLAUDE.md bullet under Need help / Key Patterns, `docs/changed/2026-10-30-needs-retest-status.md`, and this plan's Status.

## Verification

No automated tests. Run `pre-commit run --all-files` (strict mypy, black, isort), `python manage.py check`, `makemigrations --check` and `sqlmigrate projects 0045`. Then do these manual smoke checks on a dev DB with daphne on a unique port:

1. A read-only member POSTing to both URLs gets 403. Foreign or unknown pivot ids give a form error.
2. Done run: mark a Passed case. The badge, the stat card, the run badge ("Needs re-test" when there is no failure or block), the narrative and the runs list text all update. Undo restores Passed with the same `finished_at`.
3. Running run, case IN_PROGRESS: mark it. The worker stops within one check, the log shows no verdict, the status stays Needs re-test, and the next case starts. The run finishes Done.
4. Running run: mark a later CREATED case. Its task skips it. Mark the *last* CREATED case: the run still reaches Done.
5. Paused (Need help, Attended): mark the help case. The request resolves as "Marked for re-test" and the run continues. Repeat in Night mode with the auto-fix running: the run waits for the turn to stop, then continues.
6. Human in the loop, Your turn: mark the case. The step closes as "Marked for re-test", the runner window drops it, and the run continues.
7. Bulk across a paused case plus finished cases: both phases apply, and the flash shows the counts.
8. Re-run (N) on a Needs re-test case: it runs from step 1, the retest fields are cleared, and Earlier attempts shows "Needs re-test". Reset clears it too.
9. TestRail (run-link import): Check + Send checked posts status 4 with the "Marked for re-test by …" lead. Early send from the run page works on a running run (if Q7). The post-run Push sends 4.
10. Test session open: marking records "Marked for re-test", another session's row stays frozen at the old verdict, and the CSV shows "Needs re-test".
11. Test case list filter "Needs re-test". A Failed case that was marked leaves `/failures/` and the inbox.

Then run `reviewer` and `verifier` in parallel, giving the verifier the original request and this plan.

## Open questions

1. **A case that is running right now (IN_PROGRESS): what happens to the worker?**
   - (a) Stop it at its next check. The case becomes Needs re-test with no verdict, and the run moves on to the next case. **Recommended**: the request says "even in middle of a run".
   - (b) Refuse until the case reaches a verdict.
   - (c) Let it finish, then replace its verdict with Needs re-test.
   - Draft assumes (a).
2. **A case waiting for help (Need help, maybe with a Night auto-fix running) or for a person's step (Your turn):**
   - (a) Marking closes the help request or person step as "Marked for re-test", stops a running auto-fix, and the run continues with the remaining cases. Same as Mark failed / Mark blocked. **Recommended.**
   - (b) Refuse: resolve the help or step first.
   - Draft assumes (a).
3. **After marking mid-run, should the case be re-tested automatically?**
   - (a) No: it is a status. The run finishes Done with it, and a person re-tests later with Re-run (N) on the same run. **Recommended.**
   - (b) Re-queue it automatically at the end of the current run.
   - (c) (a) plus a "Re-run needs re-test in a new run" button, next to "Re-run failed in a new run".
   - Draft assumes (a).
4. **Should Resume run (which continues Blocked/Cancelled cases from the step where they stopped) also pick up Needs re-test cases?**
   - (a) No. Re-test means from step 1, which is Re-run. **Recommended.**
   - (b) Yes, continue from the checkpoint.
   - Draft assumes (a).
5. **Can the mark be cleared without re-running?**
   - (a) Yes, "Undo" restores the earlier Passed/Failed/Blocked verdict. Not offered when the case had no verdict (it was running, waiting or not started); those are cleared only by Re-run or Reset. **Recommended.**
   - (b) Only by Re-run or Reset.
   - (c) (a), and also allow "Discuss result" / result changes on a Needs re-test case.
   - Draft assumes (a).
6. **Keep the earlier evidence?**
   - (a) Keep the log, screenshots, step progress and result text, and store who, when, a note and the earlier status. **Recommended.**
   - (b) Clear them at marking, as Re-run does.
   - Also: is an optional note (up to 2000 characters) wanted? The draft assumes (a) with an optional note.
7. **TestRail: map Needs re-test to TestRail "Retest" (status 4)** in the post-run Push and in "Send checked" (these cases can be Checked). Should a Needs re-test case also be sendable early, from the run page while the run is going, the case page and the bulk Send, like a Failed case?
   - (a) Yes. **Recommended**: it is a person's decision, final until a re-run.
   - (b) No, Failed only; Needs re-test goes with Send checked / Push after the run.
   - Either way, a case already sent as Failed loses its Sent mark when marked, and the next send adds a Retest result in TestRail.
   - Draft assumes (a).
8. **Draft runs:**
   - (a) Marking is not offered on a Draft run, since nothing ran yet. **Recommended.**
   - (b) Allow it, and Start skips those cases.
   - Draft assumes (a).
9. **Failures page and inbox:**
   - (a) A Failed case marked for re-test leaves `/failures/`, the Failed widget and the missed-failure inbox, which all list Failed cases only. **Recommended**: no change needed.
   - (b) Keep listing it there with a "Needs re-test" badge.
   - Draft assumes (a).

## Changelog

- 2026-10-30: created (Draft).
- 2026-10-30: questions answered (all recommended: 1a–9a, optional note kept); implemented.
- 2026-10-30: review round 1 fixes: marking never restarts a paused run's remaining cases (it only finishes a run the marks settled, and reports a refusal); the teardown of a Failed verdict uses a run-only cancellation check; person steps closed only for cases actually marked; earlier result label "Passed" (not "Success"); `machine_setup` run catalog reports "Done, Needs re-test" and the count; case page dialog only for writable members; Draft run message on the view. `_pause_case_after_fault` also uses the conditional cancel (protects a mark; accepted beyond the plan).
- 2026-10-30: rejected review nits: extracting a shared safe-`next` helper and a neutral run-case getter (the views follow the existing per-module pattern in `testrail_views.py` / `person_step_views.py`; a cross-module refactor is out of scope); a shared `case_outcome_counts()` for the six count sites (separate refactor plan); a shared person-step close helper (`_cancel_person_steps` is private to the help module; the inline UPDATE now filters by the marked cases).
- 2026-10-30: review round 2 clean (reviewer + verifier); Done.
