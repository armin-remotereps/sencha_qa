# fix: Test Runner recovers from a dropped connection and the run continues

Status: Done
Created: 2026-10-22
Request: If the Test Runner's (controller client's) WebSocket connection dies during a
test run for any reason, the runner should keep retrying until it reconnects, and the
run should continue on the new connection. Today the runner logs that it is
reconnecting and then stays stuck. To recover, the user has to stop the runner, press
Force disconnect in the panel, and start the runner again. (The user labelled this
"Feat"; it is filed as `fix` because it restores behaviour that is expected and broken.)

## Context

- **`controller_client/client.py`, `ControllerClient`:**
  - `run()` retries `_connect_and_listen()` at most `max_reconnect_attempts` times (default 10, `controller_client/config.py`), sleeping a fixed `reconnect_interval` (5s) between tries.
  - `AuthenticationError` stops the client permanently. `_handle_handshake_ack` raises it for any ack whose status is not `"ok"`, including `already_connected`.
  - Client keepalive is disabled (`ping_interval=None`, commit afb5ea2). Daphne pings the client instead (`--ping-interval 30 --ping-timeout 120`, `docker-compose.yml`).
  - `_message_loop` awaits each handler inline. While an `asyncio.to_thread` action runs, the loop does not reach the next `async for` step, so a closed socket goes unnoticed until the action returns (up to `AGENT_TIMEOUT_SECONDS` = 300s for `run_command` and interactive commands).
  - There is no timeout on the handshake ack.
- **`projects/controller_authenticator.py`:** `authenticate_handshake` calls `mark_agent_connected`. If that returns False it answers `already_connected` and the consumer closes the socket.
- **`projects/services.py`, connection state:**
  - `mark_agent_connected` (l.727) is a compare-and-set on `agent_connected=False`.
  - `mark_agent_disconnected` (l.747) is an unconditional full `project.save()` on the consumer's in-memory copy of the project, which may be stale.
  - `force_disconnect_controller` (l.814) sends `controller.force_disconnect`, clears the DB state and aborts the active run (commits e09602f, 6e70844).
- **`projects/services.py`, actions and runs:**
  - `_dispatch_controller_action` (l.905) and `controller_run_command_streaming` (l.1114) send to the `controller_<id>` group and wait on a fresh reply channel with a per-action timeout. Timeouts are 30s for most actions, `CONTROLLER_FIND_ELEMENT_TIMEOUT` = 120, and `AGENT_TIMEOUT_SECONDS` = 300 for `run_command`.
  - `execute_test_run_test_case` (l.2124) runs one pivot per Celery task. A run is a `chain` of these tasks on the `execution` queue (`start_test_run`, l.1800).
  - Before each pivot, `_wait_for_agent_connection` waits for the controller using `CONTROLLER_AGENT_CONNECT_TIMEOUT` (60s). On timeout it raises `TimeoutError`: the pivot is FAILED, the chain breaks, and the run stays STARTED.
  - `abort_test_run` (l.2301) cancels the run and revokes `celery_task_id` (the chain's last task) with `terminate=True`. Later pivot tasks already return early when the run is CANCELLED.
- **`projects/controller_consumer.py`:**
  - On `disconnect()`, `_cleanup_connection` marks the agent disconnected, broadcasts, and calls `abort_active_test_run_on_disconnect`. That last call is spec 010, which listed auto-reconnect as a non-goal.
  - `_forward_action` records each request's reply channel in a per-consumer `ReplyTracker` (`projects/controller_reply_tracker.py`). Nothing answers those pending requests when the socket drops.
- **Timers on the execution path**, all of which a 300s reconnect wait would hit today:
  - Per-action reply timeout in `_dispatch_controller_action` / `_receive_streaming_command_result`.
  - Agent loop: `agents/services/agent_loop.py`, `run_agent_loop`, uses `start_time = time.monotonic()`, checks `elapsed > config.timeout_seconds` every iteration, and sets `ToolContext.deadline = start_time + timeout_seconds`. This applies to every sub-agent; budgets range from `SUB_AGENT_MIN_TIMEOUT_SECONDS` = 150 to `SUB_AGENT_MAX_TIMEOUT_SECONDS` = 900. The orchestrator has no clock of its own.
  - Code that compares `ToolContext.deadline` to `time.monotonic()`:
    - `agents/services/tools_utility.py`, `_remaining_seconds_after_margin` (used by the `wait` tool);
    - `agents/services/verified_element_actions.py`, `_deadline_exhausted` (candidate budget for click / hover / drag).
  - Celery `execute_test_run_case` in `projects/tasks.py`: `soft_time_limit=1800`, `time_limit=1860` per test case.
- **Agents:** `agents/services/tool_utils.py`, `safe_tool_call`, turns every exception except `AgentCancelledError` into a tool error that the LLM sees. `_safe_cleanup` (`projects/services.py` l.1430) swallows everything.
- **Versions:**
  - Server: Django ~5.2, channels ~4.3.2, daphne ~4.2.1, channels-redis ~4.3.0. A dead channel's group membership survives a Daphne restart until `group_expiry` (24h by default).
  - Controller: websockets ~15.0, Python 3.13.

## Root cause

Confirmed from the code; (1)+(2) explain the report and the user's workaround exactly.

1. **Server rejects the reconnect as `already_connected` (confirmed, primary).**
   - Ownership is a bare boolean. Only the *old* consumer's `disconnect()` or Force disconnect ever clears it.
   - On a half-open link Daphne needs up to ~150s to notice the drop. After a Daphne restart or crash, `disconnect()` never runs, so the flag stays True forever.
   - In both cases the runner reconnects within ~5s, `mark_agent_connected` finds `agent_connected=True`, and the handshake is answered `already_connected` (`controller_authenticator.py` l.72-78).
   - Force disconnect was added to clear exactly this "zombie" state (6e70844), and pressing it is the user's workaround.
2. **The runner treats that rejection as fatal (confirmed).** Any non-`ok` ack raises `AuthenticationError`. `run()` then sets `_running = False` and never retries (`client.py` l.158-161, l.357-361).
3. **Retry budget (≈50s) is shorter than the server's detection window (≈150s) (confirmed).** A clean close also resets the counter and reconnects without sleeping.
4. **A drop aborts the run immediately (confirmed, spec 010).**
5. **A late `disconnect()` of the old socket would wipe a fresh connection (confirmed, latent).** `mark_agent_disconnected` is unconditional and full-`save()`s a stale copy, which can also revert fields edited meanwhile, such as a regenerated `api_key` or the application context.
6. **Contributing (suspected):**
   - **H1, no client keepalive.** A silent server-to-client failure leaves the runner waiting forever without logging anything.
   - **H2, inline handlers.** Reconnection waits for the in-flight action's thread to return, which can take up to 300s.
   - **H3, no handshake-ack timeout.**
   - **H4, exit can hang.** Process exit can block joining abandoned executor threads.
7. **Rejected:**
   - **Default thread-pool exhaustion.** The pool holds min(32, cpu+4) threads, there is at most one abandoned action per outage, and OmniParser loading is serialised by `_model_lock`.
   - **Unbounded connect.** websockets' default `open_timeout=10` already bounds `connect()`.

Q0 (which log line the user saw) is unknown, so the plan fixes both the "rejected" variant (1-5) and the "silent" variant (H1-H3).

## Goals

- **Runner retries forever by default.** Backoff is exponential, capped at 60s. It stops only on an invalid key, an incompatible client, Force disconnect (close code 4002), or being replaced by a newer runner (4001). Every await on the reconnect path has a timeout.
- **Newest authenticated connection wins.** It cleanly replaces a stale one. A late `disconnect()` of the old consumer never touches the fresh connection.
- **Runs survive an outage of up to `CONTROLLER_RECONNECT_GRACE_SECONDS` (default 300s):**
  - An action interrupted by the drop is **re-sent once** to the new connection after reconnect.
  - Later actions wait for the runner.
  - Only when the grace period expires is the run aborted, as today.
- **Reconnect wait time does not count against the agent's clocks.** This covers the sub-agent `timeout_seconds`, `ToolContext.deadline` and the per-action reply timeout. The Celery time limits get a matching allowance.
- **Manual Abort and Force disconnect keep working.** Force disconnect still aborts the run immediately, and the runner exits.

## Non-goals

- Making actions idempotent or deduplicating a re-sent action. **The user accepted that a non-idempotent action (click, typing, command, launch) may execute twice** when the drop happened after the runner had already performed it.
- More than one automatic re-send per action.
- A "waiting for Test Runner" banner on the run page. The machine badge already flips to disconnected via `agent.status`, and the server logs the wait.
- A liveness reaper for the badge after a Daphne restart when nobody reconnects (unchanged).
- Running controller handlers concurrently, or killing processes an abandoned action started.
- Fixing the pre-existing `abort_test_run` revoke-target quirk, beyond not self-revoking in the new path.

## Design

### Data model

- `Project.agent_channel_name = CharField(max_length=255, blank=True, default="")`. It holds the Channels `channel_name` of the consumer that owns the connection.
- Additive migration `projects/0017_project_agent_channel_name.py`.
- `agent_connected` stays as the readable flag. It is written together with the channel name, always via queryset `update()`.

### Outage clock (new, shared)

New pure-Python module `projects/controller_outage_clock.py`. It has no Django or ORM imports, and its state is process-local. That is correct for Celery prefork, where each test case runs in one process, one thread. The functions:

- `reset_outage_clock() -> None`: called at the start of every test case.
- `record_outage(seconds: float) -> None`: called by the reconnect wait with the time it actually spent waiting.
- `outage_seconds() -> float`: total reconnect wait time in the current test case.
- `agent_monotonic() -> float`: `time.monotonic() - outage_seconds()`. This is a monotonic clock that stands still while the server waits for the runner.

Every agent timer that compares against a start time or deadline switches from `time.monotonic()` to `agent_monotonic()`. That excludes the wait from all of them with one mechanism and no plumbing through call signatures. The places:

- `agent_loop.py`: `start_time`, `elapsed`, `deadline`.
- `tools_utility.py`: `_remaining_seconds_after_margin`. The real sleep in `_sleep_until_deadline` stays on `time.monotonic()`, because it is relative.
- `verified_element_actions.py`: `_deadline_exhausted`.

The per-action reply timeout needs no clock change. The wait happens between attempts, and each attempt gets a fresh full timeout.

### Services

**`projects/services.py`** (legacy module; the existing controller-agent and execution functions are changed in place).

Connection ownership:

- `claim_agent_connection(project: Project, channel_name: str, system_info: dict[str, Any], client_version: str, capabilities: Sequence[str]) -> str` replaces `mark_agent_connected`.
  - In `transaction.atomic()` with `select_for_update()`, it reads the previous `agent_channel_name` and unconditionally sets `agent_connected=True`, `agent_channel_name`, the system info / version / capabilities, and `agent_omniparser_status=_initial_omniparser_status()`.
  - It refreshes `project` and returns the previous channel name, or `""` if none or the same.
  - It never refuses.
- `release_agent_connection(project_id: int, channel_name: str) -> Project | None` replaces `mark_agent_disconnected`.
  - It is a conditional `filter(pk=project_id, agent_channel_name=channel_name).update(agent_connected=False, agent_channel_name="", agent_system_info={}, agent_client_version="", agent_capabilities=[], agent_omniparser_status={}, last_connected_at=now)`.
  - It returns the refreshed `Project` when a row changed (so the caller broadcasts), else `None`.
- `_clear_agent_connection(project_id: int) -> None`: the same update without the channel condition, used by Force disconnect.
- `force_disconnect_controller(project) -> bool` keeps its contract: clear the state, `group_send controller.force_disconnect`, broadcast, `abort_active_test_run_on_disconnect`.

Re-send on connection loss (owner: Part C):

- **`_dispatch_controller_action(project_id, event_type, reply_timeout, **payload)`** keeps its signature. Internally:
  1. **Attempt 1.** `owner = wait_for_controller(project_id)`, then dispatch with a new `request_id` and reply channel, then wait up to `reply_timeout`.
  2. **The connection counts as lost when:**
     - the reply is an `error.result` with `code == "CONTROLLER_DISCONNECTED"`; or
     - the wait times out **and** `controller_connection_lost(project_id, owner)` is True, which covers a Daphne restart where no error reply can come.
  3. **On loss after attempt 1:** log and call `on_resend` if the caller provided one (see streaming). Then `owner = wait_for_controller(project_id)`; this returns at once if the runner already reconnected, and otherwise waits within the grace period. Re-dispatch once with a **new `request_id` and a new reply channel**. A late reply to the old request can then never be read: the old reply channel is simply abandoned and expires in Redis.
  4. **On loss after attempt 2:** raise `ControllerActionError("Test Runner connection was lost again while running <event_type>; giving up on this action")`. The agent sees it as a tool error, as today.
  5. **A plain timeout with the same owner still connected** (a genuine slow action), and any other error reply, behave exactly as today. They are never re-sent.
- **`controller_run_command_streaming(project_id, command, on_output=None)`** gets the same two-attempt loop around `_dispatch_run_command_event` and `_receive_streaming_command_result`. Before attempt 2 it emits one marker line through `on_output`: `"[Test Runner reconnected - re-running command]"`, stream `stderr`. The marker exists because lines streamed by attempt 1 stay in the log.
- **Shared helper.** Implement both loops with one private helper, so there is a single place for the attempt and loss rules. For example, `_run_with_reconnect_retry(project_id, event_type, attempt: Callable[[], T]) -> T`, where `attempt` raises a private `_ConnectionLost(Exception)` when the loss rule matches.

Execution:

- **`execute_test_run_test_case`:**
  - calls `reset_outage_clock()` at the start;
  - replaces `_wait_for_agent_connection(...)` with `wait_for_controller(project.id)`;
  - gets a new `except ControllerReconnectTimeoutError` branch before `except Exception`. The branch calls `abort_test_run(pivot.test_run, revoke_task=False)`, writes "Test Runner did not reconnect within Ns; run aborted" into the in-progress pivot's `result` and log, and does not re-raise. The remaining chain tasks then early-exit on CANCELLED.
- **`abort_test_run(test_run, reason=..., *, revoke_task: bool = True)`.** Grace expiry passes False so the task does not SIGTERM itself.
- **`_wait_for_agent_connection` is deleted.** `_raise_for_omniparser_failure` no longer raises on `not agent_connected`; instead `_wait_for_omniparser_ready` calls `wait_for_controller(project.id)` when it sees the runner disconnected.
- **`_safe_cleanup` is unchanged.** After an abort there is no STARTED run, so `wait_for_controller` raises `ControllerNotConnectedError` at once and the error is swallowed with no wait.
- **`_generate_env_content`** writes `CONTROLLER_MAX_RECONNECT_ATTEMPTS=0` and `CONTROLLER_RECONNECT_MAX_INTERVAL=60`.

**`projects/controller_reconnect_services.py`** (new use case; follows the `projects/testrail_run_import_services.py` precedent, since a `projects/services/` package cannot coexist with `services.py`). It imports only models, `projects.exceptions`, `projects.controller_outage_clock`, `agents.types` and settings, so there is no cycle with `services.py`.

- `max_outage_seconds_per_case() -> int`: returns `2 * settings.CONTROLLER_RECONNECT_GRACE_SECONDS`. It is the total reconnect-wait allowance for one test case and is also used for the Celery limits.
- `wait_for_controller(project_id: int) -> str`, which returns the owning `agent_channel_name`:
  1. One query: `Project.objects.filter(pk=project_id, agent_connected=True).values_list("agent_channel_name", flat=True).first()`. If found, return it. This is the hot path, one indexed query per action.
  2. If the project has no STARTED `TestRun`, raise `ControllerNotConnectedError` (cleanup after an abort, management commands).
  3. Compute `budget = min(grace, max_outage_seconds_per_case() - outage_seconds())`. If `budget <= 0`, raise `ControllerReconnectTimeoutError`.
  4. Poll every 2s (constant moved here from `services.py`'s `_AGENT_POLL_INTERVAL_SECONDS`) until one of:
     - connected: return the channel name;
     - the run is no longer STARTED (Manual Abort): raise `AgentCancelledError`;
     - the budget elapses: raise `ControllerReconnectTimeoutError(grace_seconds)`.
  5. Always `record_outage(actual_waited)` in a `finally`. Log the start and end of the wait at INFO.
- `controller_connection_lost(project_id: int, owner_channel: str) -> bool`: True if the project is not connected or its `agent_channel_name != owner_channel`. One query, made only after a reply timeout.

**`projects/exceptions.py`** (new):

- `ControllerNotConnectedError(Exception)`.
- `ControllerReconnectTimeoutError(Exception)` with `grace_seconds: int`. It must not subclass `ControllerActionError`.

**`agents/services/tool_utils.py`:** `safe_tool_call` re-raises `ControllerReconnectTimeoutError`, just as it does `AgentCancelledError`.

### Pages and forms

No new pages, forms or URLs. Force disconnect keeps its URL, permission and messages. Its dialog text ("You can reconnect at any time by starting the Test Runner again") becomes accurate, because the runner now exits.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| (unchanged) `projects:force_disconnect` | POST | `@project_membership_required` | none | redirect to environment | "No test machine is connected." |

### Background work and WebSockets

**Shared protocol (`controller_client/protocol.py`):**

- `class CloseCode(IntEnum)`: `SUPERSEDED = 4001`, `FORCE_DISCONNECTED = 4002`.
- `TERMINAL_CLOSE_CODES: Final[frozenset[int]]`, containing both.
- `ErrorCode.CONTROLLER_DISCONNECTED = "CONTROLLER_DISCONNECTED"`.
- `CLIENT_VERSION = "0.3.0"`.
- **No new `ClientCapability` and no `REQUIRED_CLIENT_CAPABILITIES` change.** The handshake is unchanged and old runners stay accepted.

**Consumer (`projects/controller_consumer.py`):**

- **Handshake.**
  - `ControllerAuthenticator.authenticate_handshake(..., channel_name: str)` calls `claim_agent_connection`.
  - `AuthenticationResult` gains `previous_channel_name: str`.
  - `"already_connected"` is removed from `HandshakeStatus`.
- **`_establish_authenticated_connection`:**
  1. `group_add(self)`.
  2. If there is a previous channel: `group_discard(previous)`, which drops a dead channel left by a Daphne restart, then `channel_layer.send(previous, {"type": "controller.superseded"})`.
  3. Broadcast the status.
  4. Send the ack.
- **New `controller_superseded(event)`:** `close(code=CloseCode.SUPERSEDED)`.
- **`controller_force_disconnect`:** `close(code=CloseCode.FORCE_DISCONNECTED)`.
- **`_cleanup_connection`** (authenticated only):
  1. `await self._reply_tracker.fail_pending_replies(ErrorCode.CONTROLLER_DISCONNECTED, "Test Runner connection lost")`;
  2. `group_discard(self)`;
  3. `release_agent_connection(project.id, self.channel_name)`, broadcasting only if it returned a project;
  4. **no run abort.**
- **`ReplyTracker.fail_pending_replies(code: str, message: str) -> int`** (new) sends an `error.result` (`{"type": "error.result", "request_id", "code", "message", "details": ""}`) to every pending reply channel, clears the map, and returns the count. This message is the **B→C contract** that Part C's loss rule reads.

**Celery (`projects/tasks.py`):** `execute_test_run_case` keeps the queue and options. Its limits become `soft_time_limit=1800 + max_outage_seconds_per_case()` and `time_limit=1860 + max_outage_seconds_per_case()`; at the default grace that is 2400 / 2460, evaluated at import from settings. A test case can therefore spend up to 2 × grace waiting for the runner, in total across any number of outages, without losing agent time or hitting the Celery limit. Beyond that allowance, `wait_for_controller` raises `ControllerReconnectTimeoutError` and the run is aborted cleanly, before Celery's soft limit can fire. No new tasks or queues. A waiting run holds its `execution` slot.

**Controller client (`controller_client/`):**

- **`run()`.** Loop while `_running`:
  - `AuthenticationError` or the new `ConnectionTerminatedError` (in `controller_client/exceptions.py`; carries `code`): log and stop.
  - Anything else, including a normal return: sleep `min(reconnect_interval * 2**(attempt-1), reconnect_max_interval)` (5, 10, 20, 40, 60, …).
  - Reset `attempt` only after a session whose handshake succeeded.
  - Give up only if `max_reconnect_attempts > 0` and it is exceeded.
  - Log each attempt with its delay.
- **`_connect_and_listen()`:**
  - `websockets.connect(url, max_size=..., open_timeout=15, close_timeout=10, ping_interval=30, ping_timeout=120)`. Keepalive comes back with Daphne's tolerance, so a half-open link is detected in ≤150s. Rewrite the comment about the 20s default.
  - Explicit handshake phase: send the handshake, then `asyncio.wait_for(connection.recv(), HANDSHAKE_ACK_TIMEOUT_SECONDS=30)`, parse it as `handshake_ack`.
  - Ack `ok`: start the OmniParser preload.
  - Ack `error` or `incompatible`: raise `AuthenticationError` (terminal).
  - Any other status, a non-ack message, or a timeout: retryable.
  - Remove `HANDSHAKE_ACK` from `_handlers` and the unused `_handshake_event`.
  - When the connection ends, if `connection.close_code in TERMINAL_CLOSE_CODES`, raise `ConnectionTerminatedError`, logging "replaced by another Test Runner" or "disconnected from the panel".
- **`_message_loop`.**
  - Run each `_dispatch(...)` as a task and wait on it together with `connection.wait_closed()` (`FIRST_COMPLETED`).
  - If the socket closes first, cancel the handler task and leave. The thread finishes in the background and its result is dropped.
  - Processing stays sequential.
- **`_handle_run_command._on_output`.** Capture the connection at handler start and skip sends once `self._connection` is a different object.
- **`config.py`.**
  - `max_reconnect_attempts` defaults to 0 (unlimited).
  - New `reconnect_max_interval: int`: env `CONTROLLER_RECONNECT_MAX_INTERVAL`, default 60, CLI `--reconnect-max-interval`.
  - Use `is not None` for CLI overrides.
- **`main.py`:** call `client.stop()` after `run()` returns, for cleanup before exit (H4).
- **`example.env`:** `CONTROLLER_MAX_RECONNECT_ATTEMPTS=0` (0 = retry forever) and `CONTROLLER_RECONNECT_MAX_INTERVAL=60`.

**Compatibility with deployed 0.2.0 runners:**

- Their handshake is unchanged and their reconnects are now accepted (takeover).
- They still give up after 10 × 5s.
- They treat 4001/4002 as ordinary closes and reconnect. So after Force disconnect they come back, as today, and two old runners on one key keep replacing each other.
- Unlimited retry needs a fresh ZIP or `.env` edit.

### Settings / infrastructure

- `auto_tester/settings.py`: add `CONTROLLER_RECONNECT_GRACE_SECONDS: int = config("CONTROLLER_RECONNECT_GRACE_SECONDS", default=300, cast=int)` and remove `CONTROLLER_AGENT_CONNECT_TIMEOUT`.
- `example.env`: replace `CONTROLLER_AGENT_CONNECT_TIMEOUT=60` with `CONTROLLER_RECONNECT_GRACE_SECONDS=300`, plus a comment: how long a running test waits for the Test Runner to reconnect. Each test case may wait at most twice this in total; the wait does not consume agent time.
- `docker-compose.yml` is unchanged.

### Data flow

**Outage mid-action:**

1. The socket drops. The runner detects it through a TCP error, a close frame, or keepalive (≤150s), cancels the in-flight handler, and backs off.
2. The old consumer's `disconnect()` runs, either when Daphne notices or immediately on takeover:
   - pending requests receive `error.result CONTROLLER_DISCONNECTED`;
   - `release_agent_connection` sets `agent_connected=False`, and the badge broadcast follows.
3. The waiting Celery task (`_dispatch_controller_action`) sees the loss and calls `wait_for_controller`, which polls. The outage clock records the wait, so the agent and sub-agent clocks stand still.
4. The runner reconnects:
   - `claim_agent_connection` runs, then `group_discard` of the old channel, then `controller.superseded` to it (it closes with 4001 if still alive; its late `disconnect()` releases nothing);
   - the ack is sent.
5. `wait_for_controller` returns the new channel. The same action is re-sent once with a new `request_id`, and the agent gets its normal result.

**Daphne restart:** no error reply arrives. The action's reply timeout fires, `controller_connection_lost` sees a different owner or "disconnected", and the wait-and-resend path runs.

**Idle drop:** the drop happens while no action is in flight, for example during an LLM call. The next action's precheck waits, then dispatches.

**Grace or per-case allowance expires:**

1. `ControllerReconnectTimeoutError` is raised.
2. It passes through `safe_tool_call` up to `execute_test_run_test_case`.
3. `abort_test_run(revoke_task=False)` runs, the pivot result gets the reason, and the remaining tasks early-exit.
4. Cleanup does not wait.

**Manual Abort during a wait:** the next poll raises `AgentCancelledError`, and the existing cancelled path runs.

**Force disconnect:**

1. The DB is cleared and the run aborted at once.
2. The consumer closes with 4002 and the runner exits.
3. The old consumer's `disconnect()` releases nothing.

## Alternatives considered

- **Reject newcomers while the old socket looks alive.** This needs a liveness probe and still fails after a Daphne restart. The user chose newest-wins.
- **Reset `agent_connected` at server startup.** It covers only the restart case.
- **Enforce the grace with a scheduled Celery task on disconnect.** That adds a task and races with reconnect and abort. The execution task is already blocked on the controller, so enforcing the grace there is simpler.
- **Re-send from the consumer**, buffering the action and replaying it on the new consumer. The consumer is per-socket and the new one is a different instance, possibly in another process. Replaying needs shared state. The Celery waiter already holds the full payload.
- **Pass "paused seconds" through `ToolContext` / `AgentConfig`, or extend deadlines in place.** `deadline` is a frozen value copied into many call sites (`tool_registry.py`, `tools_controller.py`, `verified_element_actions.py`). A process-local clock read in the three comparison points is fewer edits and impossible to forget at new call sites that use `agent_monotonic()`.
- **Raise the Celery limits by an unbounded or large constant.** A bounded per-case allowance (2 × grace), enforced by `wait_for_controller`, keeps the hard limit meaningful and guarantees a clean abort instead of a SIGKILL.
- **websockets' reconnecting iterator.** Its retry classification does not map onto the ack, auth and close-code rules.
- **Two settings for "wait for the runner"** (keep `CONTROLLER_AGENT_CONNECT_TIMEOUT`). Replaced by one setting, as the user agreed.

## Risks and mitigations

- **Double execution (accepted by the user).** A re-sent click, typing, `run_command` or `launch_app` runs twice if the runner had already done it before the drop. This is limited to one re-send per action. The `run_command` marker line makes it visible in the log.
- **The abandoned runner thread may overlap the re-sent action.** Both run in the same runner process: after reconnect the old thread may still be executing when the re-send arrives. This is bounded by the original action's duration.
- **Interactive sessions survive the reconnect.** They live in the runner process, so `send_input` / `wait_for_command` re-sends act on the same session. A re-sent `start_interactive_cmd` creates a second session.
- **Process-local outage clock.** It is correct only while one test case runs per process on one thread (Celery prefork, as configured). The module docstring must state this. Threaded execution would need the clock passed explicitly. `tools_search` threads do not touch the controller.
- **Celery limits grow.** With grace 300 the limit is 2400s, so a hung test case is killed up to 10 minutes later than today.
- **Worker capacity.** A waiting run holds one of the 10 `execution` slots for up to 2 × grace per case.
- **Daphne-restart re-sends cost one full reply timeout first** (30s typical, 300s for `run_command`). That time is not excluded from the agent clock. It is acceptable because restarts are rare.
- **Takeover lets any key holder kick the current runner.** This is the same trust as today. Regenerating the key still blocks new connections.
- **Client keepalive returns** (afb5ea2 disabled it). 120s matches what Daphne already tolerates.
- **Migration.** Additive, small table.
- **Env var removal.** `CONTROLLER_AGENT_CONNECT_TIMEOUT` becomes unused. Call it out in `docs/changed/`.
- **Spec 010 behaviour change.** A disconnect no longer aborts immediately; Force disconnect still does.

## Implementation steps

**Step 1: shared code** (orchestrator, before sub-agents):

- `controller_client/protocol.py`: `CloseCode`, `TERMINAL_CLOSE_CODES`, `ErrorCode.CONTROLLER_DISCONNECTED`, `CLIENT_VERSION="0.3.0"`.
- `projects/exceptions.py`: `ControllerNotConnectedError`, `ControllerReconnectTimeoutError`.
- `projects/controller_outage_clock.py` (new): `reset_outage_clock`, `record_outage`, `outage_seconds`, `agent_monotonic`.
- `projects/models.py` + `projects/migrations/0017_project_agent_channel_name.py`.
- `auto_tester/settings.py`, `example.env`: `CONTROLLER_RECONNECT_GRACE_SECONDS=300`; remove `CONTROLLER_AGENT_CONNECT_TIMEOUT`.

Then four parts in parallel, with no file overlap:

**Step 2, Part A: Test Runner client.**

- `controller_client/client.py`: `run`, `_connect_and_listen`, a new handshake-phase helper, `_message_loop`, `_handle_run_command`. Remove the `HANDSHAKE_ACK` handler and `_handshake_event`.
- `controller_client/config.py`: `ClientConfig.reconnect_max_interval`, defaults, `is not None` overrides.
- `controller_client/exceptions.py`: `ConnectionTerminatedError`.
- `controller_client/main.py`.
- `controller_client/example.env`.
- Consumes: `CloseCode`, `TERMINAL_CLOSE_CODES`.

**Step 3, Part B: consumer side of connection ownership.**

- `projects/controller_consumer.py`: `_handle_handshake`, `_establish_authenticated_connection`, `_cleanup_connection`, `controller_force_disconnect`, new `controller_superseded`.
- `projects/controller_authenticator.py`: `authenticate_handshake(..., channel_name)`, `AuthenticationResult.previous_channel_name`, `HandshakeStatus` without `already_connected`.
- `projects/controller_reply_tracker.py`: `fail_pending_replies`.
- **B→C contract it produces:** an `error.result` with `code="CONTROLLER_DISCONNECTED"` for every pending request on disconnect.
- **C→B functions it consumes:**
  - `claim_agent_connection(project, channel_name, system_info, client_version, capabilities) -> str`;
  - `release_agent_connection(project_id, channel_name) -> Project | None`;
  - `broadcast_agent_status`.

**Step 4, Part C: server services, re-send and run survival.**

- `projects/services.py`:
  - `claim_agent_connection`, `release_agent_connection`, `_clear_agent_connection` (replacing `mark_agent_connected` / `mark_agent_disconnected`);
  - `force_disconnect_controller`;
  - `abort_test_run(revoke_task=)`;
  - `_dispatch_controller_action` and `controller_run_command_streaming` with the re-send helper (`_run_with_reconnect_retry`, `_ConnectionLost`);
  - delete `_wait_for_agent_connection`;
  - `_wait_for_omniparser_ready` / `_raise_for_omniparser_failure`;
  - `execute_test_run_test_case` (`reset_outage_clock`, `wait_for_controller`, the new except branch);
  - `_generate_env_content`.
- `projects/controller_reconnect_services.py` (new): `wait_for_controller`, `controller_connection_lost`, `max_outage_seconds_per_case`.
- `projects/tasks.py`: `execute_test_run_case` time limits.
- Owns the re-send logic. Exposes the two connection functions with exactly Part B's signatures.

**Step 5, Part D: agent clocks.**

- `agents/services/agent_loop.py` (`run_agent_loop`: `start_time`, `elapsed`, `deadline` via `agent_monotonic()`).
- `agents/services/tools_utility.py` (`_remaining_seconds_after_margin`).
- `agents/services/verified_element_actions.py` (`_deadline_exhausted`).
- `agents/services/tool_utils.py` (`safe_tool_call` re-raises `ControllerReconnectTimeoutError`).
- Consumes: `agent_monotonic`, `ControllerReconnectTimeoutError`.

**Step 6: integration** (orchestrator):

- Grep `projects/`, `agents/`, `dashboard/`, `templates/`, `controller_client/` and `example.env` for leftover `mark_agent_connected`, `mark_agent_disconnected`, `already_connected`, `_wait_for_agent_connection`, `CONTROLLER_AGENT_CONNECT_TIMEOUT`.
- Check that no agent deadline comparison still uses `time.monotonic()` against `ToolContext.deadline`.
- Run `python manage.py makemigrations --check` and `pre-commit run --all-files`.

## Verification

There is no automated test suite; prove the fix with manual smoke checks:

1. **Short outage during an action.** Start a run. While a `run_command` (for example `sleep 20`) or a click is in flight, disable networking on the runner machine for ~20s.
   - The runner backs off and reconnects.
   - The server log shows "connection lost, waiting" then "re-sending <event> (attempt 2)".
   - The run log shows the re-run marker line for `run_command`.
   - The action's normal result reaches the agent, and the run finishes in a non-CANCELLED status.
2. **Clocks paused.** Set a sub-task budget of about 150s. Cause a 200s outage mid-sub-task (with grace 300).
   - The sub-agent does **not** report TIMEOUT after reconnect.
   - The `wait` tool's remaining budget is unchanged across the outage.
   - The Celery task is not soft-killed.
3. **Daphne restart mid-action** (`docker compose restart server`).
   - The runner is accepted on reconnect: no `already_connected`, no Force disconnect needed.
   - After the reply timeout the action is re-sent once and the run continues.
4. **Half-open link** (Linux runner: `iptables` DROP rules for port 8000 in both directions, removed after).
   - The runner logs a keepalive timeout within ~150s.
   - `Project.agent_channel_name` equals the new consumer's channel after the reconnect.
   - The old consumer's late cleanup changes nothing.
5. **Second drop during the re-sent action.** The action fails to the agent with "lost again", and the run continues.
6. **Outage longer than grace.** Set grace to 30 and stop the runner for 60s.
   - The run is CANCELLED, the case result names the reason, and the remaining cases are CANCELLED.
   - Cleanup does not wait. The runner connects normally after restart.
7. **Per-case allowance.** With grace 30, cause three ~25s outages in one case. The third wait aborts the run once 60s total is used.
8. **Manual Abort during a wait.** The run is CANCELLED within ~2s.
9. **Force disconnect.** The run aborts immediately and the runner logs "disconnected from the panel" and exits.
10. **Two runners, same key.** The newer one connects; the older one logs "replaced" and exits.
11. **Invalid key / incompatible client.** The runner exits on the first ack.
12. **Server accepts but never acks** (scratch branch). The runner retries after 30s.
13. **Old 0.2.0 runner.** It connects, and a reconnect after a drop is accepted.
14. **Pre-commit and migrations.** `pre-commit run --all-files` is clean; `makemigrations --check` reports no changes.
15. **Review agents.** Run `reviewer` and `verifier` in parallel, giving the verifier the original request and this plan path.

## Open questions

None open. Resolved by the user on 2026-10-22:

- **Q0 (which log line):** unknown. The plan covers both the "rejected" and the "silent" variants.
- **Q1:** grace is 300s (`CONTROLLER_RECONNECT_GRACE_SECONDS`), and the wait must not trip the agent, sub-agent, reply or Celery timers. Handled by the outage clock, the per-attempt reply timeout and the Celery allowance.
- **Q2:** auto re-send the interrupted action once after reconnect, with a new `request_id`; a late old reply is ignored. A second loss or grace expiry fails as today. Double execution of non-idempotent actions is accepted.
- **Q3:** the runner exits on Force disconnect (4002 is terminal).
- **Q4:** newest connection wins; the replaced runner exits (4001).
- **Q5:** retry forever by default, backoff capped at 60s, and new ZIPs write `CONTROLLER_MAX_RECONNECT_ATTEMPTS=0`.
- **Q6:** `CONTROLLER_AGENT_CONNECT_TIMEOUT` is replaced by `CONTROLLER_RECONNECT_GRACE_SECONDS`.

## Changelog

- 2026-10-22: created (Draft).
- 2026-10-22: user answers applied.
  - Grace raised to 300s. Added the outage clock (`projects/controller_outage_clock.py`, `agent_monotonic`) so agent and sub-agent deadlines exclude reconnect waits, plus the per-case allowance (2 × grace) added to the Celery limits.
  - In-flight actions are now re-sent once after reconnect instead of being reported failed. Loss is detected by a `CONTROLLER_DISCONNECTED` reply, or by a reply timeout with an owner change. Double execution is accepted.
  - Added Part D (agent clocks).
  - `wait_for_controller` now returns the owner channel.
  - Status set to Ready.
- 2026-10-22: review round 1 fixes.
  - Consumer `_cleanup_connection` now discards the group and releases ownership *before* failing pending replies; `wait_for_controller(project_id, lost_owner=)` never returns the channel the action was just lost on; `force_disconnect_controller` clears ownership before sending the close event. (Re-send could otherwise go to the dead channel.)
  - Authenticator split: `authenticate_handshake` only validates; the consumer does `group_add` and then `ControllerAuthenticator.claim_connection`, so DB ownership is visible only once the consumer is in the group.
  - `_safe_cleanup(project_id, *, wait_for_runner)`: before a case it propagates `ControllerReconnectTimeoutError` / `AgentCancelledError`; after a case it skips when no runner is connected, so cleanup never spends grace twice.
  - `execute_test_run_test_case` treats `ControllerNotConnectedError` like a cancel (it only occurs once the run is no longer STARTED).
  - `_disconnected_agent_fields` returns a `TypedDict` instead of `dict[str, Any]`.
  - Accepted, not fixed:
    - Daphne restart with a runner whose backoff outlasts the reply timeout: the DB still names the dead channel, so the timeout looks like a slow action and is not re-sent. Distinguishing the two needs a liveness probe (non-goal); the next action after the takeover works normally.
    - Between the new consumer's `group_add` and the old channel's `group_discard`, an action can reach both runners (one more possible double execution, already accepted by the user).
    - `test_controller_*` management commands (manual dev tools) print a traceback on `ControllerNotConnectedError` instead of a handled error.
    - `layer: Any` in `_send_controller_action_once` follows the existing pattern for the untyped channel layer.
- 2026-10-22: review round 2 fixes.
  - A `CONTROLLER_DISCONNECTED` reply only counts as a lost connection when `controller_connection_lost(project_id, owner)` agrees (`_is_stray_disconnect_reply`); otherwise it came from a superseded consumer that also got the action, and the waiter keeps reading for the owner's reply. Prevents a healthy owner being passed as `lost_owner` and the run aborting after grace.
  - `_safe_cleanup` always propagates `ControllerReconnectTimeoutError`; the after-case call in `execute_test_run_test_case`'s `finally` aborts the run (without touching the finished case) instead of letting the next case wait again. Before a case it also propagates `ControllerNotConnectedError`.
- 2026-10-22: review round 2 (verifier) fixes.
  - After-case cleanup is dispatched with `resend_on_reconnect=False` (`_dispatch_controller_action` / `_run_with_reconnect_retry(resend=)`): a drop during it fails the cleanup at once instead of spending grace; the next case's pre-run cleanup still runs.
  - Accepted: an action dispatched in the ms window between the old consumer's `group_discard` and `release_agent_connection` lands in an empty group and is re-sent only after its reply timeout (same class as the accepted Daphne-restart case).
  - Accepted: two 0.2.0 runners on one key keep replacing each other until their 10 attempts run out; a 0.2.0 runner beats a 0.3.0 one (the 0.3.0 exits on 4001). Called out in `docs/changed/`.
- 2026-10-22: review round 3 nits.
  - `_run_with_reconnect_retry(resend=False)` looks the owner up with `connected_owner` (never waits) and raises `ControllerNotConnectedError` when none; after-case cleanup therefore never waits, so `_is_agent_connected` and the `finally` abort branch were removed.
  - `on_resend` / `resend` are keyword-only.
  - Noted (accepted double-execution class): during a takeover a `run_command` reaching both runners interleaves their output lines; the first `command.result` wins.
  - Accepted: `wait_for_controller` polls for *any* STARTED run of the project. If a run is aborted and another started within one 2s poll while the runner is down, the old run's waiter may send one stray action after reconnect before its agent cancellation check stops it.
