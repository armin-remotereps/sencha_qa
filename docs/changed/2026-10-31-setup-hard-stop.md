# Machine setup Force stop

Date: 2026-10-31
Plan: docs/plan/feat-setup-hard-stop.md

## What changed
- `machine_setup/services/force_stop_turn.py` (`force_stop_turn`), view `setup_force_stop`, URL `machine_setup:force_stop`, `SetupForceStopForm`: a writable member force-closes a running turn whose Stop went unanswered for `MACHINE_SETUP_FORCE_STOP_GRACE_SECONDS` (default 30, server-enforced via `machine_setup/force_stop_policy.force_stop_in`). Closes it as `stopped/force_stopped` under the project and turn row locks, then revokes the Celery task with `terminate=True, signal="SIGTERM"` on commit.
- `machine_setup/services/live_turn.py`: `_close_lost_turn` generalised into `close_live_turn` (worker_lost and force stop); Stop records `stop_requested_at` (first press kept, `Coalesce`), as does `auto_fix_stop`.
- Worker guard (`run_turn.py`, `run_setup_task.py`, `activity_recorder.on_screenshot`): after another process closes the turn, a surviving worker writes nothing — `_finish` locks the turn and no-ops unless RUNNING; transcript persist, iteration count, transcript callbacks, screenshots and sub-task results are gated on RUNNING; `_should_stop` also fires when the turn is closed.
- Chat (`chat.html`, `includes/_composer.html`): Force stop button with countdown and confirm dialog beside "Stopping…", also on the "finishing previous turn" line after Reset (`finishing_turn` in the page and `current_state`); `applyTurn` ignores payloads of older turns.

## Why
A turn whose worker died (deploy restart) or hung in a controller call ignored Stop and blocked the project for ~67 min until it went stale (projects 24 and 29 on 2026-10-31).

## Behaviour and contract changes
- New URL `projects/<id>/machine-setup/force-stop/` (POST `turn_id`, optional `conversation_id`).
- Migration `machine_setup.0008_turn_force_stop`: nullable `SetupTurn.stop_requested_at`, end reason `force_stopped`.
- Setting `MACHINE_SETUP_FORCE_STOP_GRACE_SECONDS` (example.env).
- Turn payload gains `force_stop_in`; `current_state` gains `finishing_turn`.

## Notes for future changes
- SIGTERM on a hung prefork child runs the turn's own `_finish` (seen on production); the locked status check is what makes it a no-op after a force close.
- Accepted risk: terminate may hit the next task the pool child started (Celery warns about this). A dedicated setup-turn queue/worker would remove it.
- An action already sent to the Test Runner still finishes on the machine; the notice says so.
- Deploys that recreate `execution-celery` still orphan live turns (and running cases) until they go stale; Force stop is the manual escape hatch until that is fixed.
