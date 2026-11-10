# User-owned Test Runners, assigned per run

Date: 2026-11-10
Plan: docs/plan/feat-user-test-runners.md (Phase 1; Phase 2 deferred)

## What changed
- New `runners` app: `TestRunner` (owner, name unique per owner, token hash + Fernet copy, connection state), `RunnerId`, `runners/locks.py`, `machine_state.py`, `choices.py`, `device_label.py`, services (create, rename, reissue token, archive, authenticate, connection, access, status, ZIP, list/detail, release, disconnect), `/runners/` pages and `ws/runners/...` status sockets.
- `projects`: `TestRun.runner` (+ snapshot, assigned at); `projects/runner_use.py` is the one "runner held" rule (`runner_hold(allow=)`, bulk `runner_holds`); `run_runner_assignment.py`, `run_runner_change.py`, `run_runner_release.py`, `run_runner_default.py`, `run_runner_gate.py`, `screen_continuations.py`; every `controller_*` call and the reconnect wait keyed by `RunnerId` (group `runner_{id}`); Start / Resume / Re-run / help Continue take a runner; run page Test Runner card (Change, Release); Environment tab renamed "Test Runners"; wizard picks or adds a runner; project ZIP download, update panel and project force disconnect removed.
- Runner window, self-update (`RunnerUpdate.runner`), device status and capability checks are per runner; window answers are credited to the runner's owner.
- `machine_setup`: `SetupTurn.runner`, `SetupConversation.runner` (main chat picker), `thread_runner.py`; send gates and tools use the thread's runner.
- `machine_readiness`: ports, locks and inventory per runner; tab picker; Check machine uses the run's runner.
- `mcp_gateway`: machine, capture and browse tools use the claimed case's run's runner; machine lock per runner; hold refusals mapped to neutral text.
- Test Runner 0.25.0 (`controller_client/`): `CONTROLLER_RUNNER_TOKEN` (`--runner-token`), `CONTROLLER_RUNNER_NAME`; API key and person email (and its prompt) removed; window title shows the runner name.
- `accounts/admin.py`: deactivating a user closes their runners' sockets.

## Why
A runner was tied to one project by the project's Connection key, so moving a machine between projects meant editing its `.env` and restarting it. Runners now belong to users and are chosen per run, so one install serves every project its owner can write to, and runs of different projects run in parallel on different runners.

## Behaviour and contract changes
- **Breaking:** runners below 0.25.0 or without `USER_RUNNER_V1` are refused at handshake (`incompatible`, points to `/runners/`). Every machine needs a fresh install from `/runners/`.
- Handshake sends `runner_token` (no `api_key`, `person_email`); ack returns `runner_id`, `runner_name`.
- The Connection key is the MCP credential only.
- New URLs: `runners:*` (`/runners/`, `/runners/<id>/…`, `/runners/all/`), `projects:test_run_runner`, `projects:test_run_runner_release`, `machine_setup:runner_select`. Removed: `projects:download_controller_client`, `projects:runner_update`, `projects:force_disconnect`; WebSocket `agent-status` replaced by `ws/runners/<id>/status/`, `ws/runners/status/` and `ws/projects/<id>/events/` (`prompt_refined`).
- Setting `RUNNER_MAX_PER_USER` (default 20).
- Migrations: `runners.0001`; `projects.0061` (fields), `0062_user_runner_deploy_data` (Started agent runs cancelled with generation bump, checkpoints kept; Manual window claims released; uploading hand-ins marked incomplete; `RunnerUpdate` rows deleted), `0063_drop_project_runner_state` (drops `Project.agent_*`, `last_connected_at`, `RunnerUpdate.project`); `machine_setup.0013`, `0014_end_live_turns_for_user_runners`; `machine_readiness.0002`. The data steps are not reversible.
- Hostnames and system details of a runner are shown only to the owner and to writable members of the project it serves.

## Notes for future changes
- Deploy: announce; let Started agent runs finish or pause them and let runner windows finish uploading; `migrate`; rebuild the image; restart Daphne, web, `mcp` and every Celery worker; each user adds runners on `/runners/`, installs the ZIPs and assigns them to paused or cancelled runs (Resume works on cancelled runs).
- Phase 2 (parallel runs within one project) is not done: one active run and one live setup turn per project remain.
- A Started Manual run without a runner cannot get one (a Started run cannot switch runner); stop and re-run it with a runner.
- Lock order is always runner row (`runners/locks.lock_runner_row`), then project, then run; two runners in id order. New hold kinds go only in `projects/runner_use.py`.
- Every path that makes a runner act for a project checks `runner_serves_project` (owner active, writable member, runner not archived).
- `execution` worker concurrency is the deployment's cap on parallel agent runs.
- Nothing was run against a database, a runner or a browser yet; `scripts/verify_server_image.sh` was not run (no Docker locally).
