# Re-run selected cases in place on the same run

Date: 2026-10-23
Plan: docs/plan/feat-rerun-selected-cases.md

## What changed
- `projects/run_rerun_services.py` (new):
  - `rerun_test_run_cases` resets the selected pivots in one UPDATE (status Created, verdict, reason, timestamps, checkpoint, Checked/Sent), appends `=== Re-run from the start ===` to the log, records a `kind=rerun` history row, deletes the screenshot rows (files after commit) and queues the pivots in `RUN_CASE_ORDERING`.
  - Also `get_rerun_panel`, `rerunnable_ids_on_page`, `list_rerunnable_pivot_ids` and `format_rerun_message`.
- `projects/run_reopen_services.py` (new): `lock_project_and_run`, `list_reopen_blockers` and `reopen_run` (run back to Started, generation + 1, queue and broadcast on commit), extracted from `run_resume_services`. Resume now uses them.
- `get_resume_panel(test_run, *, blockers)` takes the blockers that `get_rerun_panel` already computed, so the gates run once per page.
- `TestRunCaseResume.kind` (`CaseRestartKind`: resume / rerun) and `is_rerun`; migration `0025_case_restart_kind`. `RunRerunError`.
- View `test_run_cases_rerun` with `RunCaseSelectionForm`.
- Run page:
  - a "Re-run (N)" button in the bulk bar and a confirm dialog;
  - every row is selectable on a finished run (`runCaseChecks` takes a third id list, `toRerun`);
  - "Re-run failed" is relabelled "Re-run failed in a new run".
- Case page: "Resume history" becomes "Earlier attempts" with per-kind wording, and the page reloads once when the case goes back to Created.

## Why
Users wanted to run some cases of a run again without creating a new run, in the run's order.

## Behaviour and contract changes
- New POST URL `projects:test_run_cases_rerun` → `/projects/<id>/test-runs/<run_id>/cases/rerun/` (fields `pivot_ids`, `next`), with `@project_membership_required`.
- Re-run works only on a Done or Cancelled run, with Resume's gates: no other run Started, no TestRail send or push live, the runner connected and the visual engine ready, and no live setup turn. Every case of any status can be re-run, including Sent ones; their Checked/Sent marks are cleared.
- The replaced attempt's screenshots and step progress are deleted. Its log text stays, and the previous status, reason, verdict and finish time go into the history.
- Migration 0025 is additive. Deploy: run `migrate` and restart Daphne/web. No task, WebSocket or controller protocol change.

## Notes for future changes
- Resume and Re-run share `run_reopen_services`. Change gates or the restart there, not in either use case.
- Known gap (follow-up): `add_cases_to_test_run` accepts cases on a Done run. An unselected Created case then keeps a re-run Started until Abort. Resume has the same gap.
- Accepted residual, as for Resume: a stale worker can land one old screenshot after the delete.
- An open case page now also reloads on Resume.
- Verified with pre-commit (mypy strict), `manage.py check`, `makemigrations --check` and a throwaway-DB smoke script: order, reset, history, file deletion, gates, page renders. Not yet run against a live runner.
