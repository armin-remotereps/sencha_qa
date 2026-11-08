# MCP browser toolset: Chrome, Edge, Firefox and Safari on desktop, Android and iOS

Date: 2026-11-08
Plan: docs/plan/feat-mcp-browser-toolset.md

## What changed
- `controller_client/protocol.py`: `WEB_BROWSER_V1`, CLIENT_VERSION 0.22.0, the `web_browser_*` messages and `web_browser_result`, `WebBrowser`, the one support table `web_browsers_for`, limits and the error codes `WEB_BROWSER_SETUP_NEEDED` / `_UNSUPPORTED` / `_NOT_OPEN` / `_AMBIGUOUS` and `SCRIPT_FAILED`.
- `controller_client/web_browser_*.py`: a new WebDriver browser layer, with no Playwright.
  - `hub`: picks the browser per call, runs on a daemon worker within a budget, and refuses a browser as busy while an overrun call on it runs.
  - `desktop`, `launch`, `presence`: Selenium plus Selenium Manager. Chrome/Edge/Firefox are attached through the dedicated profile's port file; Safari is a safaridriver session.
  - `android`: chromedriver attached to the running Chrome/Edge, a geckodriver session for Firefox, and adb forwards removed on detach.
  - `ios`: web view contexts of the XCUITest session through `MobileDevice.web_call`, switching back to NATIVE_APP on every path.
  - `accessibility`: the fallback mode when a page is not inspectable.
  - `page_ops`, `page_script`, `evaluate`, `diagnostics`.
- Runner wiring:
  - `client.py`: handlers, error codes, shutdown and cleanup;
  - `config.py`: `CONTROLLER_WEB_DRIVER_DOWNLOAD`;
  - `mobile_ios.py`: web view capabilities;
  - `diagnose --web-browsers`;
  - `update_layout` / `update_runner` / `update_cleanup`: the `open-test-browser.sh|.cmd` shortcut, and the profiles moved by the new runner after the old one exits;
  - setup scripts: write the shortcut, desktop only.
- Server:
  - `projects/controller_web_browser_actions.py`: one function per message;
  - consumer, reply tracker and `controller_protocol` routing;
  - five `ControllerActionError` subclasses mapped in `_raise_for_error_reply`;
  - `runner_capability_services.web_browser_check` / `web_browsers_supported`.
- `mcp_gateway`: 16 `browse_*` tools (67 in all).
  - Services `browse_choice`, `browse_actions`, `browse_element_pick`, `browse_evaluate` and `machine_call`.
  - `CallKind.EVAL` and its throttle.
  - New refusal codes: `browser_setup_needed`, `browser_action_unsupported`, `element_not_found`, `script_failed`, `browser_not_open`, `browser_ambiguous`, `browser_action_failed`, `browser_out_of_time`.
  - `get_project.machine.web_browsers`.
- Packaging: `web_profiles/` is excluded from the ZIP, the update bundle, `.dockerignore` and `.gitignore`.

## Why
The user wants an MCP client to control Chrome, Edge, Firefox and Safari on desktop, Android and iOS while it helps a person with a claimed Manual case. These tools are separate from the Playwright `browser_*` tools; the user's decisions are D1–D10 in the plan.

## Behaviour and contract changes
- **New MCP tools:**
  - `browse_open(browser, url?)` and `browse_list_browsers`;
  - `browse_navigate`, `browse_history`, `browse_tabs`;
  - `browse_list_elements`, `browse_click`, `browse_double_click`, `browse_hover` (desktop only), `browse_type`, `browse_select`, `browse_press_key`;
  - `browse_read`, `browse_wait`, `browse_evaluate`, `browse_close`.

  Every one takes the claim, as the machine tools do.
- **Browser choice:**
  - Nothing opens by itself; only `browse_open` starts a browser.
  - The other tools act on the browser named in the call, or on the only open one. Otherwise they answer `[browser_not_open]` or `[browser_ambiguous]`.
  - A person can open desktop Chrome, Edge or Firefox with `<base>/open-test-browser.sh|.cmd`, and phone browsers by opening the app.
  - Desktop Safari and Android Firefox can only be opened with `browse_open`.
- **Targeting:** an element is exactly one of `index`, `description` (one AI pick), `css` or `xpath`. `browse_evaluate` is capped at 20k characters, 10 s and a 64 KiB result.
- **Controller protocol:** new optional capability `WEB_BROWSER_V1`. Older runners get `[machine_outdated]`. No required capability changed.
- **Runner `.env`:** `CONTROLLER_WEB_DRIVER_DOWNLOAD` (default true).
- **Runner folder:** `controller_client/web_profiles/<browser>/`.
- **No migrations and no server settings.**

## Notes for future changes
- **Never run on a real browser or device.** The plan's Step 0 device test was skipped at the user's choice. Walk the plan's Verification list, starting with:
  - iOS Edge/Firefox inspectability;
  - Android Edge's DevTools socket;
  - Android Firefox settings;
  - the attach paths;
  - chromedriver's adb forward format.
- **Accepted limits** are listed in the plan's Changelog:
  - no auth on the loopback remote-control port;
  - worst-case iOS time over 28 s, because the presence check and the action each make their own phone-session call;
  - Android accessibility taps may repeat after a session recreate;
  - cleanup can reach about 12 s with all Android sessions open;
  - the iOS script timeout is left at 15 s.
- **A deadline-aware `MobileDevice.web_call`** would bound the iOS timing; that is a follow-up.
- **Runner text never reaches an MCP refusal.** Runner failures map to fixed texts, and the raw text goes to the server log only, cut short.
