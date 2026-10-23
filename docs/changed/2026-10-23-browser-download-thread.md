# Save browser downloads on the Playwright thread

Date: 2026-10-23
Plan: docs/plan/fix-browser-download-thread.md

## What changed
- `controller_client/client.py`: every `BrowserSession` call (11 browser handlers, `cleanup_environment`, `stop()`) runs through `ControllerClient._run_browser` on a single-worker `playwright` executor instead of `asyncio.to_thread`.
- `controller_client/browser_executor.py` `BrowserSession._on_download`: saves the download inline in Playwright's event greenlet; the worker thread, lock and `completed_event` are gone.
- `BrowserSession.wait_for_downloads` / `wait_for_claim` (shared `_drive_until`): bounded polls with `page.wait_for_timeout`, which drives the event loop so pending saves can finish.
- `execute_browser_download`: no own `save_as` or `expect_download`; it claims the next download via `explicit_download(save_path)`, navigates, then waits within a 110 s budget from action start.
- `controller_client/protocol.py`: `CLIENT_VERSION` 0.5.1.

## Why
Playwright's sync API is bound to the thread that started it. Passive downloads were saved from a worker thread, so every one failed with "Cannot switch to a different thread" plus an asyncio `greenlet.error`. Other browser actions could also land on the wrong pool thread.

## Behaviour and contract changes
- No protocol, payload or server change. `browser_download` messages keep their wording. An explicit download is now listed once, as completed with its real path, instead of alongside a failed duplicate. A download still running when the budget runs out fails with "still in progress", and the save carries on.
- `CLIENT_VERSION` 0.5.1 is informational only; old runners keep the bug until the runner ZIP is re-downloaded.

## Notes for future changes
- Never call Playwright outside `_run_browser`, and never `time.sleep`/`Event.wait` on that thread: pending saves only advance while a Playwright call runs the loop.
- `Page._on_download` emits to its listeners in order within one greenlet, so a listener added after `_on_download` (e.g. `expect_download`) only fires once the save returns. Do not reintroduce `expect_download` on this path.
- A failed `goto` in `browser_download` reports after the 15 s claim window (WebKit emits the download after `goto` raises).
- `stop()` queues behind a running browser job (up to 300 s during `browser_list_downloads`).
