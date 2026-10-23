# Move OmniParser off the Test Runner onto remote GPU vision workers

Date: 2026-10-24
Plan: docs/plan/refactor-remote-vision-worker.md

## What changed
- `vision_worker/` (new standalone client): OmniParser (vendored code moved from `controller_client/omniparser/`) runs on a GPU machine with `python -m vision_worker` or the CUDA Docker image (`Dockerfile`, `docker-compose.yml`, one service per GPU). It connects out over wss, authenticates with `VISION_WORKER_TOKEN`, preloads the model, heartbeats `status` every 30 s and answers `parse_request`. Wire protocol: `vision_worker/protocol.py` (stdlib only, imported by the server). `python -m vision_worker.diagnose` checks the setup.
- `visual_engine` (new Django app):
  - `VisionWorker` model, admin view (read-only), and `VisionWorkerConsumer` on `ws/vision-worker/`.
  - Services: `parse_screenshot` dispatches round robin across ready workers using a Redis counter, skips busy workers, and retries disconnects and timeouts within `VISION_PARSE_BUDGET_SECONDS`. `wait_for_ready_worker` waits for a worker; `pool_status` computes the engine state.
- `projects/services.controller_find_elements`: runner `controller_screenshot`, then `parse_screenshot`; its return type is unchanged. Run, resume, re-run and machine-setup gating now need at least one ready worker. `MachineState` gains `offline`.
- `controller_client` 0.6.0: OmniParser, torch, the weights, `find_element` and `omniparser_status` are removed. Setup no longer needs a GPU.
- Templates: every machine badge and Alpine component reads `visualEngine` instead of `omniparserStatus`. The Environment page gets a "Visual engine" card listing workers.

## Why
OmniParser needs a GPU and made every Test Runner a heavy install. It now runs on dedicated GPU machines, and several of them share the load round robin.

## Behaviour and contract changes
- New WebSocket endpoint `ws/vision-worker/` (worker ⇄ server). Browser sockets get a new `{"type": "visual_engine", ...}` message, and `agent_status` / `current_state` carry `visual_engine` instead of `agent_omniparser_status`.
- Runner protocol: `find_element`, `find_element_result`, `omniparser_status` and their two capabilities are removed. 0.5.x runners still connect, and their status messages are ignored. A 0.6.0 runner is rejected by an older server, so deploy the server first.
- Settings: added `VISION_WORKER_TOKEN`, `VISION_PARSE_ATTEMPT_TIMEOUT`, `VISION_PARSE_BUDGET_SECONDS`, `VISION_WORKER_WAIT_SECONDS`, `VISION_WORKER_READY_TIMEOUT` and `VISION_WORKER_STALE_SECONDS`. Removed `CONTROLLER_FIND_ELEMENT_TIMEOUT` and `CONTROLLER_OMNIPARSER_READY_TIMEOUT`.
- Migrations: `visual_engine.0001` (additive) and `projects.0026`, which **drops `Project.agent_omniparser_status`** (transient status only). Restart Daphne and all Celery workers together after `migrate`.
- With no worker connected the visual engine is "offline" and no run can start; bring a worker up before cutting over.

## Notes for future changes
- A late worker reply is dropped by the consumer (`PendingParses`, absolute `deadline_epoch`); otherwise it stays forever in the channel-layer receive buffer of whichever Celery process reads next. Keep any new reply path deadline-aware.
- The worker answers `BUSY` beyond 1 running + `VISION_MAX_QUEUE`, and the server backs off 1→8 s. Server-side in-flight leases (least-busy dispatch) were rejected because a killed Celery process leaks its lease.
- The default worker name includes `CUDA_VISIBLE_DEVICES`. Two processes with the same name supersede each other (close 4001, and the older one exits).
- macOS Retina coordinate scaling is a pre-existing issue and is untouched.
- Not yet run live: a real CUDA/Docker build, Windows Docker Desktop, and an end-to-end case through Daphne + Celery. The worker client was exercised on MPS against a fake server.
