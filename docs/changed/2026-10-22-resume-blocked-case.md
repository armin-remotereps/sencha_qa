# Resume Blocked and Cancelled cases in place, from the step where they stopped

Date: 2026-10-22
Plan: docs/plan/feat-resume-blocked-case.md

## What changed
- `projects/models.py`, migration `0023_case_resume` (additive):
  - `TestRunTestCase.execution_checkpoint` (JSON);
  - `TestRun.execution_generation`;
  - new model `TestRunCaseResume` (resume history);
  - `BlockedReason.RESUME_STATE_LOST`.
- `agents/types.py`: `OrchestratorCheckpoint` and `CheckpointCallback`. `SubTaskAttempt.kind` gains `"resume"`.
- `agents/checkpoint.py` (new, pure): `checkpoint_to_json`, `checkpoint_from_json` (strict and versioned; `None` means plan from scratch), `truncate_checkpoint`.
- `agents/services/orchestrator.py`: `run_orchestrator(resume=, on_checkpoint=)`.
  - A resumed case skips planning, seeds its attempts, state lines and browser, and runs a restore sub-task before the step it resumes at.
  - A failed restore raises `ResumeStateLostError`.
  - The cursor is saved before each step and after each attempt or decision. A "stop" decision moves it past the last step.
- `agents/services/orchestrator_prompts.py`: labels for resume attempts and a "resumed" note in the verdict.
- `projects/case_checkpoint_services.py` (new): `build_checkpoint_callback(pivot, generation)`, `load_resume_checkpoint`, `list_resume_steps` / `ResumeStepOption` (includes a `verdict_only` option).
- `projects/run_resume_services.py` (new):
  - `get_resume_panel(test_run, project)`, `can_resume_case`, `get_run_case_for_resume`, `list_case_resumes`;
  - `resume_test_run` / `resume_test_run_case`, which lock the project and then the run, check the gates, snapshot history, reset Checked/Sent, bump the generation and queue the cases after commit.
- `projects/services.py`:
  - `start_test_run` is atomic and takes the project lock.
  - New shared `queue_test_run_cases` cancels the run if the broker refuses the chain.
  - `broadcast_run_and_pivots`.
  - `execute_test_run_test_case(pivot_id, generation=None)` stands down when the generation is stale: no verdict, status, cleanup, logs, screenshots or checkpoint.
  - Log lines are appended in the database (`_append_pivot_log`) instead of rewriting `logs`.
  - `abort_test_run` re-reads the status under a row lock.
  - `reset_test_run` clears checkpoints and resume history.
- `projects/tasks.py`: `execute_test_run_case(pivot_id, generation=None)`. A missing generation means 0.
- `projects/case_blocking.py`: `ResumeStateLostError` maps to `resume_state_lost`.
- Views, forms, URLs and templates:
  - `test_run_resume` and `test_run_case_resume` with `CaseResumeForm`;
  - a "Resume run" button and confirm dialog on the run page (disabled, with the reason, when blocked);
  - a "Resume case" dialog with a step select, and a "Resume history" list on the case page;
  - "Re-run blocked" relabelled "Re-run blocked in a new run".

## Why
Cases blocked by infrastructure (runner lost, visual engine, AI service, ...) could only be re-run from scratch in a new run. Once a person fixes the infrastructure, they now continue on the same run from the step that was interrupted, keeping the earlier steps' evidence.

## Behaviour and contract changes
- New POST URLs, both `@project_membership_required`:
  - `projects:test_run_resume` → `/projects/<id>/test-runs/<run_id>/resume/`
  - `projects:test_run_case_resume` → `.../cases/<pivot_id>/resume/` (field `from_step`)
- Resume is allowed only when:
  - the run is Done or Cancelled;
  - no other run is Started;
  - no TestRail send or push is live;
  - the Test Runner is connected and the visual engine is ready. This is enforced on the server.
- What Resume run does:
  - It resumes every Blocked and Cancelled case. A case with a checkpoint continues from it; a case without one runs from step 1.
  - It clears the case's Checked/Sent marks, so a new TestRail result can be sent.
  - It keeps the log, screenshots and checkpoint. `started_at` is set again when the case is picked up.
  - The run keeps its original start time.
- A resumed case gets a fresh time budget and fresh recovery counters. Its restore sub-task counts against that budget.
- Celery task signature: `execute_test_run_case(pivot_id, generation)`. Tasks already queued without a generation still run as generation 0.
- Start now queues its chain in `on_commit`.
- Deploy: run `migrate`, then restart the execution workers and Daphne/web. Rollback of 0023: first run `UPDATE projects_testruntestcase SET blocked_reason='internal_error' WHERE blocked_reason='resume_state_lost'`.
- No WebSocket event, setting or controller protocol change.

## Notes for future changes
- The checkpoint is written only through the generation-filtered callback. A new orchestrator field to persist needs `agents/checkpoint.py` updated, and a `CHECKPOINT_VERSION` bump if the shape changes.
- `projects.case_checkpoint_services` must not import anything under `agents.services`: that package's `__init__` imports `projects.services` (cycle). This is why the serializer lives in `agents/checkpoint.py`.
- Any new write a case worker makes must respect `execution_generation`. Use a filtered UPDATE, as `_append_pivot_log` does, or `_is_stale_execution`.
- Accepted residuals:
  - A stale worker keeps driving the machine until its next cancellation check (the same latency as Abort).
  - Its terminal status write is check-then-write, with a window of milliseconds.
  - If the broker refuses a Resume's queue, the reset cases become Cancelled. They stay resumable, and the history keeps the earlier Blocked outcome.
- Downloads from earlier steps are gone after a block, because the cleanup empties the folder. The restore sub-task has to fetch them again.
- Verified with mypy (strict), black/isort, `manage.py check`, `makemigrations --check` and a shell smoke test of the checkpoint round-trip, step options and form. Not yet run against a live runner (plan Verification steps 2–10). The local dev DB is behind on migrations.
