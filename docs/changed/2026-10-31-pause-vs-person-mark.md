# Pause waits for a person's mark evidence

Date: 2026-10-31
Plan: docs/plan/feat-pause-and-discuss.md (changelog, integration entry)

## What changed
- `projects/case_pause_services.py`: `request_case_pause` and `_can_pause` refuse Pause while the open person step is reported with a mark close reason (`MARK_CLOSE_REASONS`); the case page passes `person_step_close_reason`.
- `projects/person_mark_verdict.py`: `apply_person_mark` clears an empty pause request like the other verdict writers.

## Why
Merging pause and discuss with the HITL failure evidence fix let a Pause trip the mark's evidence pass: the screenshot and logs were lost and the pauser was left at "Pausing…" with no discussion.

## Behaviour and contract changes
- New refusal message `PAUSE_MARK_RECORDING` (web and Test Runner pause).

## Notes for future changes
- The run page still offers Pause on every In progress case; the service refusal is the guard.
