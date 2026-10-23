# fix: Passive browser downloads fail with "Cannot switch to a different thread"

Status: Done
Created: 2026-10-23
Request: On the Test Runner (macOS, Playwright 1.50 sync API, Python 3.13), every
download that a page starts on its own (a click on a download link, not an explicit
`browser_download`) is logged as `Download save failed ...: Cannot switch to a
different thread`. It is then followed by an asyncio `greenlet.error`. The download is
recorded as `failed` and never copied to `~/Downloads`. Fix it so passive downloads
are saved and reported correctly by `browser_list_downloads`. Keep the server-facing
result contract unchanged, and leave out threading that is no longer needed.

## Context

- `controller_client/browser_executor.py`:
  - `BrowserSession.ensure_page` starts `sync_playwright()` lazily. It registers `self._page.on("download", self._on_download)`.
  - `_on_download` (l.330) appends a `DownloadRecord(status="in_progress")` under `_downloads_lock` and starts a daemon `threading.Thread` running `_save_download`.
  - `_save_download` (l.345) calls `download.save_as(~/Downloads/<suggested_filename>)`. It then sets `path`, `size` and `status` under the lock, and sets `record.completed_event`.
  - `DownloadRecord.completed_event: threading.Event` (l.240) and `_downloads_lock` exist only for this thread hand-off. Those are the only uses of `threading` in the module.
  - `execute_browser_list_downloads` (l.682) takes `snapshot_downloads()`. For each `in_progress` record it calls `completed_event.wait(remaining)`, bounded by `_LIST_DOWNLOADS_WAIT_S = 300.0`, then formats the result with `_format_downloads_list`.
  - `execute_browser_download` (l.646) uses `page.expect_download` plus `download.save_as` on the calling thread. The `page.on("download")` handler also fires for this download, so today a second, passive save of the same file is also started. Because of the bug, that save shows up as a `failed` entry in the list.
  - `close()` runs `_close_browser()` (closes the context, which deletes Playwright's temp download files), then `clear_downloads()`, then `playwright.stop()`.
- `controller_client/client.py`:
  - Messages are handled one at a time (`_message_loop` -> `_run_until_closed`).
  - Every browser handler calls `asyncio.to_thread(execute_browser_*, self._browser_session, ...)`, as do `stop()` (l.260, `self._browser_session.close`) and `_handle_cleanup_environment` (l.958, `execute_cleanup` -> `BrowserSession.close`). `asyncio.to_thread` uses the loop's default `ThreadPoolExecutor`, which has many workers. Nothing pins Playwright to one thread.
- `controller_client/cleanup.py` `_close_browser` calls `browser_session.close()`.
- Server side, which does not change:
  - `projects/services.py` `controller_browser_list_downloads` waits up to 310 s.
  - The tool description in `agents/services/tool_definitions.py` says "Blocks until every in-progress download finishes (up to 5 minutes)".
- Playwright in use: `playwright~=1.50.0` (`controller_client/requirements.txt`, installed 1.50.0).

## Root cause

**Confirmed.** Playwright's sync API is greenlet-based and bound to the thread that ran
`sync_playwright().start()`.
- `SyncBase._sync` (`playwright/_impl/_sync_base.py`, l.104-115 in 1.50.0) captures `greenlet.getcurrent()`, schedules the coroutine on the Playwright loop, and switches to `self._dispatcher_fiber`.
- From a foreign thread that switch raises `greenlet.error: cannot switch to a different thread`. This is the WARNING.
- The task's done callback `lambda _: g_self.switch()` (l.111, exactly the frame in the log) later runs on the Playwright thread and tries to switch into the greenlet of the worker thread, which has exited by then. This is the asyncio ERROR.
- So `_save_download` always fails. Passive downloads have never been saved since they were introduced (839a842).

**Confirmed by reading the code, latent and not yet observed.** The other browser actions are also not guaranteed to run on the Playwright thread.
- `asyncio.to_thread` hands each action to whichever idle worker of the default pool picks it up.
- The pool has more than one worker whenever two jobs overlapped. For example, OmniParser preloads in `to_thread` while the first browser action runs, or a handler cancelled by `_run_until_closed` keeps its thread busy.
- After that, a browser action can land on a thread other than the one that started Playwright and fail with the same greenlet error.
- This also matters for the fix: the new wait loop must run on the Playwright thread.

**Playwright behaviour the fix relies on** (docs, playwright.dev/python/docs/api/class-download and /docs/downloads, and the 1.50.0 source):
- In the sync API each event listener runs in its own `EventGreenlet` on the Playwright thread (`_connection.py` l.448-455: "Each event handler is a potentially blocking context"). Blocking sync calls inside a handler are supported. The official example is `page.on("download", lambda download: print(download.path()))`.
- The handler greenlet only makes progress while the Playwright thread's event loop is running, that is, while some sync API call is in progress on that thread.
- `save_as`, `path`, `failure` and `delete` wait for the download to finish and take no timeout. There is no public, non-blocking "is finished" check.
- "All the downloaded files belonging to the browser context are deleted when the browser context is closed."

## Goals
- Passive downloads are saved to `~/Downloads/<suggested_filename>` and reported as `completed` with path and size, or as `failed` with Playwright's error.
- `browser_list_downloads` still waits at most `_LIST_DOWNLOADS_WAIT_S` for in-progress downloads, then reports whatever state they are in.
- Every Playwright call, including session close from `stop()` and cleanup, runs on one dedicated thread.
- Remove the threading machinery that the fix makes unnecessary (`threading.Thread`, `_downloads_lock`, `completed_event`).

## Non-goals
- Any change to the protocol, payload, server, or the agent tool description.
- Native (non-Playwright) download handling.
- Saving to a custom directory or de-duplicating filenames. Same-name overwrite stays as it is today.

## Design

### Data model
None (controller only). `DownloadRecord` loses `completed_event`. Its other fields are unchanged.

### Controller: one Playwright thread (`controller_client/client.py`)
- `ControllerClient.__init__` creates one `concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")` owned by the client.
- A private helper (e.g. `_run_browser(fn, *args)`) runs a callable on it via `asyncio.get_running_loop().run_in_executor(...)`. Type it with `ParamSpec`/`TypeVar`, or pass `functools.partial`, so mypy strict holds.
- Every call that touches `self._browser_session` goes through this helper instead of `asyncio.to_thread`:
  - all `_handle_browser_*` handlers (navigate, click, double_click, type, hover, get_elements, get_page_content, get_url, take_screenshot, download, list_downloads);
  - `_handle_cleanup_environment`. `execute_cleanup` closes the browser. Its other steps are thread-agnostic and run there too.
  - `stop()`'s `self._browser_session.close`, after which the executor is shut down (`wait=False`, since close has already been awaited).
- Non-browser actions keep `asyncio.to_thread`.
- Consequence: a browser action whose handler was abandoned on socket drop still occupies the thread, and the next browser action queues behind it. That is the correct result, because Playwright cannot run two sync calls at once anyway.
- I chose not to add a generic "thread-affine session" abstraction. One single-worker executor is enough.

### Controller: download handling (`controller_client/browser_executor.py`)
- `_on_download(download)` runs in Playwright's handler greenlet on the Playwright thread:
  1. If an explicit download is in progress (see below), return immediately.
  2. Otherwise append `DownloadRecord(status="in_progress")`, then call `download.save_as(~/Downloads/<name>)` directly in the handler. It must not start a thread.
  3. On success set `path`, `size` (`stat`) and `status="completed"`. On exception set `status="failed"` and `error`, and log a warning (current wording).
  4. No lock is needed. All access is on one thread, and greenlets only switch inside Playwright calls.

  Effect: the save starts as soon as the event is dispatched. It streams whenever any later browser action runs, so the file lands in `~/Downloads` without the agent having to call `browser_list_downloads`.
- `_save_download`, `_downloads_lock` and `DownloadRecord.completed_event` are removed, and so is `import threading`. `snapshot_downloads` and `clear_downloads` become plain list operations.
- New `BrowserSession.wait_for_downloads(timeout_s: float) -> None`:
  - While any record is `in_progress` and time remains, call `page.wait_for_timeout(min(poll_ms, remaining_ms))` on the session's open page. Use a `_DOWNLOAD_POLL_MS` constant of about 250 ms.
  - `wait_for_timeout` drives the Playwright event loop, which is what lets the handler greenlets finish their saves.
  - If there is no open page (page or context closed), stop waiting at once. A closed context means the saves have already failed or will never progress.
  - It must never use `time.sleep` or `threading.Event.wait` on the Playwright thread. That would stall the loop, the saves could not advance, and every list call would wait out the full 300 s.
  - This is how the remaining timeout is honoured despite `save_as` having none: the blocking `save_as` lives in the handler greenlet, and the main greenlet polls with a bounded wait.
- `execute_browser_list_downloads`: replace the per-record `completed_event.wait` loop with `session.wait_for_downloads(_LIST_DOWNLOADS_WAIT_S)`, then snapshot and format exactly as today. The output contract (`ActionResultPayload`, message format from `_format_downloads_list`) is unchanged.
- Explicit `execute_browser_download`:
  - Set a session flag (e.g. `_explicit_download_active: bool`, toggled through a small context manager or a begin/end method pair, cleared in `finally`) around the `expect_download` block, so the passive handler skips that download.
  - This removes the concurrent second `save_as` of the same file into `~/Downloads`. The explicit save path is unchanged.
  - After a successful save, append a `completed` `DownloadRecord` through a session method (e.g. `record_download`), so `browser_list_downloads` still lists it ("List all downloads that occurred during this browser session"), now with the real `save_path`.
  - Trade-off: an unrelated passive download that starts during the same at most 15 s window is not tracked. This is accepted.
- `close()` semantics are unchanged:
  - `_close_browser()` closes the context. Playwright deletes its temp files, and any handler greenlet blocked in `save_as` resumes with an error during that call and marks its record `failed`.
  - `clear_downloads()` then empties the list, and `playwright.stop()` follows.
  - Records from a previous session never leak into the next one.
  - Switching browser (`ensure_page` -> `_close_browser`) still does not clear the list. Records of the old browser keep their final status, which is today's behaviour.

### Pages and forms
None.

### Background work and WebSockets
- No new events. No protocol change: same `browser_list_downloads` / `browser_download` actions, payloads and result text.
- `CLIENT_VERSION` 0.5.0 -> 0.5.1. This is informational only, so a fixed runner can be told apart in handshake logs. The server does not gate on it.
- No `ClientCapability` and no change to `REQUIRED_CLIENT_CAPABILITIES`. Old runners keep working with the bug and get the fix by re-downloading the runner ZIP.

### Settings / infrastructure
None.

### Data flow
1. The agent calls `browser_click`. It runs on the Playwright thread, and the page starts a download.
2. Playwright dispatches `download`, and `_on_download` runs in an EventGreenlet: it records `in_progress` and blocks in `save_as`. The click returns.
3. Later browser actions, or `browser_list_downloads` -> `wait_for_downloads` polling via `wait_for_timeout`, drive the loop. The save completes, and the handler sets `completed` with path and size.
4. `execute_browser_list_downloads` formats the snapshot, and the result goes to the server unchanged.

## Alternatives considered
- **Lazy save at inspection time** (the handler only stores the `Download`, and `list_downloads` calls `save_as`):
  - Rejected. `save_as` has no timeout, so a stalled download would block the Playwright thread past the 300 s and the server's 310 s, wedging every later browser action.
  - Files would also only reach `~/Downloads` if the agent happened to call `list_downloads` before the context closed.
- **Polling `failure()` or `path()`**: both block until finish, so they cannot be polled. The private `Artifact.absolute_path` is not public API.
- **Keep the worker thread but marshal the call to the Playwright thread**: there is no such public API in sync Playwright, and this is more machinery than the handler greenlet that Playwright already provides.
- **Only fix downloads and leave `asyncio.to_thread`**: this leaves the latent cross-thread failure for all browser actions, and the new wait loop could run on the wrong thread.

## Risks and mitigations
- **Head-of-line blocking on the single browser thread.** A long `list_downloads` (up to 300 s) or an abandoned handler delays the next browser action. This is acceptable: actions are already serial per connection, and Playwright cannot run them concurrently.
- **`wait_for_timeout` needs an open page.** If the user or agent closed the tab but the context lives on, the wait exits early and reports `in_progress`. Mitigation: use any open page of `self._context` (`context.pages`) before giving up.
- **Explicit-download flag hides an unrelated download in the same window.** Rare, and documented above.
- **A filename collision with an existing file in `~/Downloads`** overwrites it, as today. Out of scope.
- No secrets, auth or server changes.

## Implementation steps
1. Pin browser work to one thread. Add the single-worker executor, the typed `_run_browser` helper, and route every `_browser_session` call (11 browser handlers, cleanup, `stop()`) through it. Shut it down in `stop()`. (files: `controller_client/client.py`)
2. Rework passive downloads:
   - make `_on_download` save inline;
   - remove `_save_download`, the lock, `completed_event` and `import threading`;
   - add `wait_for_downloads`, `_DOWNLOAD_POLL_MS`, the explicit-download flag and `record_download`;
   - update `execute_browser_list_downloads` and `execute_browser_download`.

   (files: `controller_client/browser_executor.py`)
3. Bump `CLIENT_VERSION` to `0.5.1`. (files: `controller_client/protocol.py`)
4. Run `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`, then `pre-commit run --all-files`.

## Verification
Manual checks on a controller machine, macOS and ideally Windows, with `python -m controller_client.main` against a local server:
- **Passive download:** run a case that clicks a download link (e.g. a Zulu JDK `.dmg`, or a small file).
  - The runner log must not show "Cannot switch to a different thread" or an asyncio `greenlet.error`.
  - The file appears in `~/Downloads`.
  - `browser_list_downloads` reports `completed` with path and size.
- **Large or slow download:** call `browser_list_downloads` right after the click. It blocks until done, well under 300 s, and returns `completed`.
- **Stalled download:** throttle the network or use a slow endpoint. The list returns after `_LIST_DOWNLOADS_WAIT_S` with `in_progress`, and the next browser action still works.
- **Explicit `browser_download`:** the file is saved once to the requested path, the list shows it once as `completed` with that path, and there is no failed duplicate.
- **Cleanup:** start a download, then run `cleanup_environment`. The browser closes without hanging, and the next list says "No downloads recorded."
- **Thread pinning:**
  - Interleave browser actions with `screenshot` / `find_element` / `run_command` while OmniParser preloads after connect. No greenlet errors appear.
  - Optionally log `threading.current_thread().name` once in a debug build: it should always be `playwright_0`.
- **Shutdown:** stop the runner with Ctrl+C after browser use. It exits cleanly.
- Type check: controller mypy (strict) and `pre-commit run --all-files` are clean.
- Run the `reviewer` and `verifier` agents on the diff.

## Open questions
None blocking. Decisions taken in this draft that the user may want to overrule:
- An explicit `browser_download` is listed once as `completed` with its real save path. The passive handler skips it instead of saving a second copy. (Alternative: do not list explicit downloads at all.)
- `CLIENT_VERSION` gets a patch bump to `0.5.1`, informational only, with no capability.

## Changelog
- 2026-10-23: created
- 2026-10-23: implementation deviations and review outcomes:
  - Executor is not shut down in `stop()`: `stop()` can run twice (signal handler, then after `run()` returns, `controller_client/main.py`) and a second `run_in_executor` after shutdown would raise. The idle worker is joined at exit.
  - Review: an explicit `save_as` had no timeout and would now block the single Playwright thread (and shutdown). Fixed: `browser_download` no longer saves itself; `explicit_download(save_path)` lets `_on_download` claim the next download and save it to the requested path, and `execute_browser_download` waits for that record with `wait_for_downloads(..., [record])`, bounded by what is left of `_EXPLICIT_DOWNLOAD_BUDGET_S=110` measured from the action start, browser launch included (server waits 120 s; second verifier pass). This replaces the skip flag and `record_download`. Still in progress after the wait -> `ExecutionError`; the record keeps saving and shows in `browser_list_downloads`.
  - Review: `page.wait_for_timeout` can raise if the page closes mid-poll. Fixed: caught as Playwright `Error`, the wait returns and the current snapshot is reported.
  - `_handle_download_timeout` is typed `NoReturn` (it always raised) so mypy narrows the claimed record.
  - Accepted, not changed: `stop()` queues behind a running browser job, so Ctrl+C during `browser_list_downloads` can take up to 300 s. Every browser job is now bounded, so it cannot hang forever.
  - Rejected as out of scope: sanitising `suggested_filename` (pre-existing; browsers already strip path separators).
  - Second review: `expect_download` cannot bound the explicit path. `Page._on_download` emits to its pyee listeners in order within one dispatch greenlet, so the waiter's listener only runs after our handler's blocking `save_as` returns; a download slower than 15 s was reported as "No download triggered". Fixed: `execute_browser_download` drops `expect_download`, calls `goto` inside the claim (a `goto` error is fatal only if no download is claimed) and polls with `wait_for_claim(claim, 15 s)`; the shared poll loop is `BrowserSession._drive_until`.
  - Third pass clean. Accepted: a `goto` that fails for a real reason (DNS, refused) now reports after the 15 s claim window, because WebKit emits the download event after `goto` has already raised "Download is starting". Downloads started in popups are still untracked (pre-existing, out of scope).
