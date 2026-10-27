# Send only the relevant screenshots to TestRail

Date: 2026-10-28
Plan: docs/plan/feat-testrail-relevant-screenshots.md

## What changed
- `agents/services/orchestrator.py`: each sub-agent run (step, recovery, resume restore) gets a random `attempt_id`; its screenshots are reported through `AttemptScreenshotCallback` tagged with that id and the step, and the recorded `SubTaskAttempt` carries the same id.
- `agents/checkpoint.py`: optional `attempt_id` key on attempts (version unchanged); `final_step_indices` moved here from `orchestrator_prompts`.
- `TestRunScreenshot`: new `attempt_id`, `step_num`, `testrail_evidence` (migration `0033`, which flags every screenshot of cases already checked so their sends keep the old list).
- `projects/testrail_evidence.py` (pure rules) + `projects/testrail_evidence_services.py` (`freeze_testrail_evidence`): choose the screenshots to send.
- `projects/run_case_check_services.check_run_cases`: atomic, locks the cases and freezes their evidence flags before checking them.
- `projects/testrail_checked_push_services.py`: attaches and counts only flagged screenshots; `testrail_mapping`: filenames gain the step (`C123_s03_01_click.png`), the comment says which steps the screenshots come from.

## Why
"Send checked to TestRail" attached every screenshot of a case. Passed cases now attach none and failed or blocked cases only the screenshots of the attempt that caused the result.

## Behaviour and contract changes
- Passed: no screenshots. Failed: the last `TESTRAIL_EVIDENCE_SHOTS_PER_STEP` (3) screenshots of each failed step's final attempt, at most `TESTRAIL_EVIDENCE_MAX_SHOTS` (10). Blocked: the attempt the case stopped on (a fault-interrupted attempt at the cursor first). Otherwise (legacy, person-made Failed, no checkpoint): the case's latest 3.
- New settings `TESTRAIL_EVIDENCE_SHOTS_PER_STEP`, `TESTRAIL_EVIDENCE_MAX_SHOTS` (in `example.env`).
- Migration `projects.0033_screenshot_attempt_evidence` (additive, no data loss). Restart web, `execution` and `upload` workers.
- `run_orchestrator(on_screenshot=...)` now takes an `AttemptScreenshotCallback`; `ScreenshotCallback` for tools is unchanged.

## Notes for future changes
- The selection is frozen at check; only `check_run_cases` writes `testrail_evidence`. The send's resume counter indexes the flagged list, so never change the flags of a checked case.
- The link screenshot -> attempt is stored but not shown in Punk Hazard (user's choice); a gallery step label or log marker can build on `attempt_id`/`step_num`.
- Teardown screenshots are untagged and only reach TestRail through the fallback.
