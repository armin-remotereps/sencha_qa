# Run test cases in any Playwright browser

Date: 2026-10-22
Plan: docs/plan/feat-browser-selection.md

## What changed
- `controller_client/protocol.py`:
  - `BrowserName` (chromium, firefox, webkit, chrome, msedge), `BrowserEngine`, `BROWSER_ENGINES`, `BROWSER_DISPLAY_NAMES`, `DEFAULT_BROWSER`;
  - `browser` on the browser payloads, and a new `BrowserTargetPayload`;
  - optional `ClientCapability.BROWSER_SELECTION_V1`, `ErrorCode.BROWSER_NOT_INSTALLED`;
  - `CLIENT_VERSION` 0.4.0.
- `controller_client/browser_executor.py`:
  - `launch_browser` picks the engine and channel. `--disable-gpu` is passed for the Chromium engine only; `--no-sandbox` is dropped because Playwright adds it.
  - `BrowserSession.ensure_page(browser)` relaunches when the browser changes or has crashed.
  - A not-installed launch raises `BrowserNotInstalledError`.
- `controller_client/client.py`: `_browser_error_code` replaces the per-handler except chains.
- `controller_client/browser_diagnostics.py` (new) and `diagnose.py --browsers`.
- `projects/services.py`:
  - every `controller_browser_*` except list_downloads takes a required `browser`;
  - `BROWSER_NOT_INSTALLED` raises `BrowserUnavailableError` (shown text is fixed; the runner text goes to the log only).
- `projects/browser_selection_services.py` (new): `ensure_runner_supports_browser`.
- `projects/case_blocking.py`, `projects/models.py`: `BlockedReason.BROWSER_UNAVAILABLE`.
- `agents/`:
  - `BrowserSelection` (one per case, created in `run_orchestrator`) is passed to every sub-agent's `ToolContext`;
  - `browser_navigate` takes an optional `browser` enum;
  - the prompts (environment, selection rule, sub-agent "current browser", planner rule) are updated.
- `templates/partials/runner_download_card.html`, setup scripts: help text on browsers.

## Why
The Test Runner always launched bundled Chromium. Test cases now run in the browser their text names, and Chromium stays the default.

## Behaviour and contract changes
- The browser is chosen by the agent from the test case text. There is no run, project or case field, and it resets to Chromium for each case.
- WebSocket browser actions carry `"browser": "<name>"`. A missing key means chromium, and an unknown value gets `INVALID_MESSAGE`.
- 0.3.0 runners still connect. A non-Chromium case on one is Blocked (`browser_unavailable`, "download a fresh Test Runner").
- A browser missing on the machine leaves the case Blocked (`browser_unavailable`), not Failed.
- Switching browser mid-case closes the old browser, its page state and any download still in progress. The case's download records are kept.
- Migration `0019`: choices only, no SQL.

## Notes for future changes
- The not-installed check matches Playwright error text (`_NOT_INSTALLED_PATTERN`). Re-check it on any Playwright upgrade.
- Other launch failures (for example missing Linux host deps for WebKit) still surface as `EXECUTION_FAILED` tool errors.
- Chrome and Edge are never installed by the setup scripts: Playwright's installers remove and reinstall the system browser.
- To check by hand: plan Verification steps 2–8, including the "unsupported flag" infobar on branded Chrome/Edge on Windows.
