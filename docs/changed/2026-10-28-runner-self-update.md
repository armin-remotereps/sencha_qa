# Test Runner self-update from the panel

Date: 2026-10-28
Plan: docs/plan/feat-runner-self-update.md

## What changed
- `controller_client/protocol.py`: `CLIENT_VERSION` 0.8.0, optional capability `self_update_v1`, messages `update_runner` / `update_status`, `UpdatePhase`, `UPDATE_MAX_SECONDS`, `MAX_UPDATE_ARCHIVE_BYTES` (7 MiB), `MAX_MESSAGE_SIZE`, handshake field `update_in_flight_id`, parsers.
- `projects/models.py`: `RunnerUpdate` (one in flight per project, audit of who pressed it), migration `projects.0039_runner_update`.
- `projects/runner_bundle.py`: builds the runner ZIP (download keeps `.env`; the update bundle never has one). `generate_controller_client_zip` is a thin wrapper.
- `projects/runner_update_offer.py`, `runner_update_request.py`, `runner_update_progress.py`, `runner_update_state.py`: offer state and gates, request (project lock, owner channel only), phase recording, handshake settle (succeeded / kept / lost), expiry.
- Reverse gates while an update is in flight: `start_test_run`, readiness check `no_runner_update`, `list_reopen_blockers` (Resume, Re-run, redesign, Continue), Machine setup send, Night auto-fix.
- `projects/controller_consumer.py`, `controller_protocol.py`, `consumers.py`: send `update_runner`, record `update_status`, settle on handshake, live `runner_update` event on the agent-status socket.
- `projects/runner_update_views.py`, Environment page Connection card: Update Test Runner button, confirm dialog, live phases, outcome; Runner-outdated help card links to it.
- `controller_client/update_archive.py`, `update_layout.py`, `update_installer.py`, `update_handover.py`, `update_cleanup.py`, `update_runner.py`; `client.py`, `config.py`, `main.py`, `cleanup.py`: verify and extract into `<base>/test-runner-<version>/`, new venv + pip + Playwright, detached start, handover, old-folder removal by the new runner, `start-test-runner.sh|.cmd`, file logging.
- `projects/case_faults.py`, `case_help_services.py`: Runner outdated restores on Continue; paused cases switch to restore after a successful update.
- Setup scripts, `.gitignore`, `.dockerignore`, `scripts/verify_server_image.sh`, CLAUDE.md.

## Why
Updating the Test Runner by hand on every machine was slow; a member can now update it from the panel.

## Behaviour and contract changes
- New URL `projects:runner_update` (POST, `projects/<id>/runner-update/`).
- Migration `projects.0039_runner_update` (additive).
- New setting `RUNNER_UPDATE_TIMEOUT_SECONDS` (default 3900; values below 3660 refuse to start).
- Controller protocol: version 0.8.0, `self_update_v1`, `update_runner`, `update_status`, handshake `update_in_flight_id`. Runners ≤ 0.7.0 need one last manual update.
- Deploy: `migrate`, rebuild the server image (it ships the runner bundle), restart Daphne/web and every Celery worker.

## Notes for future changes
- Mechanism is the user's choice: side by side, no rollback. If the new runner never connects within 5 minutes, the old one stays connected, stops the new process and reports `failed` with the log path; the new folder is kept.
- Only folders with `.punk-hazard-runner` count as managed; a runner in a source checkout refuses to update.
- New OS packages (`playwright install-deps`, apt) are out of scope: such a release needs the setup script by hand.
- Accepted residual: after a server-side expiry, a second Update pressed while the first install still runs can be marked succeeded by the first install's new runner.
- Unverified on real machines: macOS Screen Recording/Accessibility permissions for the detached runner, a systemd unit with `KillMode=control-group`, Windows end to end. Verified with pre-commit (strict mypy incl. the win32 pass), `manage.py check`, `makemigrations --check`, `scripts/verify_server_image.sh`, throwaway-DB end-to-end smokes (pages, real consumer, gates), runner smokes (archive, layout, deletion guards, spawn/terminate, deadlines), and three reviewer/verifier rounds.
