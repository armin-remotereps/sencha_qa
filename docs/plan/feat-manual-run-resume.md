# feat: Resume a Manual run

Status: Done
Created: 2026-11-01
Request: A Manual run that was aborted cannot be resumed today: the "Resume run"
button is not shown for Manual runs and the services refuse them. A writable
member must be able to resume a Manual run that is Cancelled (or Done with
Cancelled cases left) from the run page, so its unfinished cases go back to the
Test Runner's list and a tester can perform them. "Resume" for a Manual run means
something different from an agent run: there is no agent, no chain and no
checkpoint to continue. The run goes back to Running, its Cancelled cases go back
to Created, and the tester starts each of them again at the Test Runner from step
1. The existing gates still apply: one active run per project, a connected Test
Runner that supports manual runs, no live TestRail send or push, no setup turn or
runner update in progress. The resume is recorded in the test session. The run
cannot switch to or from Manual on Resume.

## Context

- **Resume today** (`projects/run_resume_services.py`):
  - `get_resume_panel` returns an empty panel for `is_manual_run(test_run)`, so
    `templates/projects/test_run_detail.html` renders no button and no dialog.
  - `can_resume_case` is False for a manual run or a manual checkpoint.
  - `_resume_pivots` refuses a manual run, a mode crossing the manual
    boundary, or any manual checkpoint with `manual_run_policy.MANUAL_RESUME_REFUSED`.
  - For agent runs it locks project → run (`run_reopen_services.lock_project_and_run`),
    checks `REOPENABLE_RUN_STATUSES` (DONE, CANCELLED) and `list_reopen_blockers`,
    selects BLOCKED + CANCELLED cases, records `CaseActivity.RESUME`, credits the
    resumer, writes `TestRunCaseResume` snapshots, and calls
    `reset_case_for_restart` and `reopen_run`.
- **Reopen machinery is already manual-aware** (`projects/run_reopen_services.py`):
  - `list_reopen_blockers` uses `_manual_machine_blocker` for a manual run:
    runner connected plus `runner_runs_manual_tests` (MANUAL_RUN_V1). The visual
    engine is not needed. It also covers another active run, TestRail
    send/push, a live setup turn and a runner update.
  - `reopen_run` sets STARTED, bumps `execution_generation`, and for a manual
    run only broadcasts `broadcast_run_and_pivots` (which nudges the runner).
    It queues no chain.
  - Re-run in place (`projects/run_rerun_services.py`) already works for manual
    runs. It refuses mode crossings (`MANUAL_MODE_SWITCH_REFUSED`), clears the
    claim (`MANUAL_CLAIM_CLEARED`), supersedes hand-ins
    (`manual_submission_supersede.supersede_manual_submissions`) and deletes
    the screenshots.
- **What abort leaves on a manual case** (`projects/services.py`
  `abort_test_run` ~4567, `_mark_pivot_cancelled` ~4546):
  - CREATED and IN_PROGRESS (claimed) cases become CANCELLED with
    `finished_at`, and the open segment is closed.
  - Claims are cleared, so a late hand-in is dropped.
  - `execution_checkpoint` is kept. A case that was claimed still has its
    manual plan (`manual: true`, no attempts). A case that was never claimed
    has `{}`.
  - A **Cancelled manual case never has a live hand-in**. A hand-in sets the
    case to SUCCESS/FAILED/BLOCKED under the run lock, abort only cancels
    CREATED/IN_PROGRESS cases under the same lock, and Re-run/Reset supersede
    hand-ins before a case returns to CREATED.
- **Claim** (`projects/manual_case_claim_services.claim_manual_case`): needs the
  run STARTED and manual and the case CREATED. It rebuilds the checkpoint from
  the verbatim steps (`build_manual_checkpoint`), mints a new token and opens a
  segment. The runner window keys its local draft on the claim token
  (`controller_client/person_window_manual.py` `apply_state` / `_load_claim`),
  so a new claim starts a fresh draft.
- **Finish**: `manual_run_finish.finish_manual_run_if_settled` (called on hand-in
  and on Needs re-test of the claimed case) finishes the run when no case is
  CREATED/IN_PROGRESS/NEED_HELP/AWAITING_PERSON. Other Cancelled cases do not
  keep the run open.
- **Credit**: manual Start gives no credit (`start_test_run`); the hand-in
  credits the tester (`docs/plan/feat-manual-run-mode.md` Changelog).
- **Case page**: `projects/views.py` `test_run_case_detail` builds `can_resume`,
  `resume_steps` (`case_checkpoint_services.list_resume_steps`) and
  `resume_form`. `templates/projects/test_run_case_detail.html` has the "This case
  was cancelled" card and the Resume case dialog with a step select and
  `_help_mode_select.html`. The Blocked card's `manual_verdict` hint already
  points to Re-run, not Resume.
- **Run page dialog**: `test_run_detail.html` ~669 renders `confirm_body` and
  `_help_mode_select.html` (a select for superusers, "Run mode: X" text for
  everyone else).

## Goals

- "Resume run" is shown on a Manual run that is Done or Cancelled and has at
  least one Cancelled case. Pressing it puts those cases back to Created and the
  run back to Running (manual, no Celery, no agent). The Test Runner window
  lists the cases again, so a tester can claim them.
- "Resume case" on the case page does the same for one Cancelled manual case.
- Same gates as other Resumes: writable member (POST + decorator), one active
  run per project, a connected manual-capable Test Runner, no live TestRail
  send or push, no live setup turn or runner update.
- The Resume is recorded in "Earlier attempts" (`TestRunCaseResume`) and in
  the test session (`CaseActivity.RESUME`).
- The run's mode cannot cross the manual boundary on Resume. The dialog shows
  the mode as fixed text, with no select.

## Non-goals

- Resuming manual **Blocked** cases. Blocked is the tester's verdict, and
  Re-run in place already redoes it (Q1).
- Continuing a manual case mid-way. A manual case always starts again from step
  1, because the runner window's draft belongs to the old claim, which abort
  cleared.
- Changes to the controller protocol, the runner window or `CLIENT_VERSION`.
- Changes to Re-run, Reset, abort or the help-mode select of the Re-run dialog.

## Design

### Data model

No changes and no migration.

### Services

**New `projects/manual_run_resume_services.py`** (one use case: resume a Manual
run's Cancelled cases). Its dependencies point inward:
- It imports `run_reopen_services`, `manual_run_policy`,
  `manual_submission_supersede`, `run_help_mode_policy`, `models`,
  `run_case_order` and `test_sessions`.
- It must **not** import `run_resume_services`, because that module imports
  this one.

It exposes:
- `MANUAL_RESUMABLE_STATUSES = (CANCELLED,)`: the single place to widen if Q1
  is answered yes.
- `count_manual_resumable(test_run) -> int`: one `COUNT` over the run's
  Cancelled cases.
- `build_manual_resume_confirm_body(count) -> str`: dialog text built on the
  server from the count only (see Pages).
- `can_resume_manual_case(pivot) -> bool`: no query. True when the case is
  Cancelled, the run status is in `REOPENABLE_RUN_STATUSES`, and the run is
  manual.
- `resume_manual_cases(*, test_run_id, pivot_id: int | None, user, help_mode: str | None) -> int`.
  It runs in one `transaction.atomic`:
  1. Validate `help_mode` (`is_valid_help_mode`), then lock project → run
     (`lock_project_and_run`).
  2. Gate in this order:
     - The run must be manual, else `RunResumeError(MANUAL_RESUME_NOT_MANUAL)`.
       This is defensive: the router below already decided manual.
     - The status must be in `REOPENABLE_RUN_STATUSES`.
     - `resolve_help_mode(user=, requested=, current=)`: a non-superuser may
       only keep the mode.
     - Refuse a resolved mode that crosses the boundary with
       `MANUAL_MODE_SWITCH_REFUSED`. A resolved `manual` is the same mode, so
       nothing is stored.
     - `list_reopen_blockers`: first blocker as `RunResumeError`.
  3. Select the run's cases in `MANUAL_RESUMABLE_STATUSES` (optionally only
     `pivot_id`) with `.defer("logs")` in `RUN_CASE_ORDERING`. If there are
     none, raise `RunResumeError(MANUAL_NOTHING_RESUMABLE)`.
  4. `record_case_activity(..., CaseActivity.RESUME)` before any write. This
     follows the existing rule: hooks that discard a verdict call it first.
     There is **no** `credit_cases`: credit comes with the hand-in, as at
     manual Start (Q3).
  5. Write a `TestRunCaseResume` snapshot per case (`kind=RESUME`,
     `from_step=None`).
  6. For each case in memory:
     - `reset_case_for_restart` (CREATED, clear verdict, `finished_at` and
       Checked/Sent).
     - Set `execution_checkpoint = {}`, as `release_manual_case` does, so no
       stale manual plan is shown before the next claim rebuilds it.
     - Clear the claim fields (`MANUAL_CLAIM_CLEARED`). They are already empty
       after abort; this is defensive.
  7. Save with one `bulk_update` on `RESTART_RESET_FIELDS` +
     `execution_checkpoint` + `MANUAL_CLAIM_FIELDS`.
  8. After that UPDATE, call
     `supersede_manual_submissions(pivot_ids=…)`. It is a defensive no-op
     today (see Context) and uses the case-then-hand-in lock order of Re-run.
     It keeps the invariant "a Created manual case has no live hand-in".
  9. `reopen_run(test_run, pivots, help_mode=None)`. Its manual branch
     broadcasts the run and the cases on commit and nudges the runner. It
     queues no Celery work.
  10. Return the count.

  Kept as they are: logs, screenshots, `started_at`, `active_duration`, Ask AI
  questions and earlier (superseded) hand-ins. These match the agent Resume
  rule "results stay on this run".

**`projects/run_resume_services.py`** (routing only, small changes):
- `get_resume_panel`:
  - For a manual run in a reopenable status, build the panel from
    `count_manual_resumable`: `cancelled_unstarted=count`, the other counts 0,
    and `confirm_body` from `build_manual_resume_confirm_body`.
  - `can_resume` and `blockers` follow the same rule as today.
  - Add a field `is_manual: bool` to `ResumePanel` so the template can drop
    the mode select.
  - It stays one query.
- `can_resume_case`: for a manual run, return `can_resume_manual_case(pivot)`.
  For agent runs keep the existing rule, including the refusal of a manual
  checkpoint.
- `resume_test_run` / `resume_test_run_case`: when `is_manual_run(test_run)`
  (the run the view loaded), delegate to `resume_manual_cases`.
  `resume_test_run_case` must receive `from_step=None` for a manual case. A
  value raises `RunResumeError`.
  - Routing on the unlocked read is safe. A non-draft run never crosses the
    manual boundary: `change_run_help_mode`, Re-run and the project scopes all
    refuse or skip it. Both paths also re-check under the lock and fail closed.
- `_resume_pivots` keeps refusing a manual run and manual checkpoints for the
  agent path, with the re-worded text below.

**`projects/manual_run_policy.py`** (strings only):
- `MANUAL_RESUME_REFUSED` is re-worded for the remaining agent-path refusal:
  "A manual case can be resumed only in a Manual run."
- New `MANUAL_NOTHING_RESUMABLE`: "There are no Cancelled cases to resume. To
  test a finished case again, re-run it."
- New `MANUAL_RESUME_NOT_MANUAL`: "This run is not a Manual run."
  (defensive).

**`projects/case_checkpoint_services.list_resume_steps`**: return `[]` for a
manual checkpoint (`is_manual_checkpoint`). This drops the step select and the
"agent restores the state" text for a manual case. It is a leaf import, so
there is no cycle.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_resume` (unchanged URL) | POST | `@project_membership_required` (writable) | `RunHelpModeForm` (blank for manual) | redirect to run page; flash "Resumed N case(s)." | `RunResumeError`, `HelpModeNotAllowedError` → flash error |
| `projects:test_run_case_resume` (unchanged URL) | POST | same | `RunHelpModeForm` (no step options for manual) | redirect to case page; "Resumed this case." | same |
| `projects:test_run_detail` | GET | member | — | `resume_panel` (+ `is_manual`) | — |
| `projects:test_run_case_detail` | GET | member | — | `can_resume`, `resume_steps` (empty for manual), `resume_form` None; new `resume_is_manual` (bool, from `is_manual_run(pivot.test_run)` via the service; not computed in the view) | — |

No view logic changes beyond passing the new context key. Views keep calling
`resume_test_run` / `resume_test_run_case`.

Templates:
- `test_run_detail.html`, Resume dialog: when `resume_panel.is_manual`, render
  `_help_mode_select.html` with `can_change=False`, so it shows "Run mode:
  Manual" and posts nothing. Update the context-contract comment.
- Manual confirm body, built on the server from counts only, so there is no XSS
  surface: "N cancelled case(s) go back to the Test Runner's list; a tester
  starts each one again from step 1. The run is Running until they are handed
  in or the run is stopped. Results stay on this run; Checked marks on these
  cases are cleared."
- `test_run_case_detail.html`:
  - "This case was cancelled" card hint for manual: "The run stopped before
    this case was handed in. Resume it to put it back in the Test Runner's
    list; a tester starts it again from step 1."
  - Dialog, manual branch: the same sentence instead of the step select or the
    "No progress was saved" text, and the mode as text (`can_change=False`).
  - Update the context-contract comment.
- The Blocked card's `manual_verdict` hint stays as it is: it points to Re-run.

### Background work and WebSockets

- No Celery task and no new queue. `reopen_run`'s manual branch broadcasts
  `broadcast_run_and_pivots` on commit. That updates the run and case pages,
  and the runner nudge makes the window re-read `manual_run_state`, which now
  includes the STARTED run with Created cases.
- Controller protocol: unchanged. Older runners are already excluded by the
  reopen blocker (`MANUAL_RUN_V1` via `runner_runs_manual_tests`), so neither
  `CLIENT_VERSION` nor `REQUIRED_CLIENT_CAPABILITIES` changes.
- Runner window: the old claim token is gone (cleared at abort) and the
  generation is bumped. Any local draft or spool of the aborted claim is refused
  at hand-in as before, and a new claim starts a fresh draft.

### Settings / infrastructure

None.

### Data flow

1. A writable member presses Resume run (or Resume case) → POST →
   `RunHelpModeForm` (blank) → `resume_test_run` sees a manual run →
   `resume_manual_cases`.
2. The service locks project → run, applies the gates (manual, finished, mode,
   reopen blockers), then selects the Cancelled cases.
3. It records the session activity, writes the snapshots, resets the cases to
   Created (checkpoint and claim cleared), supersedes any hand-in (no-op), and
   sets the run to STARTED with a new generation.
4. On commit it broadcasts and nudges the runner. The view flashes "Resumed N
   cases".
5. The tester claims a case at the Test Runner (`claim_manual_case`: the run is
   STARTED, the case CREATED), performs it and hands it in. The last hand-in
   with no Created/In-progress case left finishes the run as Done
   (`finish_manual_run_if_settled`).

## Alternatives considered

- **Branch inside `_resume_pivots`.** Rejected. The manual path shares almost
  nothing with it: no step choice, no help/person-step clearing, no checkpoint
  truncation, no credit, but it does clear the checkpoint and claim. Mixing
  them would break "one use case per module" and make the agent path harder to
  read.
- **Let the view call the manual service directly.** Rejected. Choosing the use
  case by run mode is a domain decision, so it stays in the service layer. The
  views and URLs stay unchanged.
- **Tell users to Re-run instead.** Re-run already works for manual runs, but
  it only selects cases one by one on the page and discards history (logs,
  screenshots). The user asked for Resume explicitly, and Resume keeps "Earlier
  attempts".
- **Include Blocked cases.** A Blocked manual case has a real hand-in with
  screenshots. Resuming it would have to supersede that hand-in and drop its
  screenshots, which is exactly Re-run. It is left out pending Q1.
- **A separate `ManualResumePanel` type.** Rejected. One `is_manual` flag on
  the existing `ResumePanel` is enough, because the template only reads
  `can_resume`, `blockers`, `confirm_body` and the flag.

## Risks and mitigations

- **Import cycle.** `run_resume_services` imports the new module, so the new
  module must not import it back. The panel is assembled in
  `run_resume_services`. The new module returns plain ints and strings.
- **Unlocked mode routing.** Covered above: the mode never crosses the manual
  boundary on a non-draft run, and both paths re-check under the lock.
- **Stale claim or draft at the runner.** Claims are cleared by abort and again
  by Resume, and the generation is bumped. A late hand-in of the old claim is
  refused (`ManualSubmissionDroppedError`), as after abort today.
- **Run stays Running when nobody picks up the cases.** This is the same as a
  manual Start. Stop run (abort) is available.
- **Ask AI cap.** Earlier questions of a resumed case still count toward its
  20-per-case cap. This is accepted: same behaviour as the agent Resume and
  Re-run keeping history.
- **No N+1.** The panel is one aggregate. Resume uses a fixed number of
  queries whatever the case count: one select, `bulk_create`, `bulk_update`,
  one supersede UPDATE, the run save, and at most 5 in
  `record_case_activity`.

## Implementation steps

1. Policy strings (files: `projects/manual_run_policy.py`).
2. New use case `resume_manual_cases`, `count_manual_resumable`,
   `build_manual_resume_confirm_body`, `can_resume_manual_case` (files:
   `projects/manual_run_resume_services.py`).
3. Routing and panel flag (files: `projects/run_resume_services.py`).
4. No step options for manual checkpoints (files:
   `projects/case_checkpoint_services.py`).
5. Case page context key `resume_is_manual` (files: `projects/views.py`,
   `test_run_case_detail` only).
6. Templates: run page dialog and case page card and dialog, plus their
   context-contract comments (files: `templates/projects/test_run_detail.html`,
   `templates/projects/test_run_case_detail.html`).
7. `pre-commit run --all-files`; the change note
   `docs/changed/2026-11-01-manual-run-resume.md`. Update the "Refusals" line
   of the manual-run note's successor and the CLAUDE.md manual-run mention if
   one says Resume is refused.

Steps 1–4 are one dependent chain and are implemented directly. Steps 5–6 can
go to one sub-agent once step 3 fixes the `is_manual` / `resume_is_manual`
names.

## Verification

Manual checks (no automated tests in this project), with a manual-capable
Test Runner connected:
1. Start a Manual run with 3 cases, claim one at the runner, then Abort. The
   run page shows "Resume run" with the manual confirm text and "Run mode:
   Manual", with no select even as a superuser.
2. Resume. The flash says "Resumed 3 cases". The run is Running, all three are
   Created, and the window lists them. Claiming the previously claimed case
   shows a fresh draft. The case page "Earlier attempts" shows "Cancelled ·
   Resumed by … from the start". The open test session has three RESUME rows.
3. Hand in all three. The run finishes Done and the hand-ins credit the tester.
   The Resume gave the resumer no credit.
4. Abort again with one case done and two Cancelled. On the case page of a
   Cancelled case, the Resume case dialog has no step select. Resume only that
   case, hand it in, and the run finishes Done with one case still Cancelled.
   "Resume run" is offered again.
5. Gates:
   - Disconnect the runner: the button is disabled with "Connect the Test
     Runner."
   - Start another run: "Another test run is already in progress".
   - A live Send checked: blocked.
   - A crafted POST with `help_mode=attended` as a superuser gets the mode
     switch refusal. As a non-superuser it gets `HelpModeNotAllowedError`.
   - A read-only member's POST gets 403.
6. A Done manual run with only Blocked/Passed cases shows no Resume button. A
   POST to Resume case on a Blocked manual case gets the "nothing to resume"
   message.
7. Regression: an agent (Attended) run still resumes Blocked and Cancelled
   cases with the step select and the mode select.
8. `mypy .` clean. Run the `reviewer` and `verifier` agents.

## Open questions

- Q1. Should manual **Blocked** cases (the tester's own "Blocked" verdict) also
  go back to the list on Resume? (Assumption in this draft: no. Blocked is a
  verdict, and Re-run in place redoes it with its hand-in superseded and
  screenshots dropped. The case page already points there. Saying yes means
  adding BLOCKED to `MANUAL_RESUMABLE_STATUSES` and superseding plus deleting
  those cases' screenshots, as Re-run does.)
- Q2. Should the case page also offer **Resume case** for a single Cancelled
  manual case? (Assumption: yes, through the same service with one case. The
  run then finishes when that case is handed in, even if other cases are still
  Cancelled.)
- Q3. Should the person who presses Resume get the **leaderboard credit** for
  the resumed cases, as in agent runs? (Assumption: no. As at manual Start,
  credit goes to the tester at hand-in.)
- Q4. Earlier hand-ins of Cancelled cases: none can exist (a handed-in case is
  Passed, Failed or Blocked, never Cancelled). The plan only adds a defensive
  supersede and keeps logs, screenshots and history. Please confirm that
  keeping that history (the "Earlier attempts" row and any older screenshots)
  is wanted rather than wiping like Re-run. (Assumption: keep.)

## Changelog

- 2026-11-01: created
- 2026-11-01: open questions answered by the user: all four assumptions confirmed (Q1 only Cancelled; Q2 case-page Resume case yes; Q3 credit to the tester at hand-in; Q4 keep history). Status In progress.
- 2026-11-01: reviewer and verifier clean. Optional nit not taken: shared Resume refusal strings (_NOT_FINISHED, _INVALID_HELP_MODE) stay per module, as in case_review_redesign_services. Status Done.
