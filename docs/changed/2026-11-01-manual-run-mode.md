# Manual run mode

Date: 2026-11-01
Plan: docs/plan/feat-manual-run-mode.md

## What changed
- `projects/models.py` + migration 0053: `RunHelpMode.MANUAL` (`"manual"`), `FailedBy.MANUAL`, `BlockedReason.MANUAL_VERDICT` ("Marked blocked by the tester"), claim fields on `TestRunTestCase` (`manual_claim_*`), `ManualCaseSubmission` (hand-in, upload and AI rewrite state), `TestRunScreenshot.manual_key`, `PersonStepQuestion.manual_case` / `manual_step_num` (one anchor). `test_sessions` 0003: `CaseActivity.MANUAL_RESULT`.
- Server services: `projects/manual_case_claim_services.py` (claim/release, one claim per run under the run lock), `manual_case_submit_services.py` (hand-in: verbatim-step person attempts in the checkpoint with `manual: true`, verdict from a map, run finishes when no case is left), `manual_screenshot_services.py` (Pillow-checked, idempotent per `(case, manual_key)`), `runner_manual_state_services.py` (window snapshot), `manual_case_steps.py` (verbatim steps; free-text case = one step), `manual_advice_mapping.py` (Ask AI on a manual step via `build_advice_context`), plus leaf helpers `manual_case_lock`, `manual_run_finish`, `manual_submission_supersede`, `manual_screenshot_push`, `manual_question_push`, `manual_current_submission`.
- `projects/controller_consumer.py` + `runner_manual_services.py`: `manual_case_action`, `manual_case_submit`, `manual_screenshot_upload`, manual `person_step_question`; owner connection only, size caps before parsing, throttles; `manual_run_state` pushed with the person-step snapshot.
- Test Runner (`controller_client/`, CLIENT_VERSION 0.13.0, `manual_run_v1`): "Manual run" tab (`person_window_manual.py`), `manual_capture.py` (confirmed hide → settle → capture → show; no capture when the hide is not confirmed), `manual_spool.py` (local spool `controller_client/manual_spool/`, `CONTROLLER_MANUAL_SPOOL_MAX_MB`), `manual_uploader.py` (uploads after hand-in, resumes after disconnect/restart), `manual_coordinator.py`; self-update refused while the spool has files.
- TestRail: `agents/services/manual_result_refiner.py` rewrites only the tester's notes and case note (statuses, step texts and literals untouched; tester's text on any AI fault); `projects/manual_refinement_services.py` (task `projects.tasks.refine_manual_case_result` on `upload` when uploads finish, re-checked by hash and refreshed at push, 4 parallel calls); uniform manual comment layout in `testrail_mapping.py`; manual screenshots attached (`MANUAL_TESTRAIL_MAX_SHOTS`); cases still uploading cannot be checked or sent; beat `sweep_stalled_manual_uploads` marks stalled hand-ins `incomplete`.
- Web: run page manual card and upload badges, case page `_manual_case_card.html` (steps, notes, screenshots, verdict, prepared TestRail text, live Ask AI), Release of a stuck claim (`projects:test_run_case_manual_release`), Manual copy in the start dialog and Settings.
- Refusals: Resume, help, person steps, Pause, redesigns and mid-run mode switches across Manual; Needs re-test of the claimed case clears the claim and may finish the run.

## Why
Testers wanted a mode where a person performs every case and decides the result, with step screenshots from the Test Runner, while TestRail still gets consistent, cleaned-up text.

## Behaviour and contract changes
- New run mode Manual (superuser-chosen like the others); readiness checks the Test Runner (`manual_runner`) instead of the visual engine and machine preparation.
- Controller protocol 0.13.0: capability `manual_run_v1`; messages `manual_run_state`, `manual_case_action(_result)`, `manual_case_submit(_result)`, `manual_screenshot_upload`, `manual_screenshot_ack` (`drop` = whole hand-in, `drop_shot` = one shot); `MANUAL_SHOT_MAX_BYTES` 7 MiB, `MANUAL_SHOT_MAX_PIXELS` one 8K screen. Older runners cannot start a manual run.
- Settings: `MANUAL_SHOT_MAX_BYTES`, `MANUAL_SHOT_RATE_PER_MINUTE`, `MANUAL_TESTRAIL_MAX_SHOTS`, `MANUAL_REFINE_TIMEOUT_SECONDS`, `MANUAL_UPLOAD_STALE_HOURS`; runner `CONTROLLER_MANUAL_SPOOL_MAX_MB`.
- Migrations: `projects` 0053 (additive; rollback fails once a manual question exists), `test_sessions` 0003.
- URL: `projects/<id>/test-runs/<run>/cases/<pivot>/manual/release/` (POST).

## Notes for future changes
- `parse_manual_case_submit_payload` runs on both sides and a refusal drops the spooled hand-in: tightening it needs a CLIENT_VERSION bump.
- Refusal levels: `ManualRunError` = retry, `ManualShotRejectedError` = drop that shot (hand-in goes `incomplete` at once), `ManualSubmissionDroppedError` = drop the hand-in.
- Blocked is still written only from a variable (one literal Blocked write in `case_help_services.py`).
- Testrail/evidence code imports `projects.manual_run_policy`, never `manual_case_steps` (cycle via `case_step_refs`).
- Screenshots go to TestRail in upload order (resumable attachment counter); file names carry the step number.
- Not yet tried live: real window hide/show, Windows/Linux Tk, a real database, TestRail or the AI provider.
