# Live step progress on the case page; per-step breakdown in TestRail results

Date: 2026-10-23
Plan: docs/plan/feat-case-step-progress.md

## What changed
- `agents/types.py`, `agents/checkpoint.py`: the checkpoint gains optional `decisions` (`StepDecision`), `phase` (`OrchestratorPhase`), `phase_started_at` and `attempts[].duration_seconds`. `CHECKPOINT_VERSION` stays 1; these keys parse leniently. `truncate_checkpoint` also drops later decisions.
- `agents/services/orchestrator.py`: `_Progress.set_phase` checkpoints on step / evaluating / recovery / resume / verdict; each sub-agent run is timed; every failure decision (and the synthetic recovery stops, via `_record_stop`) is recorded.
- `agents/services/orchestrator_prompts.py`: `format_attempt_lines`, `build_verdict_prompt` and `_deterministic_summary` take the plan; step lines and the "not run" line carry each step's description; the verdict prompt must name any step it numbers.
- `projects/case_step_progress.py` (new, pure): `build_case_step_progress` (single source of step status), `case_step_payload` / `pivot_step_payload` (`StepProgressPayload`).
- `projects/case_checkpoint_services.build_checkpoint_callback(..., on_saved=)`: called only when the generation-filtered UPDATE landed.
- `projects/services.py`: `_broadcast_case_steps`; `CaseStatusEvent.steps`.
- `projects/consumers.py` (`TestRunCaseConsumer`): new `steps` message; `current_state` and `status` carry `steps`.
- `projects/views.test_run_case_detail` + `templates/projects/test_run_case_detail.html`: "Steps" card (live), `json_script` `initial-steps`. CSS `.step-list` component and `.card-header-count` in `punk-hazard.css`.
- `projects/testrail_mapping.py`: `format_step_breakdown`; `build_result_comment` / `build_checked_result_comment` take `step_breakdown`. Both push services pass it (`pivot_step_breakdown` in `testrail_results_services.py`); the checked push loads `execution_checkpoint`.

## Why
The orchestrator's sub-tasks could only be followed in the raw log, and TestRail comments said things like "Steps 4–7 were not run" using the agent's own sub-task numbers, which never matched anything the reader could see.

## Behaviour and contract changes
- Case page: new Steps card (one row per agent step: status, current activity, result, error, decision reason, duration, attempts disclosure).
- WebSocket (case): group event `test_run_case.steps` → client `{"type": "steps", "steps"}`; `current_state.steps` and `status.steps`.
- TestRail comments (Send checked and 018 Push) include "Steps planned by the agent (N):" with every step, capped at 6000 chars. The 018 comment hash changes, so re-pushing a run pushed before this deploy adds a second result per case (accepted).
- No migration, setting, task or controller protocol change. Deploy: restart execution and upload workers and Daphne/web.

## Notes for future changes
- Step numbers are the agent's sub-tasks, not TestRail case steps. Mapping onto TestRail `custom_step_results` (plan Part C) was deliberately not built.
- Abort does not bump `execution_generation`, so a worker can still land a checkpoint after Abort; the client ignores `steps` events unless the case is in progress.
- Cases run before this change show steps without durations, decisions or activity; cases with no checkpoint show an empty state.
- Verified with pre-commit (mypy strict), `manage.py check`, `makemigrations --check` and shell smoke scripts of the derivation and comment text. Not yet run against a live runner or a live TestRail instance (plan Verification steps 3–11).
