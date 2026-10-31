# Pause a running case and discuss it with the agent

Date: 2026-10-31
Plan: docs/plan/feat-pause-and-discuss.md

## What changed
- `projects/case_pause_services.py`: `request_case_pause(pivot, actor, message)` stores a pause request on a running case (one per run generation) or converts a waiting person step at once; `get_case_pause_panel` (no queries), `get_pause_requester_label`, `dismiss_dropped_pause`.
- `projects/case_pause_policy.py`: `allowed_continue_modes`, `pause_continue_mode`, `CasePauseOutcome`, pause texts. `projects/case_pause_rows.py`: `open_pause_help_request` (shared by the worker and the person-step conversion), `mark_case_paused_for_help`, `pause_pending_for`.
- `projects/services.py` (worker): the case cancellation poll also sees a pause; the stop opens a Need help request (`_pause_case_for_person`, `stopped_by_pause`); a pending pause beaten by need_help, a fault or a HITL hand-off still records the pauser (no Night auto-fix, no ntfy); the verdict drops it (a message is kept as "Too late to pause"); abort, re-test, Reset and Re-run clear it; pick-up clears another generation's request and a same-generation one skips the pre-case cleanup (redelivery).
- `projects/case_help_services.py`: `continue_after_help(continue_mode=)` validated against `allowed_continue_modes`; a redesign requires current screen and is refused on a restore request; `HelpPanel.continue_mode_choices` / `agent_continue_mode` / `default_continue_mode`; Mark failed/blocked say "while it was paused by X". `case_help_proposal_services.record_agent_proposal(continue_mode=)`. `run_help_mode_services`: a Night switch never auto-fixes a request with a pending person.
- `projects/runner_case_pause_services.pause_from_runner`: server seam for the Test Runner window (owner connection only, shared 20/min runner throttle; the member-or-anonymous actor). The runner throttle helpers in `runner_person_step_services` are now public.
- `machine_setup`: task `open_paused_help_chat` (`execution` queue, sent by name) opens the `case_help` thread and sends the pause message as the pauser (`pause_message_state` pending → sent / refused / dropped); `propose_resolution(continue_mode=)`; the help brief and `get_help_case` say "a person paused it" and list the allowed modes; the thread help panel shows the mode radios, the agent's suggestion and an unsent message for re-send. A manual send clears a refused/dropped message.
- `agents`: `CheckpointHelp.paused_by`; a pause is treated like a fault when anchoring the help line; pause wording in the help state line and verdict note; one more cancellation check before the verdict phase.
- Pages: Pause and discuss button + dialog (`_case_pause_dialog.html`) on the case page, the run page (running case) and the run help banner (waiting person step); "Pausing… requested by X", "Paused by X at step N", message state and re-send, continue-mode radios, the "Too late to pause" notice (Discuss the result pre-filled, or Dismiss). The case page follows `discussion_url` once the discussion opens (`?discussion=1` fallback).

## Why
A person watching a test must be able to stop it mid-case, talk it through with the agent, change the plan or the machine, and choose how the case goes on.

## Behaviour and contract changes
- Migration `projects.0050_case_pause` (nullable/blank-default columns on `TestRunTestCase` and `CaseHelpRequest`; `PersonStepClose.PAUSED`). Deploy: migrate, then restart web, Daphne and every Celery worker together.
- URLs `projects:test_run_case_pause` (POST `message`, `next`) and `projects:test_run_case_pause_dismiss`.
- `CaseHelpResolutionForm.continue_mode` (optional radio). A redesign on a restore request is now refused ("Restore first").
- Socket: `test_run_case.status` gains `pause_pending`, `pause_requested_by`, `discussion_url`; `test_run.pivot_status` gains `pause_pending`. `projects.services.broadcast_case_status`.
- New Celery task `machine_setup.tasks.open_paused_help_chat` (args: help request id only).

## Notes for future changes
- Wave 2 (`docs/plan/feat-runner-person-window-v2.md`) owns the runner protocol (`case_pause_action`, `CASE_PAUSE_V1`) and calls `pause_from_runner`, `request_case_pause(actor=)`, `get_case_pause_panel`, `allowed_continue_modes` unchanged.
- An anonymous runner pause pauses but never auto-sends its message (no member to send it as).
- Only `waiting` person steps convert at once; `not_confirmed` is refused, `reported` pauses like a running case once picked up.
- A Needs re-test mark on a finished case does not clear a dropped record; Re-run, Reset, Dismiss or the next pick-up do.
- The dialog hard-codes the 8000-character limit (`PAUSE_MESSAGE_MAX_LENGTH`) so the run page can include it without a form.
