# Human in the loop run mode

Date: 2026-10-28
Plan: docs/plan/feat-hitl-run-mode.md

## What changed
- `projects/models.py`: `RunHelpMode.HUMAN_IN_THE_LOOP` (`"hitl"`), case status `awaiting_person` ("Your turn"), model `CasePersonStep` (one row per hand-off; redo after "not confirmed" bumps `round`) and its enums.
- `agents/types.py`, `agents/checkpoint.py`: optional checkpoint `person` key (`CheckpointPersonStep`: hand-off, state waiting/reported/overruled/handed_back, report, `continue_pending`), phase `awaiting_person`, attempts' `performed_by`/`person_step_id`, `AgentStopReason.AWAITING_PERSON`; helpers `with_person_step`, `without_person_step`. Version stays 1.
- `agents/services/person_steps.py`, `person_step_prompts.py`, `orchestrator.py`: `run_orchestrator(person_mode=)`; per step, prepare a person-facing instruction (command only when the case states it as a whole), hand it off, then confirm the report with a sub-agent limited to `take_screenshot`/`execute_command`/`wait_for_command` (read-only by prompt; step text and report fenced as data). PASS records a person attempt; FAIL re-hands with the objection; "different" records a fail and stops the case; overrule records a pass. A due restore is handed to the person too. `person_mode()` is re-checked before every hand-off.
- `agents/os_shell.py` (new leaf): `get_os_name`, `shell_label`.
- `projects/services.py`: `_pause_run_for_person` (case `awaiting_person`, run PAUSED, re-checks the mode under the lock), no AI teardown in HITL, settle hook on checkpoint save, abort/reset/summary/finish handle the new status.
- `projects/person_step_rows.py` (new): row open/settle/hand-back helpers and the push decision.
- `projects/person_step_services.py`, `person_step_views.py`, `urls.py`: report, overrule, mark failed, mark blocked; `hand_back_person_step` on a mode switch away (`run_help_mode_services.py`).
- `projects/run_pause_panel.py` (new): run-page help/person panels and reopen gates computed once; plain paused-run Continue banner.
- `projects/person_step_notifications.py` (new): ntfy "Your turn" push (Need help preference).
- `projects/case_help_services.py`: help Mark failed/blocked cancel open person steps; Continue drops the `person` key unless current screen and cancels the orphaned row. `agents/plan_revision.py`: a redesign drops the key. `run_resume_services.py`: Resume drops the key.
- `projects/case_step_progress.py`, `testrail_mapping.py`: step status "Your turn", "performed by a person" in the Steps card and TestRail text.
- UI: "Your turn" card on the case page (command with Copy and shell), run banner, badges, Start/Resume/Re-run/mid-run mode option, dashboard attention group "Your turn".

## Why
Some tests must be performed by a person (or the user wants a person to drive them) while the agent still plans, checks and records each step and writes the verdict and TestRail result.

## Behaviour and contract changes
- New URLs: `projects:test_run_case_person_{report,overrule,fail,block}` (POST, `…/cases/<pivot>/person-step/<action>/`).
- Migration `projects.0038_hitl_run_mode` (additive: choices-only `AlterField`s + `CasePersonStep`). Rollback: move `hitl` runs to `attended` and settle `awaiting_person` cases first.
- New setting `PERSON_STEP_CONFIRM_TIMEOUT_SECONDS` (default 120).
- Run status event `help_state` gains `person_step`; step payloads gain `performed_by`/`performed_by_label`; run summary gains `awaiting_person`.
- No controller protocol change, no new Celery task. Deploy: `migrate`, restart Daphne/web and every Celery worker.

## Notes for future changes
- The confirm agent's read-only rule is prompt-enforced only (accepted risk, as for night auto-fix).
- Blocked invariant kept: `grep "status = TestRunTestCaseStatus.BLOCKED"` has one hit; `mark_blocked_person_step` assigns from a variable.
- A person-performed attempt's duration is the person's time; the case's active duration excludes it.
- The shared run badge in run lists still says "Needs help" for a run waiting on a person (lists don't carry `help_state`); the run page says "Waiting for a person".
- Cosmetic: a resolved earlier Need help in the same run can leave its case name in the banner after a later person-step Mark failed whose re-queue a gate refused.
- Verified with pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, template compilation, rolled-back DB smoke scripts and stubbed-LLM orchestrator scripts, and five reviewer/verifier rounds. Not yet run against a live runner, a vision worker or the real model: plan Verification steps 3–9 remain manual.
