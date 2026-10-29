# Test sessions: testing hours, projects and tests per tester, with CSV export

Date: 2026-10-29
Plan: docs/plan/feat-test-sessions.md (interfaces: docs/plan/feat-test-sessions-contract.md)

## What changed
- New app `test_sessions`:
  - `TestSession`: one open per user (partial unique constraint), name, note, `closed_automatically`.
  - `TestSessionCase`: one row per (session, run case), holding name snapshots, the first and last action, counts, the run's help mode, and the `frozen_*` outcome.
- `services/record_case_activity.py`:
  - `record_case_activity` upserts rows into the actor's open session (at most 5 queries; 1 or 2 without a session);
  - `freeze_session_cases_of_run` (Reset);
  - `freeze_finished_cases_of_sessions` (Stop and auto-close).
  - All three run in a savepoint and swallow `DatabaseError` only.
- Hooks in `projects`, one call each:
  - `services.start_test_run` gains `user=`, passed by `views.test_run_start` and `_handle_setup_run_submit`;
  - `services.reset_test_run` freezes first;
  - `run_resume_services._resume_pivots`;
  - `run_rerun_services.rerun_test_run_cases`;
  - `case_review_redesign_services.rerun_with_redesigned_plan`;
  - `case_help_services._resolve`;
  - `person_step_services._after_action` gains an `actor` parameter;
  - `case_result_change_services.apply_result_change`.
- Services:
  - start, stop, edit and delete a session;
  - `session_queries` (list 2 queries, summary 2, case page 3);
  - `case_outcome`: frozen-or-live expressions, with live values only while the owner is a project member;
  - a streamed CSV export (19 columns, keyset pages, a 50,000-row cap, formula-safe cells, 5 exports per minute);
  - `close_stale_sessions`.
- Pages:
  - `/test-sessions/`: start a session, the open one, a paginated list;
  - the detail page: session time, test time, tests, projects and person-step tiles; by-result and by-project breakdowns; the cases table; Stop, Edit and Delete dialogs; Export CSV.
- Nav: a "Sessions" link with a "Live" chip. The sidebar and topbar includes forward `open_test_session`.
- Admin: read only (no add, change or delete).

## Why
Testers need to report their testing hours, the projects they worked in and the number of tests they did, and to export those tests with each one's duration and result.

## Behaviour and contract changes
- New URLs: `test_sessions:{list,start,detail,stop,edit,delete,export}` under `test-sessions/`. Every view needs a login and only the owner can open a session (404 otherwise); the actions are POST.
- `projects.services.start_test_run(test_run, *, help_mode, user)`: `user` is required.
- Migration `test_sessions.0001_initial` (new tables only; no data loss).
- New setting `TEST_SESSION_MAX_HOURS` (default 12, 0 disables auto-close), a context processor, a Celery route and beat entry `test-sessions-close-stale` (`notify` queue, every 15 minutes).
- Deploy: `migrate`, then restart Daphne/web, the `notify` worker and beat.

## Notes for future changes
- What a test is: the user chose to count every action (start, resume, re-run, help, person step, result change). A case counts once per session.
- Test time is the case's total `active_duration`, so a case resumed in two sessions counts its whole time in both.
- An action that discards a verdict must call `record_case_activity` (or the run freeze) **before** wiping the pivot. Otherwise other sessions lose the outcome they saw.
- Accepted edge cases:
  - Auto-close runs every 15 minutes, so for up to 15 minutes after a session passes its maximum, a finishing case can be frozen with a later "As of" time, and a manual Stop can end it slightly past `started_at + max`.
  - A Stop freeze racing a Resume freeze may deadlock and leave those rows live (logged).
  - A Test Runner answer counts for the member its email names.
- Superusers have no team view yet; a team or manager page belongs with the user-management work.
- Verified with pre-commit (strict mypy for all three configs), `manage.py check`, `makemigrations --check` and `sqlmigrate`. A rolled-back smoke script on a throwaway database passed 42 checks: hooks, freezes, aggregates, query counts, CSV, auto-close, owner 404s, CSRF and the former-member gate. A browser check on port 8013 covered the list, detail, Live chip, Stop dialog and CSV download. Two rounds of reviewer and verifier passes. Not exercised with a live Test Runner run.
