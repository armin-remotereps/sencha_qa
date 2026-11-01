# Resume for Manual runs

Date: 2026-11-01
Plan: docs/plan/feat-manual-run-resume.md

## What changed
- `projects/manual_run_resume_services.py` (new) `resume_manual_cases`: locks project → run, requires a Manual run that is Done or Cancelled, no mode switch across the Manual boundary, no reopen blockers; Cancelled cases get a `TestRunCaseResume` snapshot, `CaseActivity.RESUME`, `reset_case_for_restart`, checkpoint `{}` and claim cleared (one `bulk_update`), hand-ins superseded defensively, then `reopen_run` (manual branch: broadcast and runner nudge, no chain).
- `projects/run_resume_services.py`: `resume_test_run`, `resume_test_run_case`, `get_resume_panel` (`ResumePanel.is_manual`), `can_resume_case` and `is_manual_resume` route Manual runs to the new module; the agent path is unchanged.
- `projects/case_checkpoint_services.py`: `list_resume_steps` returns nothing for a manual checkpoint (no step select).
- `projects/manual_run_policy.py`: refusal strings `MANUAL_NOTHING_RESUMABLE`, `MANUAL_RESUME_NOT_MANUAL`; `MANUAL_RESUME_REFUSED` reworded.
- `projects/views.py` (`resume_is_manual`), `test_run_detail.html`, `test_run_case_detail.html`: Resume run / Resume case dialogs show "Run mode: Manual" as text and manual wording.

## Why
An aborted Manual run had no way back: Resume was refused for Manual runs, so cases cancelled by an abort could not be performed again on the same run.

## Behaviour and contract changes
- Resume run on a Manual run reopens only its Cancelled cases (not the tester's Blocked verdicts, which Re-run redoes); the run goes back to Running and the cases reappear in the Test Runner's manual list.
- Resume case works for one Cancelled manual case; the run finishes once handed-in cases settle (Cancelled ones are ignored).
- No leaderboard credit at Resume (the tester is credited at hand-in); logs, earlier attempts and screenshots are kept.
- No migration, URL, setting or controller protocol change.

## Notes for future changes
- A Cancelled manual case never has a live hand-in (hand-ins settle Passed/Failed/Blocked under the run lock); the supersede is defensive.
- Not yet smoke-tested against a database or in a browser.
