# Needs re-test status

Date: 2026-10-30
Plan: docs/plan/feat-needs-retest-status.md

## What changed
- `projects/models.py`: `TestRunTestCaseStatus.NEEDS_RETEST` ("Needs re-test", a finished status), `TestRunTestCase.retest_from_status / retest_marked_at / retest_marked_by / retest_note`, `RETEST_VERDICT_STATUSES`, `RETEST_FIELDS` / `RETEST_CLEARED`, `HelpResolution.MARKED_RETEST`, `PersonStepClose.MARKED_RETEST`; `test_sessions` `CaseActivity.RETEST` (verdict-discarding).
- `projects/case_retest_marking.py` (new, leaf): `apply_retest_mark` / `clear_retest_mark`, shared by every marking path.
- `projects/run_case_retest_services.py` (new): `mark_cases_for_retest` (phase 1: every finished, not-started or running selected case in one transaction; phase 2: the run's waiting case through `case_help_services.mark_retest_after_help` or `person_step_services.mark_retest_person_step`), `undo_retest`, read helpers and `get_case_retest_panel`.
- `projects/services.py`: per-case cancellation check (a running case marked for re-test stops, the chain continues); pick-up skips a marked case; `_finalize_pivot` and the cancel path write only while the locked case is In progress; `needs_retest` / `retest_count` counts, run ordering, narrative "N need re-test", Reset clears the mark. The Failed-verdict teardown uses a run-only check.
- `projects/run_rerun_services.py`: Re-run clears the mark.
- TestRail: `testrail_mapping` Retest = 4; `pivot_result_text` leads with "Marked for re-test by …"; Needs re-test is checkable and early-sendable (`CASE_SEND_STATUSES`).
- Counts: step progress, run-finished notification, night digest, `machine_setup` run catalog.
- Pages: run page "Needs re-test (N)" bulk action, stat card and Reason text; case page "Mark for re-test" button and card with Undo; shared `_retest_dialog.html`; `.badge-retest` and run badges (Failed > Blocked > Needs re-test > Passed).

## Why
A person must be able to flag any result as untrustworthy, even in the middle of a run, so it is re-tested later and reported to TestRail as Retest.

## Behaviour and contract changes
- New POST URLs (`@project_membership_required`, read-only → 403): `projects:test_run_cases_retest` `/projects/<id>/test-runs/<run_id>/cases/retest/` (`pivot_ids`, `note`, `next`) and `projects:test_run_case_retest_undo` `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/retest/undo/`.
- Not on Draft runs. A running case stops at its next check with no verdict; Need help / Your turn cases are closed and the run continues (a live auto-fix is asked to stop first). The run finishes Done with marked cases; no automatic re-test; Resume ignores them; Re-run (N) or Reset clears the mark; Undo restores the earlier verdict only when there was one.
- Marking clears Checked/Sent; the next TestRail send adds a Retest result. Failed cases that are marked leave `/failures/` and the inbox.
- Migrations `projects.0045_needs_retest` (additive fields + choices) and `test_sessions.0002_needs_retest` (choices). Deploy: `migrate`, then restart web and every execution worker together (an old worker would run a marked case or overwrite the mark).

## Notes for future changes
- Lock order is project → run → open person steps → case; worker writes lock only the case row and release it before touching the run.
- `projects.services` cannot import `case_retest_marking` (cycle through `run_reopen_services`); it uses `projects.models.RETEST_CLEARED`.
- Not exercised against a database, a live runner or TestRail: checked with strict mypy, pre-commit, `manage.py check`, `makemigrations --check`, template loading and two reviewer/verifier rounds.
