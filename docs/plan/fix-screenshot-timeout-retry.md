# fix: Retry desktop screenshot reply timeouts before blocking the case

Status: Done
Created: 2026-10-23
Request: A desktop screenshot sometimes gets no reply in time, because of a slow network or a busy test machine. Today the case is Blocked (`runner_unresponsive`) on the first timeout, even though the runner is fine. Retry the screenshot, with 3 attempts in total. Only when all 3 time out should the error propagate and the case be Blocked.

## Context
- `projects/services.py:controller_screenshot` dispatches `controller.screenshot` with `runner_local=True` and a 30s timeout. The path is `_dispatch_controller_action` -> `_run_with_reconnect_retry` -> `_send_controller_action_once`.
- `_raise_reply_timeout` handles a timeout in one of two ways:
  - The owner changed: it raises `_ConnectionLost`, which takes the existing reconnect/resend path.
  - The owner is unchanged: it raises `TestRunnerUnresponsiveError`, an `InfrastructureError`.
- `safe_tool_call` re-raises `InfrastructureError`. `projects/case_blocking.py` then maps the error to `BlockedReason.RUNNER_UNRESPONSIVE`. `blocked_message` sends the exception text to the UI and to TestRail (docs/changed/2026-10-22-blocked-result.md).
- Callers, all of which go through `controller_screenshot`:
  - `agents/services/tools_controller.py:take_screenshot`
  - `agents/services/verified_element_actions.py:take_before_screenshot` and `confirm_action`
  - `projects/management/commands/test_controller_actions.py`
- Each attempt gets a fresh `request_id` and reply channel. A late reply to an abandoned request is never read.
- `controller_client/client.py` handles each incoming message in its own `asyncio.create_task`, and the capture runs in `asyncio.to_thread(execute_screenshot)`. A second request does not queue behind a slow first one, so a retry gets its own full window.
- Abort calls `abort_test_run`, which revokes the Celery task with `terminate=True`. That kills the worker process, so a retry loop does not delay Abort. Force-disconnect and connection loss change the owner and take the `_ConnectionLost` path, not this one.

## Root cause
- **Confirmed:** a single reply timeout on a runner-local, idempotent action escalates straight to Blocked. There is no transient-fault tolerance. The multi-MiB PNG crosses the runner -> nginx -> Daphne -> Redis -> worker path. A slow uplink, or a runner busy with in-process OmniParser inference (CPU/GPU contention), can push one reply past 30s while the runner is still healthy.
- **Suspected, not verified:** most of the reported cases are network or load spikes, not a dead runner. The server log would confirm this: the case is Blocked with `Timed out waiting for controller reply to controller.screenshot after 30.0s`, and the runner keeps answering later actions.
- **About the "AI model overload" wording:** the vision-model call happens after the screenshot has arrived, and its failures raise `AIServiceError`, not a screenshot timeout. Load on the runner's local OmniParser can slow a screenshot. Load on the LLM provider cannot.

## Goals
- `controller_screenshot` makes up to 3 attempts when it gets `TestRunnerUnresponsiveError`. The case is Blocked only after the third timeout.
- Each retry is logged at WARNING. The final error says how many attempts were made.

## Non-goals
- No retries for other runner-local actions. click, double_click, hover, drag, type_text and key_press are not idempotent, and a duplicate would act twice. find_element already has a 120s timeout. cleanup and close_* are out of scope too.
- `controller_browser_take_screenshot` is not `runner_local`. Its timeout is already a plain `ControllerActionError`, which the agent sees as a tool error and which does not block the case. No change.
- No LLM (`AIServiceError`) retries, and no change to runner-side `EXECUTION_FAILED` replies.
- No protocol change and no controller change.

## Design
### Data model
None.

### Services
`projects/services.py` only:
- Add a module constant `_SCREENSHOT_ATTEMPTS: Final = 3`, next to `controller_screenshot`.
- `controller_screenshot` keeps its signature. It loops up to `_SCREENSHOT_ATTEMPTS` times around the existing `_dispatch_controller_action(..., runner_local=True)` call:
  - **Catches** only `TestRunnerUnresponsiveError`. Every other exception propagates unchanged, including `SoftTimeLimitExceeded`, `ControllerActionError` from `_build_screenshot_result`, `ControllerReconnectTimeoutError` and `TestRunnerDisconnectedError`.
  - **Before each retry:** logs `logger.warning("Screenshot timed out for project %d (attempt %d/%d); retrying", ...)`.
  - **After the last attempt:** raises `TestRunnerUnresponsiveError(f"Test Runner did not return a screenshot after {n} attempts of {timeout}s each")` from the last error. This text reaches the UI and TestRail. It contains only server-built text.
- No sleep between attempts. The runner handles requests concurrently, and the wait is the whole 30s window, so an extra backoff would only add latency.
- Each loop iteration calls `_dispatch_controller_action` again, which keeps its own reconnect logic:
  - `wait_for_controller` returns immediately while the same runner is connected.
  - If the connection drops during a retry, the existing reconnect/resend rule still applies, and the outage clock is respected.

The retry stays inside `controller_screenshot` and is not built as a generic dispatcher option. Only screenshot is both runner-local and safe to repeat, so a dispatcher parameter would invite non-idempotent reuse for no gain. If a second idempotent action needs this later, lift the loop into a private helper then.

### Pages and forms
None.

### Background work and WebSockets
- No new task or event. The worst-case added time per screenshot is about 60s: 2 extra attempts of 30s each.
- Each case has a soft limit of `1800 + outage` seconds (`projects/tasks.py:279`). A screenshot that is truly hung now uses 90s of that instead of 30s. If the soft limit fires inside the loop, it propagates, because it is not caught.

### Settings / infrastructure
No new env var (see Q1). The attempt count is a code constant.

### Data flow
1. A tool calls `controller_screenshot`.
2. Attempt 1 dispatches and gets no reply in 30s while the owner is unchanged. The function logs a warning.
3. Attempt 2 dispatches with a new `request_id` and reply channel. If it gets a reply, it returns the `ScreenshotResult`. Otherwise attempt 3 runs.
4. After attempt 3 times out, `TestRunnerUnresponsiveError` reaches `safe_tool_call` and is re-raised. The case is Blocked `runner_unresponsive` with the new message.

## Alternatives considered
- **A generic `unresponsive_attempts` option on `_dispatch_controller_action`:** rejected. It is more surface, and it risks someone turning it on for clicks or typing.
- **Retrying in the agent tool layer (`take_screenshot`, `confirm_action`, ...):** rejected. There are three call sites to keep in sync, and the agent layer should not know about controller timeouts.
- **Raising the timeout to 90s:** rejected. One stuck request gets no fresh dispatch, and the runner never gets a second chance if the first request or its reply was lost in transit.
- **An env setting for the attempt count:** deferred (Q1). Keep it minimal until there is a need.

## Risks and mitigations
- **A truly dead runner blocks 60s later:** acceptable. A connection drop still takes the fast `_ConnectionLost` path.
- **Abandoned multi-MiB replies stay in Redis until the channel expires:** at most 2 per screenshot, and they are dropped on expiry (channels_redis default `expiry` is 60s). This is acceptable.
- **The runner keeps capturing for abandoned requests:** the cost is small, and the requests are concurrent rather than queued.
- **Exception text reaches TestRail:** the message is fixed server text plus the integers for the attempt count and timeout.

## Implementation steps
1. Add `_SCREENSHOT_ATTEMPTS` and the retry loop with logging to `controller_screenshot`, and update its docstring (files: `projects/services.py`; import `Final` if it is not already imported).
2. Run `pre-commit run --all-files`, which covers black, isort and mypy strict.

## Verification
- Type check: run `mypy .` and `pre-commit run --all-files`, and confirm both are clean.
- **Manual, retry path:** temporarily add `time.sleep(35)` to `execute_screenshot` on a local runner, but only for the first call (a module-level counter), and do not commit this change. Run a case, or `python manage.py test_controller_actions` for screenshot. Expect one WARNING retry log, then success, and the case is not Blocked.
- **Manual, exhaustion path:** make every capture sleep 35s. Expect 2 warnings, then Blocked `runner_unresponsive` with the "after 3 attempts" message, after about 90s.
- **Manual, disconnect:** stop the runner during attempt 2. Expect the reconnect wait path, unchanged from today.
- Run the `reviewer` and `verifier` agents.

## Open questions
- Q1: Should the attempt count be an env setting (`CONTROLLER_SCREENSHOT_ATTEMPTS` in `settings.py` + `example.env`)? (Assumption used in this draft: no. It is a code constant of 3.)
- Q2: Should `controller_browser_take_screenshot` also retry its timeout? Today it does not block the case; the agent sees a tool error. (Assumption used in this draft: no change.)
- Q3: Keep the per-attempt timeout at 30s, so the worst case is 90s before Blocked? Or shorten the per-attempt timeout, for example to 20s, to keep the total near today's figure? (Assumption used in this draft: keep 30s per attempt.)

## Changelog
- 2026-10-23: created
