# Admin edit of a run case's result

Date: 2026-11-12
Plan: docs/plan/feat-admin-case-edit.md

## What changed
- `projects/case_edit_policy.py`: who may edit (`can_admin_edit` = active superuser), which cases (`edit_blocker`: Passed/Failed/Blocked, not claimed, not Needs re-test), revert rule and field names.
- `projects/case_edit_checkpoint.py`: pure edit of the checkpoint's plan steps, final attempts and decisions (`apply_step_edits`, `step_rows`), plus the editor's optimistic-concurrency `edit_base_key`.
- `projects/case_edit_write.py`: the one write path: `lock_editable_case` (project → run → case, refuses during a live TestRail send, a Manual upload or a stale editor) returns `LockedCase`; `finish_edit` clears Checked/Sent, supersedes result-chat proposals, sets the marker (result edits only), writes the `CaseResultEdit` audit row and broadcasts on commit (`robust=True`).
- Use cases: `case_edit_result_services.edit_case_result` (status, summary, every step in one save), `case_edit_screenshot_services` (TestRail attach override, delete), `case_edit_upload_services` (PNG/JPEG upload, `load_manual_image` limits, ≤ 20 per case), `case_edit_revert_services` (newest edit only).
- Read side `case_edit_page.py`; views `case_edit_views.py`; forms `case_edit_forms.py` (CRLF normalised); templates `projects/test_run_case_edit.html`, `projects/includes/_case_edits.html`; case page: Edit result button (superusers), "Edited by" line, Edits history, "Uploaded" caption.
- `projects/case_result_fingerprint.py` (leaf): `result_fingerprint` moved here from `case_result_change_services`; `admin_edit_is_current`, `admin_edit_marker`.
- TestRail: `testrail_evidence.effective_override` + overrides in `select_reviewed_evidence` (include always attached, exclude never; no AI review when every candidate is overridden); "Edited in Punk Hazard by <name> on <date> UTC." comment line; `manual_refinement_services` never rewrites an edited case's notes.
- `services.reset_test_run` deletes the run's `CaseResultEdit` rows.

## Why
Superusers need to correct everything a run recorded for a case (verdict, summary, step wording and outcomes, screenshots) by hand, without the AI result chat, while keeping what was there before.

## Behaviour and contract changes
- New URLs under `cases/<pivot_id>/result/edit/`: `projects:test_run_case_result_edit` (GET/POST), `…_shot_evidence`, `…_shot_delete`, `…_shot_upload`, `…_revert`. All 403 for non-superusers.
- Migration `projects.0064_case_result_edit` (additive): `CaseResultEdit`; `TestRunTestCase.admin_edited_at`, `admin_edited_label`, `admin_edit_fingerprint`; `TestRunScreenshot.evidence_override`, `evidence_override_finished_at`.
- Edits overwrite the live fields (case page, WebSocket, TestRail, triage and Resume all read them); the audit row keeps the full earlier state. A sent case gets Sent cleared; the next Send adds a new TestRail result and the old one stays.
- Deploy: `migrate`, restart web/Daphne and the `upload` workers.

## Notes for future changes
- The admin marker counts only while `admin_edit_fingerprint == result_fingerprint(pivot)`; any new verdict drops it without touching reopen paths. Screenshot-only edits do not set it; revert restores the earlier marker.
- An evidence override counts only while `evidence_override_finished_at == pivot.finished_at`; read it only through `effective_override`. Setting a shot back to auto clears the stored evidence review.
- Admin step text reaches the agent on Resume (single source of truth, by design).
- Not run against a database, browser or TestRail yet (Postgres was not running locally); `vision_worker` mypy hook was skipped (no local venv).
