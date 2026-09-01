# OmniParser Client Integration — Debugging and Repair Plan

## Purpose

Diagnose and repair the regression introduced when OmniParser moved from the central server into `controller_client/`.

Current symptom: the controller/agent can no longer successfully use OmniParser-backed desktop actions such as `click`, `hover`, and `drag`.

Do not assume that "cannot connect to OmniParser" means an HTTP connection problem. There is no longer an OmniParser HTTP service. The expected path is now an existing controller WebSocket connection followed by local, in-process model inference.

## Required Outcome

At completion:

1. The production server image starts and can import the shared controller protocol.
2. The production server can generate a complete downloadable controller ZIP.
3. The server detects and rejects incompatible controller clients with a useful upgrade message.
4. A compatible controller reports whether local OmniParser is loading, ready, or failed.
5. The first real `find_element` request does not fail because model initialization exceeded an unrelated timeout.
6. Controller-side errors reach the agent immediately with their original diagnostic message.
7. One real controller performs screenshot capture, local OmniParser inference, response delivery, LLM element selection, and desktop action successfully.

## Constraints

- Diagnose before changing behavior. Capture the first failure at each boundary.
- Keep business logic in services; views and Celery tasks remain thin.
- Preserve strict typing.
- Do not restore the retired central OmniParser HTTP service.
- Do not add Docker/VNC/SSH execution paths.
- Do not use `pip freeze`. Update pinned-compatible requirements only if evidence shows a dependency problem.
- Use focused unit/integration tests. Leave the final real-machine end-to-end run to the user unless explicitly asked to run it.
- Keep the controller client's dependency environment separate from the Django server environment.

## Current Request Path

Trace this exact path when collecting evidence:

```text
agent desktop click/hover/drag
  -> agents.services.controller_element_finder.find_element_coordinates
  -> projects.services.controller_find_elements
  -> channel-layer event: controller.find_element
  -> projects.controller_consumer.ControllerConsumer.controller_find_element
  -> WebSocket message: find_element
  -> controller_client.client.ControllerClient._handle_find_element
  -> controller_client.omniparser_executor.execute_find_element
  -> local screenshot + local OmniParser model
  -> WebSocket message: find_element_result OR error
  -> projects.controller_reply_tracker.ReplyTracker
  -> projects.services.controller_find_elements
  -> vision-model element match
  -> controller click/hover/drag action
```

The message names and payload fields currently align across this path. Prioritize packaging, compatibility, readiness, timeout, and runtime failures before redesigning the protocol.

## Known High-Risk Findings

### H1 — Production Docker image excludes a server dependency

Evidence already present in the repository:

- `.dockerignore` excludes the entire `controller_client/` directory.
- `Dockerfile` uses `COPY . .`, so ignored files do not enter the image.
- `projects/controller_protocol.py` imports `MessageType` and `serialize_message` from `controller_client.protocol`.
- `projects.services.generate_controller_client_zip()` also expects the full `controller_client/` source tree to exist inside the running server.

Expected failure signatures:

- `ModuleNotFoundError: No module named 'controller_client'`
- ASGI/API container restart loop after a fresh image build
- controller download endpoint failing because `BASE_DIR/controller_client` is absent or incomplete

Confidence: very high for any fresh Docker build.

### H2 — Existing controllers can connect while lacking `find_element`

Evidence:

- `CLIENT_VERSION` is still `0.1.0`, unchanged since the original controller implementation.
- The client sends `client_version` during the handshake.
- The server ignores `client_version` and does not negotiate capabilities.
- A pre-migration controller can therefore authenticate but cannot deserialize or handle `find_element`.

Expected failure signatures:

- Client log: `Unknown message type: find_element` or failed deserialization
- Server log: controller action timeout
- UI still shows the controller as connected

Confidence: high when an already-installed controller is being reused.

### H3 — The controller is marked connected before OmniParser is usable

Evidence:

- The server marks `agent_connected=True` immediately after API-key authentication.
- OmniParser model construction is lazy and begins on the first `find_element` request.
- Setup pre-warms the OCR module download, but does not load the detector and Florence caption model.
- `controller_find_elements()` has a hard-coded 120-second timeout.
- The default sub-agent timeout is only 180 seconds.

Expected failure signatures:

- First `find_element` call times out; later attempts may behave differently
- Controller logs show model loading while the server reports a timeout
- CPU-only machines fail more consistently than GPU/MPS machines

Confidence: high for slow or first-run environments.

### H4 — Installation, weights, device, or screenshot failure

Expected failure signatures:

- Import error for `torch`, `transformers`, OCR libraries, or vendored `util`
- `OmniParser weights not found at ...`
- screenshot backend/display permission error
- device-specific model load or inference error

Confidence: environment-dependent.

### H5 — The result payload is too large

The controller returns a lossless annotated PNG as base64 plus every parsed element. A large/high-DPI desktop may produce a message too large for a WebSocket, ASGI server, reverse proxy, or Redis channel-layer boundary.

Expected failure signatures:

- Connection closes immediately after inference succeeds
- `MessageTooLarge`, WebSocket close code `1009`, Redis/channel-layer error, or missing reply with a successful controller inference log

Confidence: possible but currently unproven. Measure before changing encoding.

## Phase 1 — Capture the Exact Failure Boundary

### 1.1 Record runtime versions and deployment shape

Collect:

- Git commit used by the server.
- Whether the server runs locally or from the Docker image.
- Controller source commit or package date.
- Controller `CLIENT_VERSION`.
- OS, architecture, display resolution, Python version, and detected torch device.
- Whether the controller was freshly downloaded and set up after the OmniParser migration.

Do not continue with assumptions about server/client parity.

### 1.2 Capture correlated logs

Use one test request and correlate it by `request_id` across:

- execution Celery worker
- Daphne/ASGI API
- controller client

The client already logs incoming message type and request ID. Add temporary structured diagnostics only if current logging cannot correlate the request.

For the single request, establish the last successful checkpoint:

1. Agent invoked `click`, `hover`, or `drag`.
2. Server dispatched `controller.find_element`.
3. Controller received `find_element` with a handler.
4. Screenshot succeeded.
5. Weights paths validated.
6. Models loaded and device was reported.
7. OCR completed.
8. SOM/caption inference completed.
9. Controller serialized and sent `find_element_result`.
10. Server reply tracker routed the reply.
11. Agent received `PixelParseResult`.
12. Vision model chose an element.

Output: a short diagnosis identifying the first missing checkpoint and its raw error.

## Phase 2 — Prove or Eliminate the Docker Packaging Failure

### 2.1 Inspect a freshly built server image

Build the same image used in production, without relying on an older cached container, and verify inside it:

```bash
python -c "from projects.controller_protocol import ActionTypeRegistry; print('protocol import ok')"
test -f /src/controller_client/client.py
test -f /src/controller_client/scripts/setup.sh
```

Also exercise controller ZIP generation using the normal Django test environment and assert that the archive contains at least:

- `controller_client/client.py`
- `controller_client/protocol.py`
- `controller_client/omniparser_executor.py`
- `controller_client/omniparser/util/`
- all three platform setup scripts
- `controller_client/requirements.txt`
- generated `.env`

The archive must not contain:

- `.venv`
- downloaded weights
- caches
- tests
- a pre-existing controller `.env`

### 2.2 Apply the packaging repair if H1 is confirmed

Preferred minimal repair:

- Remove the blanket `controller_client/` exclusion from `.dockerignore`.
- Add granular exclusions for heavy/runtime artifacts such as:
  - `controller_client/.venv/`
  - `controller_client/omniparser/weights/`
  - controller caches and generated files
- Keep the tracked controller source and setup scripts in the server image because both server imports and ZIP generation require them.

Do not solve this by installing all controller dependencies into the server image. The server only needs the lightweight source files and shared protocol; the controller dependencies remain in the controller venv.

### 2.3 Add regression coverage

Add a test or build verification that fails if:

- the server image cannot import `projects.controller_protocol`, or
- controller ZIP source files are missing in the deployed image.

Gate: do not investigate model inference through a production image until this phase passes.

## Phase 3 — Enforce Controller Compatibility

### 3.1 Reproduce with a stale controller

Run a controller revision from before local OmniParser support against the current server. Confirm that it can currently authenticate and that `find_element` becomes an unknown message or timeout.

### 3.2 Add explicit capability negotiation

Do not rely only on a version string. Add a typed handshake capability list, for example:

```json
{
  "client_version": "0.2.0",
  "capabilities": [
    "find_element_local_v1",
    "interactive_commands_v1",
    "cleanup_environment_v1"
  ]
}
```

Server behavior:

- Parse and validate `client_version` and capabilities before marking the project connected.
- Reject a controller missing `find_element_local_v1` with a clear upgrade message.
- Include the handshake rejection message in the client-side exception/log; the current parsed handshake acknowledgement drops the human-readable message.
- Store or expose connected version/capabilities in project agent status so debugging does not require shell access.

Client behavior:

- Bump the controller version for this protocol change.
- Send typed capabilities.
- Display the server's full rejection reason.

### 3.3 Add compatibility tests

Cover:

- current client accepted
- stale client rejected before `agent_connected=True`
- missing local OmniParser capability rejected with upgrade guidance
- rejection reason displayed by the controller

Gate: the UI must not report a controller as usable when it lacks the required protocol.

## Phase 4 — Add OmniParser Readiness and Diagnostics

### 4.1 Create a controller-side diagnostic entrypoint

Add a command or service that checks, in order:

1. required imports
2. configured/default weights directory
3. expected detector file and caption model directory
4. screenshot permission and dimensions
5. selected device (`cuda`, `mps`, or `cpu`)
6. detector/caption model construction
7. one controlled inference
8. serialized result size

Return actionable errors. Never turn a local model error into a generic connection timeout.

### 4.2 Separate connection state from readiness state

Represent at least:

- `connected`
- `omniparser_loading`
- `omniparser_ready`
- `omniparser_failed`

Recommended flow:

1. Complete the basic controller handshake quickly.
2. Start full OmniParser initialization in a background thread/task.
3. Continue processing connection control messages while initialization runs.
4. Report readiness or the original failure to the server.
5. Prevent a test run that needs native desktop interaction from starting until readiness is confirmed.

Do not equate API-key authentication with model readiness.

### 4.3 Preload the full model

Expose a public, idempotent model-loading function rather than waiting for the first `find_element` call. The setup script may run a load-only smoke test, but runtime must still verify readiness because files and hardware can change after setup.

Ensure concurrent requests cannot trigger duplicate model loads. Preserve the existing singleton/lock intent and test it.

### 4.4 Improve failure reporting

Verify every exception path returns a correlated `error` message and that `ReplyTracker.send_error()` routes it to the waiting caller immediately.

Include:

- error code
- concise message
- failing readiness phase
- device
- resolved weights path

Do not send secrets or full environment dumps.

## Phase 5 — Correct Timeout Ownership

### 5.1 Measure instead of guessing

Record separately on CUDA, MPS, and CPU where available:

- cold model load time
- warm inference time
- total first `find_element` time
- serialized response size

### 5.2 Replace the hard-coded timeout

Add a controller/OmniParser-specific Django setting and `example.env` entry. The value must cover the supported slowest environment or use readiness to remove cold initialization from request latency.

Keep these relationships valid:

```text
warm find_element timeout
  < sub-agent timeout
  < test-case Celery soft time limit
  < test-case Celery hard time limit
```

Prefer readiness plus a reasonable warm-inference timeout over making every tool call wait several minutes.

### 5.3 Test timeout behavior

Cover:

- cold initialization is not charged to a normal element lookup after readiness
- warm inference completes within the configured timeout
- a real timeout returns a specific error
- a late reply cannot be mistaken for another request

## Phase 6 — Validate Transport Size and Encoding

### 6.1 Measure actual payloads

Log byte counts, not the image itself, for:

- screenshot bytes
- annotated image bytes
- base64 length
- element JSON length
- complete serialized WebSocket message

Test common and worst-supported display sizes, including high-DPI/4K if supported.

### 6.2 Repair only if limits are exceeded

Preferred order:

1. Downscale the image used for matching while preserving coordinate conversion to the original display.
2. Use a bounded-quality image format where acceptable.
3. Add explicit size validation with a useful error.
4. Consider chunking or external object storage only if simpler bounded messages are insufficient.

Verify Redis channel-layer and ASGI/WebSocket limits in the deployed versions before selecting a threshold.

## Phase 7 — Automated Verification

### Server tests

- `controller.find_element` maps to WebSocket `find_element`.
- Optional thresholds serialize and parse correctly.
- `find_element_result` routes through the reply tracker.
- Controller errors retain their message and request ID.
- Compatibility rejection occurs before connected state is stored.
- Controller ZIP contains all required source/setup files.
- Docker deployment smoke check imports the shared protocol.

### Controller tests

- protocol round trip for handshake capabilities and `find_element`
- missing dependency/weights diagnostic
- device detection
- singleton load behavior
- readiness state transitions
- screenshot failure
- cold-load failure propagation
- successful result serialization
- payload-size guard

### Cross-boundary integration test

Use a real Channels test communicator or equivalent boundary-level test to cover:

```text
server dispatch
  -> consumer WebSocket message
  -> simulated compatible controller response
  -> reply tracker
  -> controller_find_elements result
```

Mock the expensive model only at the controller executor boundary, not the WebSocket protocol boundary.

Run the scoped project commands documented in `.claude/CLAUDE.md`. Keep Django and controller tests/type checking in their respective environments.

## Phase 8 — User-Run End-to-End Verification

After automated checks pass, provide the user with exact setup/update steps for a fresh controller package.

The user verifies:

1. Download a new controller ZIP from the deployed server.
2. Run the platform setup script successfully.
3. Run the OmniParser diagnostic and observe `ready`.
4. Connect the controller and confirm version, capabilities, device, and readiness in the UI/logs.
5. Start a small test case with one native desktop click.
6. Confirm an annotated screenshot is persisted.
7. Confirm the correct element coordinates are selected and clicked.
8. Run a second lookup to confirm warm inference behavior.
9. Restart the controller and confirm readiness recovery.
10. Attempt a stale controller and confirm immediate, clear rejection.

## Acceptance Criteria

- A fresh production Docker build starts successfully.
- The server image contains the tracked controller source needed for imports and downloads, but no controller venv or model weights.
- A stale controller cannot appear healthy.
- A compatible controller exposes explicit OmniParser readiness.
- Missing weights and dependency failures are returned immediately and visibly.
- Cold initialization is handled outside normal lookup latency or within a deliberately configured readiness timeout.
- Warm `find_element` works reliably on the supported device classes.
- Response payloads remain below verified transport limits.
- All scoped tests and strict mypy checks pass.
- The user's real-machine desktop click test passes.

## Deliverables

Claude should return:

1. Root-cause report with logs and the first failed checkpoint.
2. List of confirmed and rejected hypotheses.
3. Focused code changes, separated by packaging, compatibility, readiness, timeout, and transport concerns.
4. Tests added and exact results.
5. Migration/update instructions for existing controller installations.
6. User-run end-to-end verification commands.
7. A corresponding implementation document under `docs/impl/` after the user confirms final testing.

