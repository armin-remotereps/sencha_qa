# Machine setup works during a Manual run

Date: 2026-11-01
Plan: docs/plan/fix-setup-during-manual-run.md

## What changed
- `projects/run_machine_use.py` (new leaf): `agent_run_started` (a Started run that is not Manual) and `manual_tester_on_machine` (a claimed, unpaused case of a Started Manual run).
- `machine_setup/services/send_gates.py`: `run_and_update_refusal` refuses only an agent-driven Started run; `own_manual_run_id` dropped; `claimed_manual_run_id` → `is_claimed_manual_pivot`.
- `machine_setup/services/chat_page.py`: the page blocker follows the same rule.
- `machine_setup/services/setup_tools.py`: `run_setup_task` (every role but MANUAL_CASE, approved actions included) refuses with `MANUAL_TESTER_ON_MACHINE` while a tester is mid-case; no approval attempt is spent.
- `machine_readiness/runner_ports.py`, `services/runner_gates.py`, `projects/machine_readiness_ports.py`: scan / quick check (and the chat's `scan_artifacts`) use the two checks through new `RunnerPorts` fields.
- `projects/manual_case_claim_services.py`, `manual_case_pause_services.py`, `manual_case_display_services.py`: Claim and Continue refuse while any setup turn of the project is live (was: only the case's discussion); `turn_state.manual_run_turn_live` deleted.
- `machine_setup/services/runner_discussion.py` + `projects/runner_window_state.py`: the Test Runner window's Continue is disabled while any setup turn is live (`RunnerManualDiscussion.setup_turn_live`).

## Why
A Manual run stays Started for its whole life although no agent drives the machine, so Machine setup refused every message. Runs waiting for a person were already Paused and allowed.

## Behaviour and contract changes
- Setup sends, approvals, prepare-machine and readiness Scan / Check machine work during a Manual run; agent-driven Started runs refuse as before.
- New refusal text while a tester is mid-case (`MANUAL_TESTER_ON_MACHINE`, `runner_gates.MANUAL_TESTER_MESSAGE`). No migration, setting or protocol change.

## Notes for future changes
- Safety relies on the project row lock: a send and a claim / Continue serialize, and while a turn is live the tester cannot take the machine back, so the call-time tester check only moves toward allowed.
- Accepted gap: a claim does not wait for a queued readiness scan / quick check (stat only; agent Start has the same gap).
- Keep `manual_tester_on_machine` aligned with `manual_run_policy.is_claimed_manual_case`.
