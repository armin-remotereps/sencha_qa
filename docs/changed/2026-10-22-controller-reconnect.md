# Test Runner reconnects after a dropped connection and the run continues

Date: 2026-10-22
Plan: docs/plan/fix-controller-reconnect.md

## What changed
- `controller_client/client.py`:
  - retries forever by default, with backoff 5s doubling to a 60s cap;
  - 30s handshake-ack timeout (`_perform_handshake`);
  - keepalive 30s/120s;
  - abandons the in-flight handler once the socket closes;
  - exits only on an `error`/`incompatible` ack or close codes 4001/4002.
- `controller_client/config.py`: new `reconnect_max_interval`. `max_reconnect_attempts` defaults to 0 (unlimited).
- `controller_client/protocol.py`: `CloseCode`, `TERMINAL_CLOSE_CODES` and `ErrorCode.CONTROLLER_DISCONNECTED`; `CLIENT_VERSION` 0.3.0.
- `projects/services.py`, connection ownership: `claim_agent_connection` and `release_agent_connection` replace `mark_agent_connected` / `mark_agent_disconnected`.
  - Ownership is `Project.agent_channel_name`; the newest connection wins.
  - Release only clears a channel that still owns the project.
- `projects/services.py`, actions: `_run_with_reconnect_retry` re-sends an action interrupted by a drop once, after the runner reconnects. `run_command` logs a re-run marker line.
- `projects/controller_reconnect_services.py` (new): `wait_for_controller` waits for the runner while a run is STARTED, up to `CONTROLLER_RECONNECT_GRACE_SECONDS`, and at most 2 × grace in total per case.
- `projects/controller_outage_clock.py` (new): `agent_monotonic()`. Agent and sub-agent deadlines, the `wait` tool and the OmniParser-ready wait use it, so reconnect waits don't consume agent time.
- `projects/controller_consumer.py`:
  - joins the group before claiming;
  - replaces the previous owner, closing it with 4001;
  - on disconnect, releases ownership and then fails pending replies with `CONTROLLER_DISCONNECTED`;
  - no longer aborts the run.
- `projects/tasks.py`: `execute_test_run_case` time limits gain the per-case outage allowance (2400/2460s at the default).
- `agents/services/tool_utils.py`: `safe_tool_call` re-raises `ControllerReconnectTimeoutError`.

## Why
The server kept a stale "connected" flag after a drop and refused the reconnect as `already_connected`. The runner treated that as fatal, so the only fix was stopping it, using Force disconnect, and restarting it. In addition, any drop aborted the active run (spec 010).

## Behaviour and contract changes
- A disconnect no longer cancels the run. The run waits up to `CONTROLLER_RECONNECT_GRACE_SECONDS` (default 300) and is aborted only after that. Force disconnect still aborts immediately.
- An action in flight when the connection drops is re-sent once. The user accepted that a non-idempotent action (click, type, command, launch) can run twice.
- A new handshake with the same key replaces the current runner. A 0.3.0 runner exits on 4001 ("replaced") and 4002 (Force disconnect).
- Settings: `CONTROLLER_AGENT_CONNECT_TIMEOUT` is removed and replaced by `CONTROLLER_RECONNECT_GRACE_SECONDS`. Update deployed `.env` files.
- Runner `.env`:
  - new `CONTROLLER_RECONNECT_MAX_INTERVAL=60`;
  - `CONTROLLER_MAX_RECONNECT_ATTEMPTS=0` means retry forever;
  - new ZIPs write both. Existing installs keep `10` until their `.env` is edited or re-downloaded.
- Migration `0017`: additive `Project.agent_channel_name`.

## Notes for future changes
- The outage clock is process-local. It assumes one test case per process and thread (Celery prefork); threaded execution would need it passed explicitly.
- After a Daphne restart, an in-flight action is re-sent only if the runner reconnects before that action's reply timeout. Otherwise it fails as a plain timeout.
- 0.2.0 runners are accepted, but on 4001 they reconnect instead of exiting. Two of them on one key keep replacing each other until their 10 attempts run out. A 0.2.0 runner beats a 0.3.0 one.
- During a takeover an action can reach both runners for a moment, and a `run_command`'s output lines can interleave. The first `command.result` wins.
- `test_controller_*` management commands now raise `ControllerNotConnectedError` (traceback) when no runner is connected.
