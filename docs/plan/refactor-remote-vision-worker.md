# refactor: Remote vision worker (OmniParser moves off the Test Runner onto GPU workers)

Status: Done
Created: 2026-10-24
Request: Take OmniParser out of the Test Runner (`controller_client`) and run it as a
separate "vision worker". A vision worker runs on a GPU machine (the user's PC), connects
out to the server over WebSocket, and handles every element-finding request
(`find_element` / `parse_screen`) that the Test Runner handles locally today. It starts with
`python -m vision_worker`, like the runner, or as a Docker container. Follow-up: several
vision workers (several GPUs) can be connected at once, and parse requests are spread across
them round robin.

## Open questions

Each has a default this draft is built on. The plan stays **Draft** until they are answered.

- **Q1. Local fallback.** Should the Test Runner keep a local OmniParser fallback for when no
  vision worker is connected?
  *Assumption:* no. OmniParser, torch and the weights are removed from `controller_client`
  entirely, and with no vision worker the visual engine is "offline".
- **Q2. Dispatch policy.** Plain round robin queues a request on a worker that is already
  busy, even when another worker is idle, because a GPU parses one image at a time. The
  options are:
  - (a) plain round robin;
  - (b) round robin with busy-skip: each worker accepts at most 1 running + 1 queued request
    and answers `BUSY` beyond that, and the server moves on to the next worker;
  - (c) server-side least-in-flight, tracked with Redis leases.

  *Assumption:* (b). It keeps the round robin the user asked for and stops idle GPUs from
  sitting next to a long queue, and the server keeps no counters that a killed Celery worker
  could leak. (c) balances best but needs lease bookkeeping and crash cleanup. Revisit it only
  if (b) measurably queues.
- **Q3. Worker authentication.** Should all workers share one token from `.env`, or should
  each worker get its own revocable key stored in the DB and managed in admin?
  *Assumption:* one shared `VISION_WORKER_TOKEN` in the server `.env`. Rotating it means
  editing `.env` and restarting Daphne.
- **Q4. GPU PC OS and run mode.** What OS does the GPU PC run, and how will the worker run
  there: Linux or WSL2 with Docker and the NVIDIA Container Toolkit, or Windows natively?
  *Assumption:* both are supported. There is a Dockerfile (Linux/WSL2) and
  `python -m vision_worker` in a venv, set up by `setup.sh` / `setup.ps1`.
- **Q5. Who sees the worker list.** Vision workers are global. Should every logged-in member
  see the worker list (name, state, device) on every project's Environment page?
  *Assumption:* yes, read-only, on the Environment page only. There is no global admin page,
  and the Django admin gets a read-only `VisionWorker` view.
- **Q6. No ready worker during a case.** When no worker is ready mid-case, the server waits up
  to `VISION_WORKER_WAIT_SECONDS` (default 30) and then blocks the case as `visual_engine`.
  Should that wait be kept out of agent time, the way runner reconnect waits are?
  *Assumption:* no, it counts as agent time. The outage clock's allowance belongs to runner
  reconnects, and sharing it would shrink that allowance.
- **Q7. Dropping `Project.agent_omniparser_status`.** Should the column be dropped in the
  same release?
  *Assumption:* yes. It only holds transient runner status and is rewritten on every connect,
  so no lasting data is lost. See Risks for the deploy order.
- **Q8. Screenshots leave the server.** Every element search now sends a full-resolution
  screenshot of the test machine from the server to the GPU PC over the internet (wss), and
  the annotated copy comes back. That adds about 1–5 MiB of upload per search. Is that
  acceptable for privacy and latency?
  *Assumption:* yes.
- **Q9. Distributing the worker.** Should the panel offer a vision worker ZIP download, like
  the Test Runner ZIP?
  *Assumption:* no. The worker is installed from a repo checkout or a Docker build on the GPU
  PC, and it needs no per-project key.

## Context

What exists today (all of it read for this plan):

- **OmniParser runs inside the Test Runner.**
  - `controller_client/omniparser/` holds the vendored code. `.gitignore`d weights live in
    `omniparser/weights`.
  - `controller_client/omniparser_executor.py`:
    - `execute_find_element` takes its own screenshot through
      `executor.execute_screenshot`, then runs OCR and SoM under a process-wide lock.
    - It returns `FindElementResultPayload`: pixel elements plus an annotated PNG.
    - `bound_find_element_result` downscales the annotated image until the message fits
      under `OMNIPARSER_MAX_RESULT_BYTES` (8 MiB).
  - Also in `controller_client/`: `omniparser_config.py`, `omniparser_diagnostics.py`
    (`DiagnosticStep` and `run_diagnostics` are reused by `browser_diagnostics.py`),
    `diagnose.py`, `scripts/download_omniparser_weights.sh`, the weights and OCR-warmup
    steps in `scripts/setup.{sh,ps1,bat}`, and the heavy deps in `requirements.txt`
    (torch, torchvision, ultralytics, easyocr, paddle*, transformers, accelerate, timm,
    huggingface_hub, supervision, opencv, numpy, einops, openai).
- **Runner protocol** (`controller_client/protocol.py`, `CLIENT_VERSION` 0.5.1):
  - `MessageType.FIND_ELEMENT`, `FIND_ELEMENT_RESULT` and `OMNIPARSER_STATUS`.
  - `ClientCapability.FIND_ELEMENT_LOCAL_V1` and `OMNIPARSER_STATUS_V1`, both in
    `REQUIRED_CLIENT_CAPABILITIES`.
  - `OmniParserState` and `OmniParserStatusPayload`.
  - The `FindElement*` and `Pixel*` payloads, `serialize_find_element_result`,
    `parse_find_element_payload` and `parse_omniparser_status_payload`.
  - `ErrorCode.FIND_ELEMENT_FAILED` and `OMNIPARSER_NOT_READY`.
- **Runner client** (`controller_client/client.py`):
  - After the handshake ack, `_initialize_omniparser` preloads the models and sends
    `omniparser_status`.
  - `_handle_find_element` runs `execute_find_element`.
  - Incoming WebSocket messages are capped at `MAX_MESSAGE_SIZE` (10 MiB).
- **Server dispatch** (`projects/services.py`):
  - `controller_find_elements` (line ~1440) sends `controller.find_element` with
    `runner_local=True` through `_dispatch_controller_action`, then builds a
    `PixelParseResult` (`agents/types.py`).
  - `controller_screenshot` (line ~1373) gets a full-resolution PNG from the same
    `execute_screenshot`. It retries a timeout 3 × 30 s and never downscales.
  - `_VISUAL_ENGINE_ERROR_CODES` maps the two OmniParser error codes to
    `VisualEngineError` (`projects/exceptions.py`). `projects/case_blocking.py` maps that
    error to `BlockedReason.VISUAL_ENGINE`.
- **Readiness** (`projects/services.py`):
  - `claim_agent_connection` writes `_initial_omniparser_status()`.
  - `update_agent_omniparser_status` stores the runner's reports on
    `Project.agent_omniparser_status`.
  - `get_machine_state(project)` derives DISCONNECTED / STARTING / READY / FAILED. Its
    callers:
    - `get_run_readiness` (`_visual_engine_ready_check`);
    - `projects/run_reopen_services._machine_blocker`;
    - `machine_setup/services/send_message._refusal` (it loads `.only(..., "agent_omniparser_status")`)
      and `chat_page`;
    - `dashboard/services._build_project_card` (once per project);
    - `projects/views.project_environment`.
  - `execute_test_run_test_case` calls `_wait_for_omniparser_ready` before every case,
    bounded by `CONTROLLER_OMNIPARSER_READY_TIMEOUT`.
- **Consumers and UI:**
  - `projects/controller_consumer.py` handles `omniparser_status` and `find_element_result`
    (through `ReplyTracker.send_find_element_result`).
  - `projects/controller_protocol.py` has `FindElementActionEvent` and the registry entries.
  - `AgentStatusConsumer` (`projects/consumers.py`) and `MachineSetupConsumer`
    (`machine_setup/consumers.py`) push `agent_omniparser_status`.
  - Templates read it in Alpine (`omniparserStatus`) to derive `machineState`:
    `projects/environment.html`, `overview.html`, `wizard/step_machine.html`,
    `machine_setup/chat.html`, `partials/advanced_diagnostics.html`,
    `projects/includes/_environment_visual_engine_failure.html`,
    `projects/includes/_project_table.html` (per row, from the project field),
    `partials/README.md`, and `partials/badge_machine.html`.
- **Consumers of the parse result:**
  - `agents/services/controller_element_finder.py` (`parse_screen`, `match_element`) and
    `verified_element_actions.py` consume `PixelParseResult` and stay unchanged.
  - The annotated image is published as tool `controller_omniparser`;
    `testrail_checked_push_services` filters it out by that name.
- **Coordinate space.** Nothing scales coordinates on either side: the element pixels are the
  pixels of `execute_screenshot`'s PNG, and clicks go to `pyautogui.click(x, y)` unchanged.
  The new flow parses the same `execute_screenshot` image, so clicks land exactly as today.
  One pre-existing caveat is untouched: on macOS Retina, `screencapture` returns physical
  pixels while pyautogui works in points. It is not changed by this plan.
- **Infrastructure:**
  - Agents run in Celery `execution` workers (prefork, concurrency 10), not in Daphne.
  - The channel layer is `channels_redis` (defaults: expiry 60 s, capacity 100, no size
    limit).
  - Daphne runs with 32 MiB WebSocket message and frame limits (`docker-compose.yml`), and
    nginx proxies all of `/ws/`.
  - Django cache is django-redis (atomic `incr`).
- **Existing patterns to copy:**
  - `projects/controller_reply_tracker.py`: a request_id → reply_channel map, failed in bulk
    on disconnect.
  - `_send_controller_action_once`: a new reply channel per attempt, so a late reply is
    never read.
  - `claim_agent_connection` / `release_agent_connection`: the newest connection wins, and a
    release only clears a channel that still owns the row.
- **Shared protocol and packaging:**
  - The server imports `controller_client.protocol` directly. `setup.cfg` excludes
    `controller_client` from discovery, but mypy follows the import.
  - `.dockerignore` keeps the `controller_client/` source in the image.
  - `generate_controller_client_zip` zips everything except `.venv`, `weights`, `tests`,
    caches and `.env`.
  - `scripts/verify_server_image.sh` asserts that the omniparser files are present.
- **No throttling exists anywhere in the codebase.** Daphne runs without `--proxy-headers`,
  so `scope["client"]` is nginx's address.

## Goals

- A new standalone client package, `vision_worker/`:
  - It runs OmniParser on a GPU and connects out over wss to `ws/vision-worker/`.
  - It authenticates with a global token, sends a version and capabilities, preloads the
    model, and reports loading / ready / failed plus the device.
  - It parses images the server sends and returns today's result shape.
  - It runs with `python -m vision_worker` or Docker (CUDA), and has its own diagnose command.
- Any number of workers, global to all projects. Parse requests rotate round robin over the
  ready workers, a busy worker is skipped, and a disconnect mid-request is retried on another
  worker within a bounded budget.
- `controller_find_elements(project_id, ...) -> PixelParseResult` keeps its contract. It now
  works in two steps: a runner screenshot, then a vision-worker parse.
  `agents/services/controller_element_finder.py` and its callers are unchanged.
- Run, resume, re-run and machine-setup gating move from the project's OmniParser status to
  "at least one ready vision worker".
- OmniParser, torch and the weights are fully removed from the Test Runner, which becomes a
  light install.
- The UI shows the visual engine state, and the Environment page lists the connected workers.
- Typed, mypy-strict for the server, `controller_client/` and `vision_worker/`, all in
  pre-commit.

## Non-goals

- Changing OmniParser itself, its thresholds, or the vendored code.
- Fixing HiDPI/Retina coordinate scaling. It is pre-existing and needs its own fix plan.
- Per-project worker pools, worker affinity, or priority between projects.
- Server-side least-in-flight scheduling (Q2 alternative c).
- A panel download for the vision worker (Q9).
- Throttling failed handshakes by client IP. No real client IP reaches Daphne today, and
  the token is 256-bit random. See Risks.
- Browser element finding (`browser_element_finder.py`). It does not use OmniParser.

## Design

### Shared protocol: `vision_worker/protocol.py`

This copies the existing pattern exactly: the server imports `controller_client.protocol`,
and now it also imports `vision_worker.protocol`.

- **Dependencies.** The module is stdlib-only and imports only `vision_worker.exceptions`.
  `vision_worker/__init__.py` stays empty, so the server never imports torch.
- **Where the find-element types go.** The controller no longer needs any of them, so they
  move out of `controller_client/protocol.py` and nothing is duplicated.
- **Contents:**
  - **Constants:**
    - `WORKER_VERSION: Final = "1.0.0"`;
    - `HEARTBEAT_INTERVAL_SECONDS: Final = 30`;
    - `HANDSHAKE_ACK_TIMEOUT_SECONDS: Final = 30`;
    - `MAX_MESSAGE_BYTES: Final = 32 * 1024 * 1024`, which matches Daphne's flag.
  - **`MessageType`:**
    - `handshake`, `handshake_ack`;
    - `status` (also the heartbeat);
    - `parse_request`, `parse_result`;
    - `error`.
  - **`WorkerCapability`:** `PARSE_V1`. `REQUIRED_WORKER_CAPABILITIES = {PARSE_V1}`.
  - **`WorkerState`:** `loading`, `ready`, `failed`.
  - **`ErrorCode`:**
    - `INVALID_TOKEN`, `INCOMPATIBLE`, `INVALID_MESSAGE`;
    - `NOT_READY`, `BUSY`, `EXPIRED`, `PARSE_FAILED`;
    - `WORKER_DISCONNECTED`, which only the server sends to its waiters.
  - **`CloseCode.SUPERSEDED = 4001`.** A newer connection with the same worker name took
    over. The worker exits on it.
  - **Frozen dataclasses:**
    - `HandshakePayload(token, worker_name, worker_version, capabilities, system_info)`.
      `system_info` holds hostname, OS, python, torch, GPU name and VRAM.
    - `HandshakeAckPayload(status, message)`.
    - `StatusPayload(state, message, device, phase, load_seconds)`.
    - `ParseRequestPayload(image_base64, box_threshold: float | None, iou_threshold: float | None, timeout_seconds: float)`.
    - `PixelBBoxPayload`, `PixelElementPayload` and `ParseResultPayload`.
      `ParseResultPayload` has `annotated_image_base64`, `elements`, `image_width`,
      `image_height`, `ocr_seconds`, `som_seconds` and `queue_seconds`; the rest is moved
      unchanged from the controller.
  - **Functions:**
    - `serialize_message`;
    - `serialize_parse_result`;
    - `parse_handshake_payload`, `parse_status_payload`, `parse_parse_request_payload`,
      `parse_parse_result_payload`;
    - `deserialize_message`, which returns `(MessageType, request_id, data)`.

    The server consumer uses these to validate what workers send. There are no hand-rolled
    `data.get` chains.

### Data model (new app `visual_engine`)

One Django app per domain: the visual engine is global and has nothing to do with projects.
The name `visual_engine` is the user-facing term, and it avoids confusion with the
`vision_worker/` client package.

`visual_engine/models.py`, `VisionWorker`:

| Field | Type | Notes |
|---|---|---|
| `name` | CharField(100), unique | From the worker's `VISION_WORKER_NAME`, e.g. `armin-pc-gpu0`. This is its stable identity: the row is reused across reconnects. |
| `connected` | Bool, db_index | |
| `channel_name` | CharField(255), blank | The consumer that owns the connection. `""` when disconnected. |
| `state` | CharField(16), choices `WorkerState`, blank | `""` until the first status. |
| `status_message` | CharField(500), blank | Truncated. |
| `device` | CharField(64), blank | e.g. `cuda`, `cpu`. |
| `client_version` | CharField(32), blank | |
| `capabilities` | JSONField(list) | |
| `system_info` | JSONField(dict) | GPU name and VRAM, for the UI. |
| `load_seconds` | FloatField, null | |
| `connected_at` | DateTime, null | |
| `last_seen_at` | DateTime, null, db_index | Bumped on every `status`/heartbeat. |
| `last_disconnected_at` | DateTime, null | |

- **Selection query:**
  `connected=True, state=ready, last_seen_at >= now - VISION_WORKER_STALE_SECONDS`,
  ordered by `id`, reading only `id, name, channel_name`. That is one indexed query over a
  handful of rows.
- **Why stale rows matter.** If Daphne crashes, rows stay `connected=True` with dead channels
  and the consumer's `disconnect` never runs. The heartbeat filter drops them within 90 s, and
  a reconnect under the same name claims the row again.
- **Migrations.**
  - `visual_engine/migrations/0001_initial.py` is additive.
  - `projects/migrations/0026_remove_project_agent_omniparser_status.py` drops
    `Project.agent_omniparser_status`. **This deletes data.** The column only holds the
    runner's last OmniParser status, which `claim_agent_connection` overwrites on every
    connect, so nothing of lasting value is lost. The migration is reversible: rollback
    re-adds the column with `default=dict`.
- **Admin.** `visual_engine/admin.py` adds a read-only `ModelAdmin[VisionWorker]`: list
  display only, no add or change.

### Types: `visual_engine/types.py`

- `VisualEngineState(StrEnum)`:
  - `READY` when at least one worker is ready;
  - `STARTING` when none is ready but at least one connected worker is loading or has not
    reported yet;
  - `FAILED` when workers are connected but every one has failed;
  - `OFFLINE` when no live worker is connected.
- `VisionWorkerView` (frozen): `name`, `state`, `device`, `gpu`, `status_message`,
  `client_version`, `connected_since`.
- `VisualEngineStatus` (frozen): `state`, `ready_count`, `connected_count`,
  `workers: tuple[VisionWorkerView, ...]`, and `to_payload() -> VisualEngineStatusPayload`.
- `VisualEngineStatusPayload(TypedDict)` is the JSON shape for templates and WebSockets:
  `{state, ready_count, connected_count, workers: [{name, state, device, gpu, message, client_version, connected_since}]}`.

### Services: `visual_engine/services/`, one use case per module

- **`worker_auth.py`**
  - `is_valid_worker_token(token: str) -> bool`: `secrets.compare_digest` against
    `settings.VISION_WORKER_TOKEN`. An empty setting means False.
  - `check_worker_compatibility(worker_version: str, capabilities: Sequence[str]) -> str | None`.
- **`worker_connection.py`**
  - `claim_worker(*, name, channel_name, worker_version, capabilities, system_info) -> str`:
    a `select_for_update` upsert by `name`. It sets connected, the channel, `connected_at`
    and `last_seen_at`, and `state=""`. It returns the replaced channel, or `""`, and
    broadcasts after commit.
  - `release_worker(*, name, channel_name) -> bool`: a conditional UPDATE that only clears a
    channel which still owns the row. It broadcasts when a row changed.
- **`worker_status.py`**
  - `record_worker_status(*, name, channel_name, status: StatusPayload) -> None`.
  - Two conditional UPDATEs, both filtered on `(name, channel_name)`, so a superseded socket
    writes nothing:
    1. `.exclude(state=new_state).update(state, message, device, load_seconds, last_seen_at)`.
       If it changed a row, the state changed, so broadcast.
    2. Otherwise `.update(last_seen_at, message)`, which is the heartbeat path and does not
       broadcast. Heartbeats never spam the UI.
- **`pool_status.py`**
  - `get_visual_engine_status() -> VisualEngineStatus`: one query over the connected,
    non-stale rows.
  - `broadcast_visual_engine_status() -> None`: `group_send` to
    `VISUAL_ENGINE_GROUP = "visual_engine_status"` with
    `{"type": "visual_engine.status", "visual_engine": payload}`.
- **`wait_for_ready_worker.py`**
  - `wait_for_ready_worker(*, timeout: float, cancellation_check: Callable[[], bool] | None) -> None`.
  - It polls every 2 s, using `agent_monotonic()` as today's wait does. It raises
    `AgentCancelledError` on cancel. On timeout it raises `VisualEngineError` with a fixed
    message naming the last state: "No vision worker became ready within Ns (state: offline)".
  - It replaces `_wait_for_omniparser_ready` and `_raise_for_omniparser_failure`.
- **`parse_screenshot.py`**
  - `parse_screenshot(image_base64: str, *, image_width: int, image_height: int, box_threshold: float | None, iou_threshold: float | None) -> PixelParseResult`.
    This is the dispatcher; it runs synchronously in Celery. The steps:
    1. If no worker is ready, wait up to `VISION_WORKER_WAIT_SECONDS`, using the same poll
       helper as `wait_for_ready_worker`. If none becomes ready, raise
       `VisualEngineError("No vision worker is ready ...")`.
    2. Set the budget deadline to `now + VISION_PARSE_BUDGET_SECONDS`. Each attempt waits at
       most `min(VISION_PARSE_ATTEMPT_TIMEOUT, remaining)`.
    3. **Round robin.** Load the ready list (ordered by id), take
       `n = cache.incr("visual_engine:rr")` (seeded with `cache.add(key, 0)`), and start at
       `ready[n % len(ready)]`. Walk the list from that offset, skipping workers already
       tried in this call. The counter is shared by every Celery process through Redis.
       Nothing is kept in process memory.
    4. **One attempt:**
       - create a new reply channel;
       - `layer.send(worker.channel_name, {"type": "vision.parse", request_id, reply_channel, image_base64, box_threshold, iou_threshold, timeout_seconds})`;
       - `layer.receive` with a timeout.

       This mirrors `_send_controller_action_once`. A late reply lands on an abandoned
       channel and expires after 60 s.
    5. **Outcomes:**

       | Outcome | What happens |
       |---|---|
       | `parse_result` | Validate it: `image_width`/`image_height` must equal the screenshot's size, otherwise the coordinate space is wrong and the server raises `VisualEngineError`. Build `PixelParseResult`, log the worker name and timings, return. |
       | `error BUSY` | The worker stays eligible. Once every ready worker was tried and some were busy, sleep 1 s and try the busy ones again while budget remains. |
       | `error NOT_READY`, `error WORKER_DISCONNECTED`, attempt timeout | Mark the worker tried; next worker. A timed-out worker is not retried in this call. |
       | `error PARSE_FAILED` / `INVALID_MESSAGE` | Raise `VisualEngineError` at once. These are deterministic and would fail the same way elsewhere. |
       | Budget spent, or no untried workers left | Raise `VisualEngineError("Visual engine did not return a result: <last error>")`. |

    6. The `_build_pixel_element` mapping moves here from `projects/services.py`.
  - Retrying is safe because a parse is idempotent.
- **`reply_routing.py`** (consumer-side, not a service).
  `PendingParses(channel_layer)` holds `register`, `send_result`, `send_error` and
  `fail_all(code, message)`. It is a 40-line, vision-only copy of the `ReplyTracker` idea.
  `projects/controller_reply_tracker.py` is not grown, because it is controller-specific.

### Exceptions

- The server reuses `projects.exceptions.VisualEngineError`. It is already an
  `InfrastructureError`, already maps to `BlockedReason.VISUAL_ENGINE`, and already
  re-raises through `safe_tool_call`. `visual_engine` may import `projects.exceptions`,
  which has no services imports; it must **never** import `projects.services`, because
  `projects.services` imports `visual_engine.services`.
- New `visual_engine/exceptions.py` holds only consumer-internal errors
  (`WorkerHandshakeError`).
- `vision_worker/exceptions.py`: `ProtocolError`, `HandshakeError`, `AuthenticationError`,
  `ConnectionTerminatedError`, `EngineError(phase, device, weights_dir)`. `EngineError` is
  the old `OmniParserError`.

### Server consumer: `visual_engine/consumers.py`, `VisionWorkerConsumer`

- **Route.** `ws/vision-worker/`, added in `auto_tester/asgi.py` next to
  `ws/controller/` through `visual_engine/routing.py`. There is no
  `AllowedHostsOriginValidator`, because the worker sends no Origin header (same reason as
  the controller).
- **`connect`.**
  - Accept, then start a `HANDSHAKE_ACK_TIMEOUT_SECONDS` timer. If no valid handshake arrives
    in time, close with 4003. The controller consumer has no such timer.
  - Text frames only. Bytes frames are ignored.
- **Handshake** (one attempt per socket):
  1. Parse it with `parse_handshake_payload`.
  2. Check `is_valid_worker_token`. On failure, send ack `error` "Invalid token", log a
     WARNING with the worker name, and close. The token is never logged.
  3. Check `check_worker_compatibility`. On failure, send `incompatible` and close.
  4. `group_add` is not needed: the worker is addressed by its channel name.
  5. Run `claim_worker`. Any replaced channel gets `channel_layer.send(prev, {"type": "vision_worker.superseded"})`,
     which closes it with 4001.
  6. Send ack `ok`.
- **After the handshake:**
  - `status` → `record_worker_status`.
  - `parse_result` / `error` → `PendingParses` → reply channel.
  - Anything else gets an `INVALID_MESSAGE` error reply.
- **Event handlers:**
  - `vision_parse(event)` registers the reply channel and forwards a `parse_request` frame.
  - `vision_worker_superseded` closes with 4001.
- **`disconnect`.** `release_worker`, then
  `PendingParses.fail_all(WORKER_DISCONNECTED, "Vision worker connection lost")`. Release
  comes first so that the waiters' retry never picks this channel again.
- **Size.** Inbound frames are capped by Daphne at 32 MiB. The worker bounds its result to
  `VISION_MAX_RESULT_BYTES` (8 MiB by default).

### Vision worker client: `vision_worker/`

Its own venv, `requirements.txt` and strict `setup.cfg`, like `controller_client`.

- **`__main__.py` → `main.py`.** Loads the config, sets up logging, runs
  `VisionWorkerClient.run()`, and handles SIGINT/SIGTERM.
- **`config.py`** (python-decouple: it is a separate codebase, as `controller_client/config.py` is).
  - `VISION_SERVER_URL`, a full URL such as `wss://sench.remotereps.com/ws/vision-worker/`.
    It **must be `wss://`** unless the host is loopback or private, or
    `VISION_ALLOW_INSECURE=true` is set.
  - `VISION_WORKER_TOKEN`.
  - `VISION_WORKER_NAME`. The default is `<hostname>-<device>`.
  - `VISION_DEVICE`: `auto` (cuda → mps → cpu), `cuda`, or `cpu`. Pick a GPU with
    `CUDA_VISIBLE_DEVICES`, one process per GPU; `cuda:N` is not passed through, because
    easyocr always uses the default device.
  - The weights and model settings move over from the controller: `OMNIPARSER_WEIGHTS_DIR`,
    `OMNIPARSER_BOX_THRESHOLD`, `OMNIPARSER_IOU_THRESHOLD`,
    `OMNIPARSER_CAPTION_BATCH_SIZE`, and `VISION_MAX_RESULT_BYTES` (default 8 MiB).
  - `VISION_MAX_QUEUE`: queued requests beyond the running one, default 1 (Q2).
  - Reconnect settings: `VISION_RECONNECT_INTERVAL` (5) and
    `VISION_RECONNECT_MAX_INTERVAL` (60).
  - `VISION_LOG_LEVEL`.
- **`engine.py`.** Moved from `controller_client/omniparser_executor.py`:
  - The screenshot step is removed. `parse(image_bytes, payload) -> ParseResultPayload`
    decodes the PNG it was sent.
  - `detect_device` honours `VISION_DEVICE`.
  - Load, readiness and phases are kept, with the same `_model_lock`, `gc.collect` and
    `torch.cuda.empty_cache`.
  - `bound_result` (formerly `bound_find_element_result`) downscales only the annotated
    image. Elements stay in input pixel coordinates.
- **`client.py`, `VisionWorkerClient`.**
  - **Connection:** `websockets.connect(url, max_size=MAX_MESSAGE_BYTES, ping_interval=30, ping_timeout=120)`,
    the same keepalive reasoning as the runner. Reconnect with exponential backoff, forever.
    Exit on `error`/`incompatible` acks and on close code 4001.
  - **After an `ok` ack:**
    - start the preload task (`asyncio.to_thread(engine.load)`), sending `status` loading,
      then ready or failed;
    - start the heartbeat task, which sends `status` every `HEARTBEAT_INTERVAL_SECONDS`.
  - **On `parse_request`:**
    - If the model is not ready, answer `NOT_READY`.
    - If the queue is full (one running + `VISION_MAX_QUEUE` waiting), answer `BUSY`.
    - Otherwise enqueue it with its receipt time.
  - **A single consumer coroutine** runs the queue: `asyncio.to_thread(engine.parse, ...)`.
    - A request older than its `timeout_seconds` when it reaches the front gets `EXPIRED`
      and is not parsed, so no GPU time goes to a request the server has given up on.
    - It replies with `parse_result` or `PARSE_FAILED`.
  - Pings and heartbeats keep flowing while the GPU works, because inference runs off the
    event loop.
- **`diagnose.py`** (`python -m vision_worker.diagnose`). Reuses the diagnostic-step
  runner. Steps:
  1. imports;
  2. weights;
  3. device, with the GPU name and VRAM;
  4. model load;
  5. one inference on `--image PATH`, or on a generated test image when none is given;
  6. result size against `VISION_MAX_RESULT_BYTES`;
  7. optionally, `--check-server`: connect and complete a handshake.
- **Weights.** `scripts/download_weights.sh` and the PowerShell equivalent download
  `microsoft/OmniParser-v2.0` and do the `icon_caption` → `icon_caption_florence` rename,
  moved from the controller scripts. `scripts/setup.sh` / `setup.ps1` create the venv,
  install torch from the CUDA index (see Settings), install the requirements, download the
  weights, and warm the OCR models.
- **Docker.**
  - `vision_worker/Dockerfile`:
    - base image `python:3.13-slim`;
    - torch and torchvision from the PyTorch CUDA wheel index. The wheels bundle the CUDA
      runtime, so the host needs only the NVIDIA driver and the NVIDIA Container Toolkit
      (WSL2 on Windows);
    - the rest of the requirements, then an OCR warm-up `RUN` so the easyocr and paddle
      models are baked into the image;
    - a non-root user and `ENTRYPOINT ["python", "-m", "vision_worker"]`.
  - The weights are **not** baked in. They are mounted read-only at `/weights`, with
    `OMNIPARSER_WEIGHTS_DIR=/weights`.
  - `vision_worker/docker-compose.yml` has one service per GPU, using
    `deploy.resources.reservations.devices: [{driver: nvidia, device_ids: ['0'], capabilities: [gpu]}]`
    plus `VISION_WORKER_NAME`. Compose's `count` and `device_ids` are mutually exclusive;
    use `device_ids` to pin one GPU per service.
  - There is a `vision_worker/.dockerignore`, and the build context is `vision_worker/`.
- **`README.md`**: setup, the Docker and venv runs, multiple GPUs, diagnose, and
  troubleshooting.

### Test Runner changes (`controller_client/`), version 0.6.0

- **Delete:**
  - `omniparser/`, which moves with `git mv` to `vision_worker/omniparser/`;
  - `omniparser_executor.py`, `omniparser_config.py`, `omniparser_diagnostics.py`;
  - `scripts/download_omniparser_weights.sh`.
- **New `diagnostics.py`.** `DiagnosticStep` and `run_diagnostics` move here from
  `omniparser_diagnostics.py`; `browser_diagnostics.py` imports from it.
- **`diagnose.py`.** The default run is now one step, a screen capture
  (`describe_screen_capture_failure`). `--browsers` is unchanged. `--skip-inference` is
  removed.
- **`protocol.py`:**
  - Remove `FIND_ELEMENT`, `FIND_ELEMENT_RESULT`, `OMNIPARSER_STATUS`,
    `FIND_ELEMENT_LOCAL_V1`, `OMNIPARSER_STATUS_V1`, `OmniParserState`,
    `OmniParserStatusPayload`, `FindElementPayload` and every `Pixel*` /
    `FindElementResult*` type, along with their parse and serialize functions.
  - Remove `ErrorCode.FIND_ELEMENT_FAILED` and `OMNIPARSER_NOT_READY`.
  - `REQUIRED_CLIENT_CAPABILITIES` becomes `{INTERACTIVE_COMMANDS_V1, CLEANUP_ENVIRONMENT_V1}`.
  - `CLIENT_VERSION = "0.6.0"`.
- **`client.py`.** Remove the find-element handler, `_omniparser_task`,
  `_initialize_omniparser`, `_cancel_omniparser_task` and `_send_omniparser_status`.
- **`exceptions.py`.** Remove `OmniParserError`.
- **`env_check.py`.** Docstring wording only.
- **`requirements.txt`.** Drop torch, torchvision, ultralytics, easyocr, paddlepaddle,
  paddleocr, transformers, accelerate, timm, huggingface_hub, supervision,
  opencv-python-headless, numpy, einops and openai. Before dropping `numpy` and `openai`,
  confirm with `git grep` that no remaining controller module imports them.
- **`example.env`.** Drop the `OMNIPARSER_*` lines.
- **`setup.cfg`.** Drop the omniparser exclude.
- **`scripts/setup.{sh,ps1,bat}`.** Drop the weights and OCR-warmup steps and renumber them.
  The setup becomes much faster and needs no GPU.

**Older runners (0.5.x).**

- They still pass the handshake: the server no longer requires the two capabilities, and
  extra advertised capabilities are ignored.
- The `omniparser_status` messages they keep sending fall through the consumer's handler
  lookup and are dropped silently, as unknown types are today.
- They keep loading OmniParser locally for nothing until they are updated. That is harmless.
- **Order matters.** A 0.6.0 runner connecting to an *old* server is rejected as
  incompatible, because the old server requires `FIND_ELEMENT_LOCAL_V1`. Deploy the server
  first; the ZIP served afterwards is 0.6.0.

### Server changes outside `visual_engine`

- **`projects/services.py`:**
  - **`MachineState`** gains `VISUAL_ENGINE_OFFLINE = "offline"`.
  - **`get_machine_state(project: Project, visual_engine: VisualEngineStatus) -> MachineState`:**

    | Condition | Result |
    |---|---|
    | runner not connected | DISCONNECTED |
    | otherwise, engine READY | READY |
    | otherwise, engine STARTING | STARTING |
    | otherwise, engine FAILED | FAILED |
    | otherwise, engine OFFLINE | VISUAL_ENGINE_OFFLINE |

    It takes the engine status as a parameter, so pages that render many projects query it
    once (no N+1).
  - **New `connected_machine_state(visual_engine) -> MachineState`.** It gives the state any
    connected project has right now, for the project list.
  - **`get_run_readiness`** calls `get_visual_engine_status()` once.
    `_visual_engine_ready_check(machine_state)` gets one help text per state:
    - starting: "A vision worker is loading its model…";
    - failed: "Every connected vision worker failed to load; check the worker's log or run
      `python -m vision_worker.diagnose`";
    - offline: "No vision worker is connected. Start one with `python -m vision_worker`."
  - **`controller_find_elements(project_id, box_threshold=None, iou_threshold=None) -> PixelParseResult`:**
    `shot = controller_screenshot(project_id)` (the existing 3 × 30 s retry), then
    `parse_screenshot(shot["image_base64"], image_width=shot["width"], image_height=shot["height"], ...)`.
    The unused `timeout` parameter is dropped (its only caller, `parse_screen`, never
    passes it).
  - **Removed:**
    - `_raise_for_find_element_failure`, `_build_pixel_element` (moved),
      `_VISUAL_ENGINE_ERROR_CODES`;
    - `_initial_omniparser_status`, `update_agent_omniparser_status`,
      `_serialize_omniparser_status`;
    - `_wait_for_omniparser_ready`, `_raise_if_run_cancelled` (moved),
      `_raise_for_omniparser_failure`;
    - `agent_omniparser_status` in `claim_agent_connection`, `_DisconnectedAgentFields`,
      `_disconnected_agent_fields`, `AgentStatusEvent` and `_build_agent_status_event`;
    - the `OmniParserState` / `OmniParserStatusPayload` / `parse_omniparser_status_payload`
      imports.
  - **`execute_test_run_test_case`** calls
    `wait_for_ready_worker(timeout=settings.VISION_WORKER_READY_TIMEOUT, cancellation_check=...)`
    in place of `_wait_for_omniparser_ready`. The runner itself is still awaited with
    `wait_for_controller` first.
- **`projects/controller_consumer.py`.** Drop `_handle_omniparser_status`, the
  `find_element_result` handler, and `controller_find_element`.
- **`projects/controller_protocol.py`.** Drop `FindElementActionEvent` and the registry rows.
- **`projects/controller_reply_tracker.py`.** Drop `send_find_element_result`.
- **`projects/consumers.py`, `AgentStatusConsumer`:**
  - It also joins `VISUAL_ENGINE_GROUP` (discarded on disconnect).
  - The initial state gains `visual_engine` and loses `agent_omniparser_status`.
  - New handler `visual_engine_status(event)` sends
    `{"type": "visual_engine", "visual_engine": {...}}`.
- **`machine_setup/consumers.py`.** Same change: join the group, put `visual_engine` in
  `current_state`, add the `visual_engine` push, and drop `agent_omniparser_status` from
  `agent_status`.
- **`machine_setup/services/send_message.py`.** `.only("id", "agent_connected")`, plus one
  `get_visual_engine_status()` call. `chat_page.py` changes the same way, and `SetupPage`
  gains `visual_engine: VisualEngineStatusPayload` for the template.
- **`projects/run_reopen_services._machine_blocker`.** Passes the engine status. New text
  for offline: "Start a vision worker."
- **`dashboard/services.py`.** Computes the engine status once per dashboard build and
  passes it to each `_build_project_card`.
- **`projects/views.py`:**
  - `project_environment` adds `visual_engine` (the payload) to the context.
  - `project_list` adds `connected_machine_state`.
  - `project_overview` and `setup_machine` add `visual_engine` where their template's
    Alpine needs it.

### Pages and forms

No new URL, form or HTTP view.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:environment` (existing) | GET | `@project_membership_required` | none | `projects/environment.html`, plus `visual_engine` (`VisualEngineStatusPayload`, via `json_script`) and a new include `projects/includes/_environment_visual_engine.html` (worker list card) | — |
| `projects:list` (existing) | GET | login | — | `projects/list.html`: `_project_table.html` uses `connected_machine_state` for connected rows | — |
| `projects:detail` overview, wizard `setup_machine`, `machine_setup:chat` (existing) | GET | membership | — | add `visual_engine` (json_script) and replace `omniparserStatus` in Alpine | — |

The templates and JS:

- **Machine-state logic.** Every Alpine component that derives `machineState` uses the same
  rule as `get_machine_state`:

  ```
  !agentConnected ? 'disconnected'
    : {ready:'ready', starting:'starting', failed:'failed', offline:'offline'}[visualEngine.state]
  ```

  Each component sets `visualEngine` from `json_script` and updates it on
  `type === 'visual_engine'` messages. The badge label for `offline` is "Visual engine
  offline". `partials/badge_machine.html` and `.badge-*` get an `offline` case, reusing the
  warning tone: no new CSS colour, only a class alias if needed.
- **`_environment_visual_engine.html` card.** Title "Visual engine", a state badge, and a
  table of workers: name, state badge, device, GPU, version, connected since. For an empty
  list it uses `partials/empty_state.html`: "No vision worker connected. See
  vision_worker/README.md."
- **XSS.** All worker-supplied strings (name, message, GPU name) are shown through
  `x-text` or auto-escaping, never `|safe` or `x-html`. Workers hold the shared token, but
  their strings are still treated as untrusted.
- **`advanced_diagnostics.html`.** The "Visual engine status" and "device" rows read from
  `visualEngine` (state plus `ready_count`/`connected_count`). The per-worker detail is in
  the new card.
- **`_environment_visual_engine_failure.html`.** Shown when `machineState` is `failed` or
  `offline`. The text says the vision workers, not the test machine.
- **`os_setup_tabs.html`, `runner_download_card.html`.** Remove "its first start also
  downloads the visual engine".
- **`partials/README.md`.** Update the documented scope: `visualEngine` replaces
  `omniparserStatus`.

### Background work and WebSockets

- **No new Celery task.** `parse_screenshot` runs inside the existing `execution` task, and
  the screenshot round trip adds no time-limit change: it is bounded by
  `VISION_WORKER_WAIT_SECONDS` + `VISION_PARSE_BUDGET_SECONDS`.
- **New WebSocket endpoint** `ws/vision-worker/`: worker ⇄ server, using the protocol above.
- **Channel-layer events:**
  - `vision.parse` (Celery → consumer, sent to the worker's channel name);
  - `vision_worker.superseded`;
  - `visual_engine.status` (group `visual_engine_status` → `AgentStatusConsumer` and
    `MachineSetupConsumer`);
  - replies to the per-request reply channel: `vision.parse_result` / `vision.error`.
- **Browser-facing messages:**
  - new `{"type": "visual_engine", "visual_engine": {...}}` on `ws/projects/<id>/agent-status/`
    and on `ws/projects/<id>/machine-setup/`;
  - `agent_status` and `current_state` lose `agent_omniparser_status` and gain
    `visual_engine` in the initial state.
- **Runner protocol.** `find_element` and `omniparser_status` are removed (0.6.0). See
  "Older runners (0.5.x)" above.

### Settings / infrastructure

Every value below is read in `auto_tester/settings.py` with `config(...)` and added to
`example.env` with comments.

| Setting | Default | Meaning |
|---|---|---|
| `VISION_WORKER_TOKEN` | `""` | Shared worker secret. When empty, every handshake is rejected and Daphne logs a WARNING at startup. Generate it with `python -c "import secrets;print(secrets.token_urlsafe(32))"`. |
| `VISION_PARSE_ATTEMPT_TIMEOUT` | 45 | Wait for one worker's reply, including its queue time. |
| `VISION_PARSE_BUDGET_SECONDS` | 90 | All attempts of one parse together. |
| `VISION_WORKER_WAIT_SECONDS` | 30 | Mid-case wait for any ready worker before the case blocks (Q6). |
| `VISION_WORKER_READY_TIMEOUT` | 600 | Wait at case start. It replaces `CONTROLLER_OMNIPARSER_READY_TIMEOUT`. |
| `VISION_WORKER_STALE_SECONDS` | 90 | A worker with no heartbeat for this long is ignored. Keep it at least 3 × the 30 s heartbeat. |

- **Removed:** `CONTROLLER_FIND_ELEMENT_TIMEOUT` and `CONTROLLER_OMNIPARSER_READY_TIMEOUT`.
  Leftover lines in `.env` are ignored.
- **Ordering comment in `example.env`.** Worst case per search is about
  90 s (screenshot) + 30 s (wait) + 90 s (parse). The typical case is about 2–6 s on a GPU.
  `SUB_AGENT_MIN_TIMEOUT_SECONDS` (150) should stay above the typical case. Replace the old
  `CONTROLLER_FIND_ELEMENT_TIMEOUT <` comments.
- **Other infrastructure:**
  - **`INSTALLED_APPS`** gains `"visual_engine"`.
  - **`setup.cfg`** (server):
    - `exclude = venv|migrations|controller_client|vision_worker`;
    - isort `known_first_party` adds `vision_worker` and `visual_engine`.
  - **`.pre-commit-config.yaml`:**
    - black and isort exclude `vision_worker/omniparser/util/` instead of the controller path;
    - the server mypy hook excludes `vision_worker/`;
    - new hook `mypy-vision-worker`:
      `vision_worker/.venv/bin/mypy --config-file vision_worker/setup.cfg`,
      `files: ^vision_worker/`, `exclude: ^vision_worker/omniparser/(util|weights)/`;
    - the controller hook's exclude is dropped.
  - **`.dockerignore` and `.gitignore`.** Add `vision_worker/.venv/`,
    `vision_worker/venv/`, `vision_worker/omniparser/weights/`, `vision_worker/.env`
    and `vision_worker/.mypy_cache/`, and remove the `controller_client/omniparser/weights/`
    lines. The server image keeps the `vision_worker/` source for the protocol import.
  - **`scripts/verify_server_image.sh`:**
    - also assert `python -c "from vision_worker.protocol import MessageType"`;
    - the `controller_client` omniparser files are expected to be *absent*;
    - assert `vision_worker/protocol.py` is present and
      `vision_worker/.venv` / `vision_worker/omniparser/weights` are not.
  - **`docker-compose.yml`** (server). Update the comment on the Daphne size flags: vision
    results and requests, not controller find_element results. The flags themselves are
    unchanged. Do not add the worker to the server compose: it runs on the GPU PC.
  - **nginx.** `/ws/` already proxies the new path. No change.
  - **CUDA wheel index.** The PyTorch index URL (for example `.../whl/cu128`) must be
    checked against the pytorch.org install selector for the pinned `torch~=2.10.0` at
    implementation time. The pip `--index-url`/`--extra-index-url` usage is documented in
    `vision_worker/README.md`. Keep `torch~=2.10.0` and `torchvision~=0.25.0` as the
    controller had them, which are known to work with the vendored code and Python 3.13.
- **`CLAUDE.md`.** Update the Architecture section (`controller_client` no longer runs
  OmniParser; a new `vision_worker` section; the `visual_engine` app; gating) and the
  Commands section (vision worker run, diagnose, mypy).
  `.claude/agents/reviewer.md` names the old vendored path. Tell the user so they can update
  it; it is agent configuration.

### Data flow

**Worker lifecycle.**

1. The worker opens a wss connection to `ws/vision-worker/` and sends a handshake (token,
   name, version, capabilities, GPU info).
2. The consumer validates it, runs `claim_worker`, supersedes any older socket with the same
   name, and acks `ok`. `broadcast_visual_engine_status` reports STARTING.
3. The worker preloads its model in a thread and sends `status` loading, then ready (or
   failed). After that it sends a `status` heartbeat every 30 s.
4. `record_worker_status` updates the row. When the state changes, the new status is
   broadcast to every open project page.
5. On close, `release_worker` runs and pending parses are failed with
   `WORKER_DISCONNECTED`. Then the UI is broadcast.

**Element search** (agent, `click`):

1. `verified_element_actions` → `parse_screen(project_id)` → `controller_find_elements`.
2. `controller_screenshot(project_id)`: the runner returns the full-resolution PNG from
   `execute_screenshot`. This is the same image and coordinate space the local
   `find_element` used.
3. `parse_screenshot(...)` picks a ready worker with the Redis counter (round robin) and
   `channel_layer.send`s a `vision.parse` event to that worker's consumer channel. The
   consumer forwards it as a `parse_request` frame.
4. On the worker, the request is queued (or answered `BUSY` or `NOT_READY`), parsed on the
   GPU, and the annotated image is bounded. The worker sends `parse_result`.
5. The consumer passes it to the request's reply channel. `parse_screenshot` validates the
   size, builds `PixelParseResult` and returns.
6. `parse_screen` publishes the annotated image as `controller_omniparser`. `match_element`
   and the click proceed unchanged.
7. **Failures:**
   - busy → the next worker;
   - disconnect, not ready or timeout → the next untried worker;
   - budget spent or no worker left → `VisualEngineError` → the case is Blocked
     `visual_engine`;
   - a runner screenshot timeout → `TestRunnerUnresponsiveError`, as today.

**Run start.**

- `get_run_readiness` reports "Visual engine ready" only when at least one worker is ready.
  This gate is blocking.
- `execute_test_run_test_case` does `wait_for_controller`, then `wait_for_ready_worker`,
  then `_safe_cleanup`, then the orchestrator.

## Alternatives considered

- **Protocol location.**
  - A new top-level `shared_protocol/` package imported by all three clients was
    considered. It is rejected because after this change the controller needs none of the
    vision types: only the server and the worker share them. `vision_worker/protocol.py`
    mirrors how `controller_client/protocol.py` is already shared, and adds no new
    packaging concept.
  - Duplicating the dataclasses in the server is rejected because the copies would drift.
- **Worker state storage.**
  - Redis-only state (hash plus TTL) was considered. DB rows were chosen instead because:
    - they match `Project.agent_channel_name` ownership;
    - they survive Redis flushes;
    - the UI and admin can read them;
    - writes are rare (connect, disconnect, state change, and a 30 s heartbeat `UPDATE`).
  - The round-robin cursor alone lives in Redis, because it must be atomic across Celery
    processes and is not worth a row lock.
- **Routing through the controller** (server → runner → worker) is rejected. It adds a hop
  and a dependency on the runner's uplink. The server already holds the screenshot.
- **Worker pulls jobs from a Celery queue or HTTP polling** is rejected. The user asked
  for an outbound WebSocket like the runner. A Celery worker on the GPU PC would also need
  direct Redis or DB access from a home network.
- **A group per worker pool with `group_send`** is rejected, because it would broadcast to
  every worker. Addressing the single chosen worker's channel name is exact.
- **Keeping `Project.agent_omniparser_status`** and filling it with the global state is
  rejected. The state is no longer per project, and duplicating it per row means a write
  fan-out on every worker change.
- **No abstraction added** for "client connection" shared between the controller and vision
  consumers. Their handshakes, ownership keys (project versus worker name) and lifecycles
  differ. A ~40-line `PendingParses` is cheaper than generalising `ReplyTracker`.

## Risks and mitigations

- **Screenshots leave the server (Q8).** Full desktop screenshots, which may show customer
  data, now go to the GPU PC and come back annotated. Mitigations:
  - the worker refuses `ws://` to a public host;
  - token authentication;
  - nothing is written to disk on the worker: images stay in memory and the logs carry
    sizes and timings only;
  - the server never logs image data.
- **Token exposure.** It is one shared secret (Q3). It is compared with
  `compare_digest`, never logged, and lives in the worker `.env`, which is `.gitignore`d and
  `.dockerignore`d. A leaked token lets an attacker register a worker and receive
  screenshots. Mitigations: rotate it in `.env`, which needs a Daphne restart; every worker
  name is visible on the Environment page; and a WARNING log line is written for each
  handshake.
- **No handshake throttling.** Each socket gets exactly one handshake attempt, and an
  unauthenticated socket is closed after 30 s. IP-based throttling would need
  `daphne --proxy-headers`, which is out of scope. The 256-bit random token makes brute
  force infeasible.
- **A malicious or buggy worker returns bad elements.** Clicks only go through after the
  vision-model verification in `verified_element_actions`. The image size check catches a
  wrong coordinate space. Worker strings are escaped in templates.
- **Latency.** Each search now needs a screenshot upload (runner → server) and an image
  upload (server → GPU PC) in place of one local step. Over a home uplink the result
  download (≤ 8 MiB) is the slowest part. Mitigations:
  - log `queue_seconds`, `ocr_seconds`, `som_seconds` and the round trip per parse;
  - if the result size hurts, lower `VISION_MAX_RESULT_BYTES` on the worker.
- **A dead consumer channel** (Daphne crash) stays "connected" in the DB. The heartbeat
  staleness filter drops it within 90 s. In between, a parse sent to it times out after
  `VISION_PARSE_ATTEMPT_TIMEOUT` and moves to the next worker.
- **Deploy order and the column drop:**
  1. Run `migrate`: `visual_engine.0001`, then `projects.0026`, which drops the column.
  2. Restart Daphne and every Celery worker together. Until they restart, old processes that
     still select `agent_omniparser_status` raise errors.

  The stack is a single-host compose, so the window is seconds. Rollback: revert the code,
  then `migrate projects 0025` (the column comes back empty), and runners report their
  status again on reconnect.
- **Runner compatibility.**
  - 0.5.x runners keep working against the new server.
  - 0.6.0 runners are rejected by an old server, so upgrade the server first.
  - Until a vision worker is running, the visual engine is OFFLINE and **no run can start**.
    The cut-over therefore needs the GPU worker up first.
- **Channel-layer payload size.** Screenshots of 1–15 MiB already pass through
  `channels_redis`, and there is no size limit there. Capacity (100 per channel) is not a
  concern, because each reply channel receives one message.
- **Queue on the worker.** `VISION_MAX_QUEUE=1` plus the `BUSY` answer keep the backlog per
  GPU at 2 or fewer. With 10 Celery execution slots and one GPU, bursts can spend the budget
  in busy-retry. If that is seen, raise the queue limit or add a GPU. It is logged.
- **pre-commit needs a `vision_worker/.venv`** with torch on every dev machine. That is the
  same cost the controller venv has today, and the controller venv becomes light.
- **N+1.**
  - `get_visual_engine_status()` is one query.
  - The dashboard and project list call it once per request, not per project.
  - `parse_screenshot` runs one selection query per call (plus one per busy retry round).

## Implementation steps

Shared code is written first, by the orchestrator. After it, parts 2–7 touch disjoint files
and can run as parallel sub-agents. Part 8 is the orchestrator's integration pass.

1. **Shared foundation** (orchestrator, before any sub-agent). Files:
   - `vision_worker/__init__.py` (empty), `vision_worker/protocol.py`,
     `vision_worker/exceptions.py`.
   - `git mv controller_client/omniparser vision_worker/omniparser`.
   - Copy `controller_client/omniparser_executor.py` to `vision_worker/engine.py` as the
     starting point. It is **copied, not moved**, so part 3 can delete the original.
   - `visual_engine/__init__.py`, `apps.py`, `models.py`, `types.py`, `exceptions.py`, and
     `migrations/0001_initial.py` (via `makemigrations visual_engine`).
   - `auto_tester/settings.py`: the app and the new and removed settings.
   - `example.env`.
   - Freeze these signatures for the sub-agents:
     - `get_visual_engine_status`, `broadcast_visual_engine_status`, `VISUAL_ENGINE_GROUP`;
     - `parse_screenshot`, `wait_for_ready_worker`;
     - `get_machine_state(project, visual_engine)`, `connected_machine_state`;
     - the `VisualEngineStatusPayload` keys;
     - the `{"type": "visual_engine"}` WebSocket message.
2. **Vision worker client** (sub-agent A). Files: everything under `vision_worker/` except
   `protocol.py`, `exceptions.py` and `omniparser/`:
   - `__main__.py`, `main.py`, `config.py`, `engine.py` (adapt), `client.py`,
     `diagnose.py`, `diagnostics.py`;
   - `requirements.txt`, `setup.cfg`, `example.env`;
   - `scripts/download_weights.sh`, `scripts/setup.sh`, `scripts/setup.ps1`;
   - `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `README.md`.

   It owns mypy-clean status under `vision_worker/setup.cfg`.
3. **Test Runner removal** (sub-agent B). Files:
   - delete `controller_client/omniparser_executor.py`, `omniparser_config.py`,
     `omniparser_diagnostics.py` and `scripts/download_omniparser_weights.sh`;
   - edit `protocol.py`, `client.py`, `exceptions.py`, `diagnose.py`, `diagnostics.py`
     (new), `browser_diagnostics.py`, `env_check.py`, `requirements.txt`, `example.env`,
     `setup.cfg` and `scripts/setup.{sh,ps1,bat}`.

   It owns controller mypy cleanliness.
4. **visual_engine services and consumer** (sub-agent C). Files:
   - `visual_engine/services/__init__.py`, `worker_auth.py`, `worker_connection.py`,
     `worker_status.py`, `pool_status.py`, `wait_for_ready_worker.py`,
     `parse_screenshot.py`;
   - `visual_engine/reply_routing.py`, `visual_engine/consumers.py`,
     `visual_engine/routing.py`, `visual_engine/admin.py`.
5. **projects / dashboard / machine_setup integration** (sub-agent D). Files:
   - `projects/services.py`, `controller_consumer.py`, `controller_protocol.py`,
     `controller_reply_tracker.py`, `consumers.py`, `views.py`, `run_reopen_services.py`;
   - `projects/models.py` plus `projects/migrations/0026_remove_project_agent_omniparser_status.py`;
   - `dashboard/services.py`;
   - `machine_setup/consumers.py`, `services/send_message.py`, `services/chat_page.py`.

   It consumes the frozen signatures from step 1.
6. **Templates, JS, CSS** (sub-agent E). Files:
   - `templates/projects/environment.html`, `overview.html`, `wizard/step_machine.html`;
   - `templates/projects/includes/_environment_visual_engine.html` (new),
     `_environment_visual_engine_failure.html`, `_project_table.html`;
   - `templates/machine_setup/chat.html`;
   - `templates/partials/advanced_diagnostics.html`, `badge_machine.html`, `README.md`,
     `os_setup_tabs.html`, `runner_download_card.html`;
   - `static/css/punk-hazard.css`, only if an `offline` badge class is needed.

   It consumes the context keys and the WebSocket message from step 1.
7. **Infrastructure config** (sub-agent F). Files: `.pre-commit-config.yaml`, `setup.cfg`,
   `.dockerignore`, `.gitignore`, `scripts/verify_server_image.sh`, `docker-compose.yml`
   (comment only).
8. **Integration** (orchestrator, not delegated).
   - Wire `auto_tester/asgi.py`: add `visual_engine.routing` and the `ws/vision-worker/` path.
   - Read the combined diff and resolve signature mismatches.
   - Run `git grep -n -i "omniparser_status\|find_element\|FIND_ELEMENT_LOCAL\|CONTROLLER_FIND_ELEMENT_TIMEOUT\|CONTROLLER_OMNIPARSER_READY_TIMEOUT"`;
     the only matches left should be history in `docs/` and the
     `controller_omniparser` tool name.
   - Run `python manage.py makemigrations --check` and `python manage.py check`.
   - Run `pre-commit run --all-files`, which covers all three mypy configs.
   - Update `CLAUDE.md`.

## Verification

No automated tests exist in this project. These manual checks need a dev stack
(`docker compose up db redis -d`, Daphne with the size flags, an execution worker) plus at
least one vision worker. A CPU worker on the dev Mac works for the logic checks.

1. **Static checks.** `pre-commit run --all-files` passes (server, controller and
   vision_worker mypy strict; black; isort). `manage.py check` and
   `makemigrations --check` are clean. `scripts/verify_server_image.sh` passes.
2. **Diagnose.** `python -m vision_worker.diagnose --image <a screenshot>` on the GPU PC
   reports cuda, the GPU name, a model load, one inference with its element count, and the
   result size.
3. **Handshake.**
   - With the right token, the worker connects. The Environment page shows it under
     "Visual engine" as Loading, then Ready, live without a reload, with device and GPU.
   - With a wrong token, the worker logs a rejection and exits. The server logs a WARNING
     without the token.
   - A socket that never sends a handshake is closed after 30 s.
   - A `ws://` URL to a public host is refused by the worker's config.
4. **Readiness gating.** With the runner connected and no worker, the run page checklist
   shows "Visual engine not ready" (offline text) and Start is refused. Resume, Re-run and
   the machine-setup composer are refused with the offline message. Once a worker is ready,
   all of them pass.
5. **One element search.** Run a case with one `click`.
   - The case log shows the parse.
   - The annotated `controller_omniparser` screenshot appears.
   - The click lands on the intended element: compare the element center with the runner
     screenshot.
   - Server logs show the worker name and the timings.
6. **Round robin.** Start two workers (two GPUs, or one GPU and one CPU worker with
   different names). Over 10 searches, the logs alternate between workers.
7. **Busy-skip.** Throttle one worker, for example with a temporary `time.sleep(20)` in
   `engine.parse` on that worker, and run two cases on two projects at once. Requests skip
   the busy worker and land on the free one, and the logs show `BUSY` replies.
8. **Disconnect mid-parse.** Kill one worker during a parse. The waiter gets
   `WORKER_DISCONNECTED` immediately, the request is retried on the other worker, and the
   case continues. With a single worker, the case blocks as `visual_engine` after
   `VISION_WORKER_WAIT_SECONDS`.
9. **Supersede.** Start a second process with the same `VISION_WORKER_NAME`. The first one
   exits on 4001, and the Environment page shows one row.
10. **Stale worker.** `kill -9` a worker without a clean close, or stop Daphne and restart
    it while the worker keeps backing off. Within about 90 s the worker no longer counts as
    ready, and reconnecting restores it.
11. **Runner compatibility.**
    - A 0.5.1 runner still connects to the new server, and its `omniparser_status` messages
      are ignored with no errors.
    - A fresh 0.6.0 ZIP installs without torch (setup is fast) and passes
      `python -m controller_client.diagnose` (screen capture) and `--browsers`.
12. **Docker on the GPU PC.** `docker compose up` in `vision_worker/` with the weights
    volume: `nvidia-smi` inside the container sees the GPU, and the worker reports device
    `cuda`.
13. **UI.** The project list, overview, wizard machine step, machine-setup chat and
    dashboard show "Visual engine offline", "starting" or "ready" consistently. Worker names
    containing `<script>` render as text.
14. **Agents.** Run `reviewer` and `verifier` in parallel, giving `verifier` the original
    request and this plan path.

## Changelog

- 2026-10-24: created (Draft; Q1–Q9 open).
- 2026-10-24: Q1–Q9 answered with the default assumptions; implementation started.
- 2026-10-24: review pass 1 fixes. Late worker replies are dropped by the consumer (they leaked in the Celery
  process's channel-layer buffer); the worker drops a queued request that cannot finish before its timeout at the
  last parse's pace; BUSY retries back off 1→8 s; tried workers are excluded by channel, and the dispatcher waits
  for a reconnecting worker within the budget; cancellation reaches `parse_screenshot`; the default worker name
  includes `CUDA_VISIBLE_DEVICES`; handshake names are capped at 100 chars and `system_info`/capabilities are
  allowlisted and length-capped; element centers outside the screenshot are dropped.
- 2026-10-24: rejected review findings:
  - Per-worker in-flight counters to avoid re-sending images to busy workers: this is option (c) of Q2, rejected
    because a killed Celery process leaks its lease. Exponential BUSY backoff bounds the re-sends instead.
  - Moving `VisualEngineError` into `visual_engine` and injecting the clock: `projects.exceptions` and
    `projects.controller_outage_clock` import nothing from `projects.services`, so there is no import cycle, and
    `VisualEngineError` belongs with the other case-blocking errors that `case_blocking` maps.
  - Broadcasting when a worker goes stale: a stale worker means its Daphne died, which also drops every browser
    socket on that Daphne; they reconnect and read fresh status. A network drop closes the socket via ping
    timeout and broadcasts normally.
  - `Any` for the channel layer: same as the existing controller code; `channels` ships no typed layer API.
  - Shared-token risks (name takeover, PARSE_FAILED from a rogue worker): accepted with Q3.
- 2026-10-24: Docker support removed at the user's request (too complicated). The worker is deployed exactly like
  the Test Runner: `scripts/setup.sh` / `setup.ps1` (the weights download is now inline in both), then
  `python -m vision_worker.main`. Removed `Dockerfile`, `.dockerignore`, `docker-compose.yml`,
  `scripts/download_weights.sh` and `__main__.py`. Several GPUs = one process per GPU with `CUDA_VISIBLE_DEVICES`.
