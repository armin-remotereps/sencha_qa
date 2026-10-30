# feat: Send failed cases to their TestRail run before the run finishes, also from the Failures page

Status: Done
Created: 2026-10-30
Request: A Failed run case must be sendable to its source TestRail run as soon
as it has its verdict, without waiting for the Punk Hazard run to finish, because
failures should be flagged early. The same send must be available on the
Failures page (`/failures/`): one case from its row, or several from the existing
bulk selection. Reuse the existing "Send checked to TestRail" machinery rather
than building a second one.

## Context

- **Send checked today** (`projects/testrail_checked_push_services.py`, task
  `projects.tasks.push_checked_results_to_testrail` on `upload`, 3600/3660 s from
  `projects/checked_push_limits.py`; `docs/changed/2026-10-22-testrail-checked-results.md`):
  - It sends **every checked case of the run** that still has something to send
    (`_READY_Q` = checked, has a source run, no skip reason, no result id;
    `_RESUME_Q` = has a result id, screenshots still to attach). `plan_checked_push`
    loads every checked pivot of the run (2 queries).
  - **What blocks a send during a run:** the run status alone. The gate
    `_FINISHED_STATUSES = (DONE, CANCELLED)` appears in three places:
    - `_disabled_reason`: "Results can be sent once the run has finished."
    - `start_checked_push`: raises `RunNotFinishedError`.
    - `_take_push_slot`: the conditional UPDATE filters on `status__in=_FINISHED_STATUSES`,
      so a concurrent reset (WAITING) wins.
    - The header button in `_testrail_checked_push_actions.html` is also
      `x-show` finished only.
    Nothing else in the send depends on the run being finished. Checking is
    already allowed during a run (`check_run_cases` has no run-status gate; the run
    page shows a checkbox for every case that has a verdict).
  - **One send slot per run:** `TestRun.testrail_source_push_status/_started_at/_pushed_at/_error/_summary`.
    The slot token is the ISO `_started_at`. A redelivered or taken-over task gets
    `None` from `get_checked_push_run` and does nothing. A PUSHING status older
    than the hard limit + 60 s (`CHECKED_PUSH_STALE_AFTER`) is stale and is turned
    into FAILED (`_fail_stale_push`).
  - **Idempotence:** `_record_results` stores `testrail_source_result_id` and
    `testrail_source_sent_at` per batch. `_attach_screenshots` stores
    `testrail_source_attachment_count` after every file. `_is_complete` (result id
    set and counter ≥ flagged screenshots) counts a case as `already_sent`, so a
    later full send skips it. **Confirmed.**
  - **Evidence:** `check_run_cases` (atomic, `select_for_update` on the pivots)
    calls `freeze_testrail_evidence` before setting `checked_at`. The flags must not
    change while a case is checked: they are the resume index
    (`docs/changed/2026-10-28-testrail-relevant-screenshots.md`).
  - **Skip reasons** (`testrail_source_skip_reason`: not in run, run closed or
    missing, no case id) keep a checked case out of later sends until it is
    unchecked or checked again.
  - The run page receives `test_run.testrail_source_push` → `{"type": "testrail_source_push", "push_status"}`
    on `TestRunConsumer`. `testRunLive` reloads once when a send settles.
    `_testrail_checked_push_status.html` is shown in every run status.
- **Which actions are refused while a send is live** (`is_checked_push_live`):
  - `uncheck_run_cases` (locks the run row);
  - `reset_test_run` (locks the run row; finished runs only);
  - `apply_result_change` (via `list_testrail_push_blockers`);
  - `list_reopen_blockers`, which includes `list_testrail_push_blockers`. Its callers:
    - Resume (`run_resume_services`), Re-run (`run_rerun_services`) and the review redesign
      re-run (`case_review_redesign_services`). These run on finished runs only,
      and they clear Checked/Sent on the cases they reopen.
    - **And the wake paths of a PAUSED run**: `queue_pending_cases` (help
      Continue / Mark failed / Mark blocked / help-mode switch in
      `case_help_services`; the person step report in `person_step_services._after_action`,
      inside a savepoint), `get_help_panel`, `get_person_step_panel` and
      `run_pause_panel.get_run_pause_panels`.
    Today the wake paths can never meet a live send (a send needs a finished run).
    With early sends they can: an early send live during a Human in the loop run
    would refuse the next person step report's re-queue. The run would stay PAUSED
    behind a "Continue run" banner reading "Wait for the checked results to finish
    sending to TestRail". This has to change (Design, "Wake gates").
- **Clearing Checked/Sent when a verdict is replaced** (confirmed):
  - Resume and the review redesign re-run use `reset_case_for_restart` → `clear_checked_and_sent`.
  - Re-run in place uses `run_rerun_services._reset_in_memory` and its UPDATE.
  - An applied result change uses `clear_checked_and_sent` + `CHECKED_SENT_FIELDS`.
  - `reset_test_run` clears all of them.
  The next send then adds a **new** TestRail result for the case in the same
  source run. TestRail keeps both, and the latest decides the test's status.
- **Case worker after the verdict** (`projects/services.py`):
  - `_finalize_pivot` writes the FAILED verdict with `update_fields`.
  - It then runs teardown (`_safe_teardown`, may take minutes), cleanup and
    `_update_test_run_status_if_needed`.
  - Every later write to the pivot is narrow: logs via `Concat` UPDATE,
    `close_case_segment`, and new screenshots whose `testrail_evidence` defaults
    to False. No full `save()` of the pivot can overwrite `checked_at` or the
    result id, and teardown screenshots never join a frozen evidence list.
  - Workers of an earlier chain stand down by `execution_generation`.
  - A FAILED case is final for its execution. Help cases are NEED_HELP, person
    steps AWAITING_PERSON, a HITL "different" step keeps the case IN_PROGRESS
    until the verdict (`docs/changed/2026-10-30-hitl-continue-after-failed-step.md`).
- **Source run** exists only for cases imported with a TestRail run link
  (`TestRunTestCase.testrail_source_run_id`, snapshotted when the case joins the
  run). Suite imports have none; their only route is the 018 "Push to TestRail"
  (`testrail_results_services`), which creates a new TestRail run, finished runs only.
- **Failures page** (`dashboard/failure_views.py`, `dashboard/services/failed_case_bulk.py`,
  `templates/dashboard/includes/_failed_case_table.html`, `_failed_case_bulk_bar.html`):
  - It lists FAILED pivots of the user's member projects, **including those of runs still running**.
  - `failed_cases_bulk` re-resolves ids against the writable projects
    (`member_project_choices_and_writable_ids`), at most `BULK_MAX_CASES = 100`.
  - Bulk `send` calls `start_checked_push` per run. That sends every checked,
    unsent case of the run, not only the selected ones, and refuses running runs.
    The dialog says so.
  - The GET view loads only `member_project_choices`, so it does not know which
    rows are writable.
  - The list queryset (`failed_case_list_queryset`) loads no project TestRail settings.
- **Run page** (`views._build_test_run_detail_context`, `test_run_detail.html`):
  - `runCaseChecks(checkIds, uncheckIds, rerunIds)` in `static/js/punk-hazard.js`
    drives the bulk bar.
  - The Checked column shows Checked / Sent to R… / skip reason.
  - Checkable ids are rendered at page load. A case that fails after the page
    loaded gets its checkbox on the next reload (existing behaviour).
- **Case page** (`views.test_run_case_detail`, `test_run_case_detail.html`):
  header Mark as checked / Uncheck / "Sent to TestRail R…" badge. It listens only
  to the case socket (`TestRunCaseConsumer`), not the run's.
- **Authorization:** project views use `@project_membership_required` +
  `@require_POST` (a read-only member gets 403 on POST). Templates gate on
  `project_can_write`.

## Goals

- A writable member can send a **Failed** case to its source TestRail run while the
  run is Running or Paused, and also after it finished. Entry points:
  - the case page (one case);
  - the run page (one case from its row, or the selected cases from the bulk bar);
  - the Failures page (one case from its row, or the selected cases from the bulk bar).
- One action **checks and sends** the cases: it freezes their evidence the way
  Check does, then sends exactly the selected cases. Other checked cases of the
  run are not swept along.
- It uses the same slot, task, plan, send, idempotence, skip reasons, summary and
  socket event as "Send checked". No new model field, no migration.
- An early send never stalls the run. Case workers continue, and the wake paths
  of a PAUSED run (help Continue, person step report, Continue run) are not
  refused because a send is live.
- Resume / Re-run / Reset / result change / Uncheck keep refusing while a send is
  live (unchanged; they replace or un-check results a send may be posting).

## Non-goals

- Passed cases before the run finishes. The run-level "Send checked to TestRail"
  stays finished-only (Q5).
- Cases without a source TestRail run (suite imports). They show why they can't be
  sent, and the 018 push stays the only route for them (Q2).
- More than one send at a time per run, or queueing a send behind a live one (Q8).
- Live "Sent" updates on the case page and the Failures page. They show a flash
  message, and the mark appears on reload. The run page keeps its live status line
  and reloads when the send settles (Q6).
- Giving a case that failed after the page loaded its checkbox or Send button
  without a reload (same as Check today).
- Test session activity for sends (Check isn't recorded either).
- Any change to the 018 "Push to TestRail" (new TestRail run).

## Design

### Rules for sending a case early ("case send")

A selected case is sent when all of these hold, re-checked under lock:

1. It belongs to the run, and the run is not WAITING
   (`CASE_SEND_RUN_STATUSES = (STARTED, PAUSED, DONE, CANCELLED)`). A reset to
   WAITING wins the race, as today.
2. `status in CASE_SEND_STATUSES = (FAILED,)` (Q1: add BLOCKED if wanted; one
   constant). A FAILED case is final, so "running / Need help / Your turn" is
   excluded by construction.
3. `testrail_source_run_id is not None`.
4. `testrail_source_result_id is None` (not sent yet in this execution). A case
   whose result went out but whose screenshots were interrupted is finished by the
   next run-level "Finish sending" (finished runs). An early case send does not
   resume it (Risks).
5. The project has TestRail settings.

**Checked/Sent semantics (Q3, recommended):** "Send to TestRail" checks and sends in
one action. Unchecked selected cases are checked by the acting user, with the same
evidence freeze as `check_run_cases`. Cases already checked keep their reviewer,
so the TestRail comment still says who checked them. The action also clears a
stale `testrail_source_skip_reason` on the selected cases, because pressing Send
on a case is an explicit retry. Rejected alternative: requiring a separate Check
first. It doubles the clicks on the path whose purpose is speed, and it adds no
safety: the confirm dialog says the cases will be marked checked by you.

**Slot (Q8, recommended):** the case send takes the **same** per-run slot. A live
send, early or full, refuses a second one with "A send is already in progress"
(`CheckedPushInProgressError`). Small sends take seconds. Two sends on one run
must not overlap, because both could plan the same pivot and post it twice.
Keeping the single slot keeps the stale and takeover logic and the run-row locks of
Uncheck and Reset valid unchanged.

**Double posting:** a case send never selects a case with a result id (rule 4). The
plan's `_is_complete` keeps a later full send from posting it again. When the
verdict changes later (Resume / Re-run / applied result change / Reset), its
Checked/Sent marks are cleared and the next send adds a new result to the same
TestRail run. This is existing behaviour, and the plan keeps it (Q7 asks the user
to confirm).

### Data model

No change. No migration.

### Services

- `projects/testrail_checked_push_services.py`:
  - **Shared slot pieces become public** so the new use case reuses them, not a copy:
    - `take_checked_push_slot(test_run, *, run_statuses)`: the current `_take_push_slot`,
      with the status filter parameterised. `start_checked_push` passes
      `_FINISHED_STATUSES`, and its `RunNotFinishedError` message stays.
    - `fail_stale_checked_push(test_run)`: the current `_fail_stale_push`.
    - `dispatch_checked_push(test_run, *, links_base_url, slot_token, pivot_ids)`:
      broadcast + `send_task`. On a broker error it releases the slot
      (`mark_checked_push_failed`) and raises `CheckedPushError`, the existing code
      lifted out of `start_checked_push`.
  - `plan_checked_push(test_run, *, links_base_url, pivot_ids: Sequence[int] | None = None)`
    and `push_checked_results(..., pivot_ids=None)`. With `pivot_ids`, the plan
    query adds `id__in=pivot_ids`, still 2 queries. Everything else is unchanged,
    including the `checked_at` / skip-reason filter, so a case un-checked or
    reopened since dispatch is left out.
  - `start_checked_push`, `get_checked_push_panel` and `_disabled_reason`: behaviour
    unchanged (finished runs only).
- `projects/run_case_check_services.py`: extract the body of `check_run_cases` after
  the lock into `check_locked_pivots(pivots, *, user, now) -> int` (freeze
  evidence + one UPDATE setting `checked_at/checked_by`, clearing the skip reason).
  `check_run_cases` keeps its contract and calls it. This module stays the only
  writer of evidence flags.
- **New** `projects/testrail_case_send_services.py` (one use case: send selected
  cases now):
  - `CASE_SEND_STATUSES`, `CASE_SEND_RUN_STATUSES`.
  - `is_case_sendable(pivot) -> bool`: pure, no query, rules 2 to 4 on a loaded pivot.
    Run and case pages use it on rows they already loaded.
  - `list_case_sendable_pivot_ids(test_run) -> list[int]`: 1 query. It gives the
    allowed choices for `RunCaseSelectionForm`.
  - `start_case_send(*, test_run, pivot_ids, user, links_base_url) -> CaseSendOutcome`
    (`sending`, `checked_now`, `skipped`):
    1. Not configured → `TestRailNotConfiguredError`. Run WAITING →
       `RunNotFinishedError` (the "nothing to send" wording fits better; see the
       steps).
    2. `fail_stale_checked_push`.
    3. `transaction.atomic`:
       - `take_checked_push_slot(run_statuses=CASE_SEND_RUN_STATUSES)` (a
         conditional UPDATE that locks the run row first; lock order run → pivots
         like Uncheck and Reset).
       - `select_for_update` the selected pivots that pass rules 1 to 4
         (`.only("id", "status", "checked_at")`). If there are none, raise
         `NothingToSendError` ("None of the selected cases can be sent: …"). The
         rollback releases the slot.
       - `check_locked_pivots` on the unchecked ones.
       - One UPDATE clearing `testrail_source_skip_reason` on the already-checked ones.
    4. After commit, `dispatch_checked_push(..., pivot_ids=[the sendable ids])`.
       A dispatch failure leaves the cases checked (they go with the next send)
       and raises `CheckedPushError`.
  - `format_case_send_message(outcome) -> str`, e.g. "Sending 3 cases to TestRail… · 1 skipped".
  - Import direction: this module imports `testrail_checked_push_services` and
    `run_case_check_services`, never the reverse.
- `projects/exceptions.py`: no new class. `NothingToSendError`,
  `CheckedPushInProgressError`, `RunNotFinishedError` and `CheckedPushError` cover
  the refusals.
- **Wake gates** (`projects/run_reopen_services.py`):
  - New `list_wake_blockers(test_run, project)`: everything in `list_reopen_blockers`
    except `list_testrail_push_blockers`.
  - `list_reopen_blockers` becomes `list_wake_blockers` + the push blockers.
  - Switch the PAUSED-run wake paths to `list_wake_blockers`: `queue_pending_cases`,
    `case_help_services.get_help_panel` and `_resolve`,
    `person_step_services.get_person_step_panel`, and `run_pause_panel.get_run_pause_panels`.
  - Resume, Re-run and the redesign re-run keep `list_reopen_blockers`.
  - Why this is safe: a wake path only touches the helped case (NEED_HELP), the
    person step case (AWAITING_PERSON) and CREATED cases. None of them is ever
    checked or selected by a send. The send itself only writes its own pivots'
    `testrail_source_*` fields.
  - `apply_result_change`, Uncheck and Reset keep refusing under a live send.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_cases_send_testrail` `/projects/<id>/test-runs/<run_id>/cases/send-testrail/` (new, `projects/testrail_views.py`) | POST | `@project_membership_required` (writable) + `@require_POST` | `RunCaseSelectionForm(list_case_sendable_pivot_ids(run))` + `next` (same-host, as Check uses `_next_or_default`) | Redirect to `next` (run page or case page). Flash `format_case_send_message` | 404 for a foreign run. Form error → "can no longer be sent, reload". `CheckedPushError` / `TestRailNotConfiguredError` → flash `exc.message` |
| Run page `projects:test_run_detail` (GET, changed) | GET | unchanged | — | New context `sendable_pivot_ids` (ids on this page, from `is_case_sendable`, no query) and `case_send_available` (`testrail_checked_push.configured and .has_any_source`) | — |
| Case page `projects:test_run_case_detail` (GET, changed) | GET | unchanged | — | New `can_send_testrail` (`configured and is_case_sendable(pivot)`) | — |
| `dashboard:failed_cases` (GET, changed) | GET | `@login_required`, rows scoped to member projects | — | Uses `member_project_choices_and_writable_ids` (same single query). New `sendable_ids`: page ids that are sendable **and** in a writable project with TestRail configured | — |
| `dashboard:failed_cases_bulk` (POST, changed meaning of `action=send`) | POST | `@login_required`; ids re-resolved against writable projects | `FailedCaseBulkForm` (unchanged fields; the per-row Send posts one id) | Redirect to Failures with the same filters | Per-run messages as today ("<project> · <run>: …") |

**Run page** (`test_run_detail.html`, `runCaseChecks`):

- `runCaseChecks` gains a 4th id list, `sendIds`, and a `toSend` getter.
- Bulk bar (writable only, `case_send_available`): "Send to TestRail (N)", in **every** run status, disabled when `toSend` is empty or `testrailSourcePushStatus === 'pushing'`.
- The button opens a confirm dialog inside the `runCaseChecks` scope, following the Re-run confirm pattern in `partials/README.md`. The text explains:
  - it sends the N failed cases now, to their source TestRail run(s);
  - unchecked ones are marked checked by you;
  - the failure screenshots are attached;
  - other cases are not sent.
- Per row: a small "Send" button in the Checked column for ids in `sendIds`. It dispatches `open-send-testrail-cases` with `{ ids: [id] }`. The dialog keeps the event's ids, or falls back to `toSend`, so the user's selection is not changed.
- The hint text names the new action.
- Rows without a source run (when the run has some that do) need no new markup. The status partial already counts "without a source TestRail run".

**Case page:** for writable members with `can_send_testrail`, a "Send to TestRail"
button next to Mark as checked, which goes through `partials/confirm_dialog.html`.
It posts the single id with `next` set to the case URL. A Failed case without a source
run shows a muted line instead: "Can't send: imported without a TestRail run link".

**Failures page:**

- Bulk bar: replace "Send checked to TestRail" with "Send to TestRail (N)", where N = selected ∩ `sendable_ids`.
  - The ids map is rendered like `runOf`, in the template's outer `x-data`, so `punk-hazard.js` is not touched.
  - The dialog text now says it checks and sends **only the selected failed cases**, per run, also for runs still running.
- Per row: a "Send" button in the Review column for `sendable_ids`. It opens the same dialog with `{ ids: [id] }`.
- `BulkAction.SEND` label becomes "Send to TestRail".
- `apply_failed_case_bulk` calls `start_case_send(test_run=..., pivot_ids=run_pivot_ids, user=...)` per run, newest run first as today, and reports `format_case_send_message`.
- `failed_case_list_queryset` gains an annotation `testrail_configured` (an expression over the project's three TestRail columns). The encrypted key is never loaded into memory, and there is no N+1.

### Background work and WebSockets

- Task `push_checked_results_to_testrail(test_run_id, links_base_url, slot_token, pivot_ids: list[int] | None = None)`,
  queue `upload`, limits unchanged.
  - `None` = full send (old in-flight messages still work).
  - A list = case send.
  - The ids list is at most `BULK_MAX_CASES` (100) from the Failures page, or one
    run page of pivots from the run page.
  - Ids are not secrets.
- The case worker keeps running cases on `execution` while the send runs on
  `upload`. They share only the run row (a millisecond row lock at slot take) and
  disjoint pivot columns.
- Events: unchanged (`test_run.testrail_source_push` on the run group).
  - The run page shows "Sending checked results to TestRail…" during an early send
    and reloads when it settles (existing `testRunLive` logic).
  - That reload also happens during a running run, which is acceptable: the page
    already reloads on help-state changes.

### Settings / infrastructure

No new settings. On deploy, restart the `upload` worker **before or together with**
web/Daphne: an old worker given the 4th argument fails before the task body runs,
and the slot stays PUSHING until it is stale (Risks).

### Data flow

1. A writable member presses Send to TestRail. The source is a case page, a run page
   row or bulk bar, or a Failures page row or bulk bar.
2. The view validates the ids against the run's sendable choices. The Failures page
   instead re-resolves them through the writable failed-case scope and groups them
   per run.
3. `start_case_send` runs per run:
   - it releases a stale slot;
   - in one transaction it takes the slot, locks the eligible pivots, freezes
     evidence and checks the unchecked ones, and clears skip reasons;
   - after commit, it broadcasts PUSHING and dispatches the task with the pivot ids.
4. The `upload` worker validates the slot token and plans only those pivots. For
   each source run it gets the run (closed or missing → skip reason) and the case
   ids (not in run → skip reason), adds the results in batches, and attaches the
   frozen screenshots with the counter. It then stamps the run summary and status
   and broadcasts.
5. The run page reloads. Its Checked column shows "Sent to R…", and the Failures
   Review column shows the same on its next load. Meanwhile the run's other cases
   keep executing, and help / person-step wake paths are not gated on the send.
6. After the run finishes, "Send checked to TestRail" sends the remaining checked
   cases and counts the early ones as "already sent".

## Alternatives considered

- **Lift the finished gate from "Send checked" entirely.** This is the smallest diff,
  but it sends every checked case including Passed ones mid-run. It also gives no
  per-case send and keeps the surprising "whole run" scope on the Failures page.
  Rejected.
- **A separate early-send slot (new fields on `TestRun`).** It would allow an early
  send next to a full send, but it needs a migration, duplicates the stale/takeover
  logic, and needs per-pivot claim locking to avoid double posts. A simple single
  slot is enough for sends that take seconds.
- **Scope by status instead of ids** (an early send carries every checked Failed
  case). It is simpler for the task, but it sends cases the user did not select.
  The ids argument is a single `id__in`.
- **Send without checking.** It would break the "Checked by" line, the evidence
  freeze (the only writer of the flags) and the Checked filter on the Failures
  page. Rejected.
- **Keep the push blockers on the wake paths and make sends short.** A send with
  screenshots can still take minutes on a slow TestRail. Stalling a HITL person
  step for that is worse than the narrow split.

## Risks and mitigations

- **Wake gate split.** The risk is that a wake path that does touch checked cases is
  added later. Mitigation: `list_wake_blockers` documents the invariant, which is
  "only NEED_HELP / AWAITING_PERSON / CREATED cases". The reviewer checks every
  `queue_pending_cases` caller.
- **Lock order:** run row (slot UPDATE) → pivots. This matches the other paths, so
  no lock cycle:
  - Uncheck and Reset use run → pivots.
  - `apply_result_change` (`_lock_case` → `lock_project_and_run` → case) uses
    project → run → case. The case send never locks the project, so its locks are
    a suffix of that order.
  - `check_run_cases` locks pivots only.
  - The case worker's `finish_test_run` is a single conditional UPDATE on the run.
- **An interrupted early send** (soft time limit or killed worker) leaves the case's
  result posted with screenshots incomplete. A case send won't resume it, because
  rule 4 excludes cases with a result id. The run-level "Finish sending" does,
  after the run finishes. Acceptable; it is documented in the change note.
- **Deploy ordering** of the task signature (see Settings). Restart `upload` first.
  The worst case is one stuck slot that turns into FAILED after about 62 minutes.
- **Failures bulk across many runs.** The cost is up to 100 runs × (≤ 9 queries + 1
  `send_task`) per request, the same shape as the existing bulk Check/Send. It also
  adds up to 100 `upload` tasks, each holding a worker slot for seconds, which
  delays imports briefly.
- **Behaviour change of the Failures bulk Send.** It now sends only the selected
  cases and checks them (Q4). Users relying on "send every checked case of the run"
  use the run page button.
- **XSS:** no new user text is rendered. Run, project and case names already use
  autoescape. The ids maps are ints.
- **Secrets:** the TestRail key is never loaded on the Failures list (annotation).
  The task gets ids only.

## Implementation steps

Each step is one commit. **Shared code first:** steps 1 and 2 are independent of
each other (disjoint files) and can run as two parallel sub-agents. Steps 3 and 4
need step 1 (and should land after step 2), and they touch disjoint files, so they
can also run in parallel. Step 5 is the orchestrator's integration and docs.

1. **Scoped send + case send service (shared).** Files:
   - `projects/testrail_checked_push_services.py`: public `take_checked_push_slot`,
     `fail_stale_checked_push`, `dispatch_checked_push`; `pivot_ids` on
     `plan_checked_push` / `push_checked_results`; `start_checked_push` uses them.
   - `projects/run_case_check_services.py`: `check_locked_pivots`.
   - New `projects/testrail_case_send_services.py`: `CASE_SEND_STATUSES`,
     `CASE_SEND_RUN_STATUSES`, `is_case_sendable`, `list_case_sendable_pivot_ids`,
     `start_case_send`, `CaseSendOutcome`, `format_case_send_message`.
   - `projects/tasks.py`: the `pivot_ids=None` parameter, passed through.
   Interfaces later steps consume: `is_case_sendable(pivot) -> bool`,
   `list_case_sendable_pivot_ids(test_run) -> list[int]`,
   `start_case_send(*, test_run, pivot_ids, user, links_base_url) -> CaseSendOutcome`,
   `format_case_send_message(outcome) -> str`.
2. **Wake gates (parallel with 1).** Files: `projects/run_reopen_services.py`
   (`list_wake_blockers`, `queue_pending_cases`), `projects/case_help_services.py`,
   `projects/person_step_services.py`, `projects/run_pause_panel.py`.
3. **Run page and case page (after 1 and 2; parallel with 4).** Files:
   - `projects/testrail_views.py` (`test_run_cases_send_testrail`), `projects/urls.py`;
   - `projects/views.py` (`sendable_pivot_ids`, `case_send_available`, `can_send_testrail`);
   - `templates/projects/test_run_detail.html` (bulk bar button, per-row Send,
     confirm dialog, hint, context contract comment);
   - `templates/projects/test_run_case_detail.html` (button, confirm, "can't send" line);
   - `static/js/punk-hazard.js` (`runCaseChecks` 4th list, `toSend`).
   - Optional: a page-private include `templates/projects/includes/_testrail_case_send_dialog.html`
     if the dialog is shared by both pages.
   Uses `RunCaseSelectionForm` unchanged.
4. **Failures page (after 1 and 2; parallel with 3).** Files:
   - `dashboard/services/failed_case_bulk.py` (SEND → `start_case_send`, label, docstring);
   - `dashboard/services/failed_case_filters.py` (`testrail_configured` annotation);
   - `dashboard/failure_views.py` (writable ids on GET, `sendable_ids`);
   - `templates/dashboard/failed_cases.html` (contract comment);
   - `templates/dashboard/includes/_failed_case_table.html` (send-ids map, per-row Send, hint);
   - `templates/dashboard/includes/_failed_case_bulk_bar.html` (button, dialog text, ids from event).
   It must not touch `punk-hazard.js` (owned by step 3).
5. **Integration and docs (orchestrator).**
   - `pre-commit run --all-files`, `python manage.py check`, and `makemigrations --check` (expects no changes).
   - `CLAUDE.md`: one line under Key Patterns or the TestRail notes, saying that failed
     cases can be sent to their source run during a run (case send = check + send,
     same slot) and that PAUSED-run wake paths use `list_wake_blockers`.
   - `docs/changed/2026-10-30-early-failed-testrail-push.md`; set this plan to Done.

## Verification

No test suite. Use a throwaway or dev database and a TestRail sandbox if available.

- `pre-commit run --all-files` (black, isort, strict mypy), `python manage.py check`,
  `python manage.py makemigrations --check` (no changes).
- Run with run-link-imported cases:
  - **Running run, case fails:** reload the run page. The row has a Send button,
    and the bulk bar shows "Send to TestRail (1)". Send it. The status line shows
    "Sending…", the run keeps executing the next case, the page reloads, and the
    row reads "Sent to R…". In TestRail: one result, Failed, the comment says
    "Checked by <you>", and only the frozen screenshots are attached.
  - **HITL run paused on a person step while an early send is live** (slow it
    down, e.g. many screenshots or a paused upload worker so the slot stays
    PUSHING): report the step. The run re-queues; no "Wait for the checked
    results…" banner.
  - **Help Continue on a Need help case during a live early send:** accepted.
  - **Apply a result change, Uncheck or (after finish) Reset/Resume/Re-run during a
    live send:** still refused with the existing message.
  - **Second Send while one is live:** "A send is already in progress", nothing checked.
  - **Already-sent case:** the Send button is gone; a forged POST gets "can no
    longer be sent".
  - **Run finishes; "Send checked to TestRail":** the early case counts as
    "already sent", no second result.
  - **Re-run that case after finish, fail again, send:** a second result appears in TestRail (expected).
  - **Case without a source run (suite import):** no Send. The case page shows the
    "can't send" line, and a forged POST is refused.
  - **Send a case that is not in the TestRail run:** the skip reason "Not sent: not
    in R…" appears. Fix it in TestRail and press Send again: the skip reason is
    cleared and the result is sent.
  - **Read-only member:** no buttons. A forged POST to `cases/send-testrail/` gets
    403. On the Failures page, cases of read-only projects are counted as skipped.
  - **Failures page:**
    - Per-row Send on a case of a running run: it is sent, and the flash names
      the project and run.
    - Bulk Send across 2 runs, with one TestRail-unconfigured project: one success
      and one error message.
    - Only the selected cases of each run go out. A checked, unsent case of the
      same run that was not selected stays unsent.
  - **Interrupted send** (kill the `upload` worker mid-attachments): after the
    stale window the status shows "interrupted". After the run finishes, "Finish
    sending" resumes without duplicates.
- Query counts (`CaptureQueriesContext` in a shell smoke on a throwaway DB):
  - `start_case_send` with 1 vs 50 cases gives the same count (≤ 9).
  - The Failures GET is unchanged versus before (membership query swapped, annotation in the list query).
  - The run page GET is unchanged.
- Run the `reviewer` and `verifier` agents in parallel. Give the verifier the
  original request and this plan path.

## Open questions

- Q1 **Blocked early too?** The request says failed. Blocked is only ever set by a
  person (Mark blocked / an applied result change), so it is also a deliberate
  signal worth flagging. *Recommendation / draft assumption:* Failed only for now;
  Blocked is one constant (`CASE_SEND_STATUSES`) if wanted.
- Q2 **Cases without a source TestRail run** (suite imports, or cases imported
  before run-link import existed): no early send. The UI says why ("imported
  without a TestRail run link"), and the 018 Push to TestRail after the run remains
  their route. *Assumed: yes.*
- Q3 **Check + send in one action**, or require Check first? *Recommended / assumed:*
  one action. Unchecked cases are marked checked by the sender (evidence frozen
  then), and already-checked cases keep their reviewer.
- Q4 **Failures page bulk Send:** today it sends every checked, unsent case of each
  affected run, and only finished runs. *Recommended / assumed:* replace it with
  "check and send only the selected failed cases", allowed for running runs too.
  The run-page "Send checked to TestRail" stays for the whole-run sweep.
- Q5 **Run-level "Send checked to TestRail" during a run** (checked Passed cases
  too)? *Recommended / assumed:* no. It stays finished-only; only the per-case /
  selected send works mid-run.
- Q6 **Live "Sent" on the case page and Failures page?** *Recommended / assumed:*
  no. Show a flash message plus the mark on reload; the run page keeps its live
  status and reload.
- Q7 **A verdict that changes after an early send** (Resume / Re-run / applied result
  change after the run) is sent again later as a **new** TestRail result in the
  same run; TestRail shows the latest. This is existing behaviour for sent cases.
  *Assumed acceptable.*
- Q8 **A send already in progress for the run:** refuse with "A send is already in
  progress" (nothing is checked), or queue the cases for after it? *Recommended /
  assumed:* refuse. Sends of a few cases take seconds, and queueing would need new
  state.
- Q9 **Same action on finished runs:** the case page / run page / Failures Send also
  works after the run finished (one rule, any run status except Draft).
  *Recommended / assumed:* yes.

## Changelog

- 2026-10-30: created (Draft).
- 2026-10-30: user accepted every recommendation (Q1–Q9 as drafted); Ready, then In progress.
- 2026-10-30: review pass 1 fixed: reopen blocker order kept as before (`_list_blockers`), Failures rows without a source run say why, `is_case_missing_source` moved to the service, the send view reuses the resolved project, `start_case_send` refuses inside an outer transaction, `sendableOf` getter on the Failures page. Not changed: `getattr` for the `testrail_configured` annotation (typed with `bool()` and commented, as the module already does for annotations); the case page Send is not disabled during a live send (no live run state there, Q6; the server refuses); a partly sent case is finished by the run-level Finish sending only (Risks).
