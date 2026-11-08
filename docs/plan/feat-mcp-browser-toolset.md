# feat: Browser toolset for MCP across desktop, Android and iOS

Status: Done
Created: 2026-11-08
Request: "Browser control toolset to be able to control chrome, edge, firefox,
and safari on desktop, android, and ios — for MCP." The MCP client (inside
its claimed Manual case) gets a **new, separate** set of browser tools,
built on a runner-side browser layer that does not use Playwright. The
layer drives the real browsers on Windows, Linux, macOS, Android and iOS at
page level:

- open and navigate pages, with history and tabs;
- list elements and act on them by index, plain-words description, CSS or
  XPath;
- type and select, read text, URL and title;
- evaluate JavaScript in the page.

The existing Playwright `browser_*` tools and the server agent stay exactly
as they are. Type: `feat`.

## Decisions (user's answers, 2026-11-08)

- **D1 Separate tools, Playwright untouched.** The nine existing MCP
  `browser_*` tools, their path (`run_agent_tool` → agent registry →
  Playwright) and all of the agent's browser code stay exactly as they are.
  The new tools have their own prefix, **`browse_`** (`browser_` is taken;
  `web_` would read like `web_search`). They run on a new runner layer
  that is independent of Playwright ("it's completely different from
  playwright").
- **D2 Support matrix: every cell is in scope.**
  - iOS: Safari, Chrome, Edge, Firefox;
  - Android: Chrome, Edge, Firefox;
  - Windows: Chrome, Edge, Firefox;
  - Linux: Chrome, Firefox;
  - macOS: Safari, Chrome, Firefox.
- **D3 One primary protocol per cell**, chosen from the user's research
  (verified below): W3C WebDriver through each vendor's driver as the one
  family, with Appium's WebKit Inspector path on iOS (see Design ›
  Protocol per cell).
- **D4 iOS Edge and Firefox at page level** through the cable-connected
  WebKit Inspector (Appium XCUITest web view contexts), when the build's
  web views are inspectable. Otherwise an accessibility-tree fallback. Both
  are checked in the Step 0 device test.
- **D5 Server agent: MCP only.** The agent is unchanged.
- **D6 Element targeting:** index (from a list-elements tool), plain-words
  description, raw CSS and XPath selectors, and JavaScript evaluate in the
  page are all in scope. Evaluate is bounded: size limits, a timeout, the
  result fenced as data, neutral wording.
- **D7 Firefox on Android uses geckodriver's test profile** (an earlier
  default; not rejected, unchanged by the new design).
- **D8 No implicit browser** (answer to Q1, verbatim: "Browser should be
  open by agent or human, if not, browser tools will throw error").
  - No platform default, and no auto-launch by any tool except
    `browse_open`.
  - "Agent" is the MCP client, which opens a browser with `browse_open`
    (`browser` required).
  - "Human" is a person who opened it on the test machine or phone; the
    tools attach to it where the cell allows (Design › Open and attach).
  - Every other `browse_*` tool acts on an already open, controllable
    browser. With none open it fails `[browser_not_open]`. With several
    open and no `browser` named it fails `[browser_ambiguous]`, listing
    them.
- **D9 Driver downloads on** (answer to Q2): Selenium Manager downloads
  chromedriver, msedgedriver and geckodriver into its cache;
  `CONTROLLER_WEB_DRIVER_DOWNLOAD` defaults to true.
- **D10 One dedicated persistent profile per desktop browser** in the Test
  Runner folder (answer to Q3). The Test Runner's browser launcher, used by
  both `browse_open` and the person's shortcuts, always starts the browser
  with that profile and with remote control on, which is what makes a
  person-opened desktop browser attachable (D8).

## Context

- **MCP machine tools** (`docs/changed/2026-11-08-mcp-machine-tools.md`,
  `docs/plan/feat-mcp-runner-tools.md`):
  - `mcp_gateway/machine_tools.py` (`MACHINE_TOOLS`, asserted count 36;
    `CallKind` throttles; `targets`; `dispatches`);
  - handlers in `machine_handlers.py`, schemas in `schemas.py`;
  - the gate `services/machine_gate.open_machine_session` (live MCP claim,
    connected, device ready, `tool.targets`, no readiness run; it loads
    `agent_capabilities` and the OS name once into `MachineSession`);
  - the lock and deadline block in `services/run_agent_tool.py`
    (`machine_round_trip`, `machine_lock`, `no_reconnect_wait`,
    `call_deadline`, `map_machine_exception`);
  - error codes in `services/machine_errors.py`, fencing
    (`untrusted_text.fence_data`, `neutral_machine_text`), limits in
    `limits.py` (lock TTL ≥ deadline + margin asserted).
- **Existing Playwright browser** (unchanged by D1):
  - `controller_client/browser_executor.py` (`BrowserSession`,
    `_COLLECT_ELEMENTS_JS` stamping `data-at-idx`);
  - the `playwright` worker thread in `client.py`;
  - `projects/services.py` `controller_browser_*`;
  - `agents/services/browser_element_finder.py`;
  - the agent's `BrowserSelection` and `BrowserName` enum.

  Phones refuse every `browser_*` (`client.py::_DESKTOP_ONLY_MESSAGE_TYPES`).
- **Phone runner** (`docs/changed/2026-11-06-mobile-device-runner.md`):
  - one Appium 3 session per device: `mobile_session.MobileSession` with an
    action lock, a 24 s budget and recreate on loss;
  - capabilities in `mobile_android.py` / `mobile_ios.py`;
  - managed Appium on 127.0.0.1 without `--relaxed-security`;
  - adb is a prerequisite on Android (`mobile_discovery.py`). The iOS host
    is a Mac (XCUITest needs Xcode).
- **Runner deps** (`controller_client/requirements.txt`): `selenium~=4.51.0`
  (includes Selenium Manager), `Appium-Python-Client~=6.1.5`,
  `playwright~=1.64.1`. No new Python dependency is needed.
- **Protocol** (`controller_client/protocol.py`):
  - `CLIENT_VERSION 0.21.0`;
  - `ClientCapability`, `REQUIRED_CLIENT_CAPABILITIES`;
  - `ErrorPayload(code, message, details)`;
  - `RunnerTarget`, `MOBILE_TARGETS`;
  - `_REFUSED_URL_SCHEMES` (used by `open_url`).
- **Server seams**: controller messages are routed by
  `projects/controller_protocol.py` (event → `MessageType`, allowed fields)
  and forwarded by `projects/controller_consumer.py`. Phone actions live in
  the leaf module `projects/controller_mobile_actions.py`, and
  `_raise_for_error_reply` is in `projects/services.py`.
- **Wording rules** (CLAUDE.md › MCP gateway):
  - nothing user-visible names AI, MCP or an agent;
  - refusals say "test machine";
  - `controller_client/` never mentions MCP;
  - machine output is fenced as data.

### Research (verified 2026-11-08)

Each item below gives the protocol options the user listed, and what was
verified about them.

- **Desktop Chrome / Edge / Firefox.**
  - The user listed CDP, WebDriver BiDi, ChromeDriver/EdgeDriver,
    Marionette and GeckoDriver.
  - Classic W3C WebDriver through chromedriver / msedgedriver / geckodriver
    is the common, stable path. Selenium (4.51 in the runner) drives all
    three. Selenium Manager, which ships inside the wheel, finds or
    downloads the matching driver when a session is created.
  - Chrome and Firefox pass most of the WebDriver BiDi tests; Selenium's
    Python BiDi modules are still changing (4.46 deprecated the raw BiDi
    connection accessors).
  - CDP covers Chromium only. Marionette is Firefox's internal protocol
    behind geckodriver.
- **Desktop Safari** (WebDriver/SafariDriver, WebKit Inspector, "Safari
  MCP").
  - `safaridriver` ships with macOS and needs `safaridriver --enable` once
    (password).
  - One session per Safari. Pages run in a separate automation window (no
    headless).
  - **No WebDriver BiDi in Safari 26** (caniuse: not supported up to TP;
    Selenium marks it experimental). So classic WebDriver is the only
    protocol that covers all four browsers.
- **Android Chrome / Edge** (CDP + ADB, DevTools + ADB, Appium).
  - chromedriver itself drives Chromium apps on a device over ADB, through
    the app's DevTools socket, which is CDP underneath. Capabilities:
    `goog:chromeOptions.androidPackage` (`com.android.chrome`,
    `com.microsoft.emmx`), `androidDeviceSerial`, and `androidUseRunningApp`
    to attach without restarting.
  - The driver's major version must match the app's Chromium.
  - Edge: community reports that Edge does not expose a debuggable page by
    default. **Unconfirmed; spike.**
- **Android Firefox** (GeckoDriver + ADB, Marionette, BiDi).
  - geckodriver ≥ 0.26 drives Firefox on Android with
    `moz:firefoxOptions.androidPackage` (`org.mozilla.firefox`) and
    `androidDeviceSerial`, over ADB to Marionette.
  - It starts Firefox with its own test profile.
  - The device settings it needs are **unconfirmed; spike.**
- **iOS Safari** (WebDriver + SafariDriver, WebKit Inspector, XCUITest).
  - Appium XCUITest attaches to Safari's web views through the WebKit
    remote debugger (`includeSafariInWebviews`).
  - Real devices need Settings › Safari › Advanced › Web Inspector. iOS 18+
    real devices use the RemoteXPC tunnel; older ones use usbmux, with
    fewer features.
  - `ios-webkit-debug-proxy` is not needed: Appium talks to the inspector
    service directly.
  - `safaridriver` can also drive Safari on a connected iPhone, but it locks
    the device into an automation session; rejected below.
- **iOS Chrome** (WebKit Inspector, XCUITest).
  - Chrome 115+ on iOS 16.4+ has its own setting: Settings › Content
    Settings › Web Inspector, then relaunch.
  - The XCUITest driver lists Chrome-based web views (Chrome 115+, iOS
    16.4+) as supported.
- **iOS Edge / Firefox** (XCUITest, limited WebKit inspection).
  - Both are WKWebView apps. Since iOS 16.4 a WKWebView is inspectable only
    if the app sets `isInspectable = true` (default false). When it is, the
    page is reachable over the cable exactly like Chrome's, through the
    WebKit Inspector protocol that Appium's `additionalWebviewBundleIds`
    uses (Edge `com.microsoft.msedge`, Firefox `org.mozilla.ios.Firefox`).
  - **No public source confirms** that current Edge or Firefox iOS builds
    set `isInspectable` (searched Apple and WebKit docs, the Mozilla wiki
    and the firefox-ios repository). The user expects they do. The Step 0
    device test decides, per build.
  - Fallback when a build is not inspectable: XCUITest still sees the web
    content as accessibility elements (links, text, text fields, buttons)
    inside the app's web view. That gives element listing, tap, type,
    visible text and the URL from the address bar. It does not give
    CSS/XPath-in-page, evaluate, hover or tabs.
- **Attaching to a browser someone already opened** (D8):
  - **Chrome / Edge (desktop).** A driver attaches to a running browser
    through `goog:chromeOptions.debuggerAddress` (Edge:
    `ms:edgeOptions.debuggerAddress`), if the browser was started with
    `--remote-debugging-port`.
    - From Chrome 136 that switch is **ignored on the default user data
      directory** (a security change against cookie theft). It works only
      with a non-default `--user-data-dir`.
    - With `--remote-debugging-port=0` Chrome picks a free port and writes
      it to `DevToolsActivePort` in that directory.
    - So an everyday Chrome window is never attachable; one started with
      the dedicated profile is (D10).
    - Edge is treated the same way; the spike confirms its policy.
  - **Firefox (desktop).** `geckodriver --connect-existing --marionette-port N`
    attaches to a Firefox that runs with Marionette on (`--marionette`, port
    from the `marionette.port` pref). Firefox writes the port to
    `MarionetteActivePort` in the profile.
    - A stale port file breaks attaching (Mozilla bug 1755312), so the port
      is always checked before use.
    - Firefox shows its "remote control" look (robot icon, striped address
      bar) while Marionette is on.
  - **Safari (macOS).** `safaridriver` always opens its own automation
    window and cannot attach to a window a person opened. Only `browse_open`
    can produce a controllable Safari on macOS.
  - **Android Chrome / Edge.** The running app exposes a DevTools socket
    (`chrome_devtools_remote`, or Edge's own socket, spike) to ADB.
    chromedriver with `androidUseRunningApp` attaches to the app as the
    person left it, with their tabs and logins.
  - **Android Firefox.** geckodriver starts Firefox itself with its test
    configuration (D7), so a person-opened Firefox is not attachable. Only
    `browse_open` produces one.
  - **iOS (all four).** XCUITest finds the inspectable web views of a
    running app through the Web Inspector, or reads its accessibility tree.
    Whoever opened the app, it is attachable, subject to D4.

Sources:
- [caniuse WebDriver BiDi](https://caniuse.com/wf-webdriver-bidi)
- [Selenium 4.46 release notes](https://www.selenium.dev/blog/2026/selenium-4-46-released/)
- [MDN moz:firefoxOptions](https://developer.mozilla.org/en-US/docs/Web/WebDriver/Reference/Capabilities/firefoxOptions)
- [XCUITest driver overview](https://appium.github.io/appium-xcuitest-driver/latest/overview/)
- [XCUITest hybrid guide](https://appium.github.io/appium-xcuitest-driver/8.1/guides/hybrid)
- [Chrome on iOS 16.4+ debugging](https://developer.chrome.com/blog/debugging-chrome-on-ios)
- [WebKit: Enabling the Inspection of Web Content in Apps](https://webkit.org/blog/13936)
- [Apple `isInspectable`](https://developer.apple.com/documentation/webkit/wkwebview/isinspectable)
- [ios-webkit-debug-proxy](https://github.com/google/ios-webkit-debug-proxy)
- [Appium forum on Edge Android contexts](https://discuss.appium.io/t/problem-with-context-switch-in-ms-edge-browser/46015)
- [CircleCI safaridriver --enable](https://support.circleci.com/hc/en-us/articles/360056461992-Enabling-safaridriver-support-on-macOS)
- [Chrome: Changes to remote debugging switches](https://developer.chrome.com/blog/remote-debugging-port)
- [geckodriver Flags (`--connect-existing`)](https://www.github.com/mozilla/geckodriver/blob/2acb733de5bda81ed30b6f4e90be27fee6f62ff7/doc/Flags.md)
- [Bug 1755312 (stale MarionetteActivePort)](https://bugzilla.mozilla.org/show_bug.cgi?id=1755312)

## Goals

- New MCP tools `browse_*` that work in every D2 cell, with one runner layer
  behind them and one protocol family per platform.
- Page-level control in every cell where the page is inspectable. A
  clearly reported **accessibility mode** where it is not (iOS Edge/Firefox
  builds without `isInspectable`; Android Edge if it exposes no DevTools
  socket). The tool reply always says which mode a page is in.
- Targeting by index, description, CSS or XPath. JavaScript evaluate,
  bounded.
- The tools act only on a browser that is already open and controllable,
  whether the MCP client opened it with `browse_open` or a person opened it
  (through the Test Runner's browser shortcut on desktop, or normally on a
  phone). There is no implicit default and no auto-launch (D8).
- No change to the Playwright tools, the agent, or how older runners behave.

## Non-goals

- Any change to the nine existing MCP `browser_*` tools, `browser_executor.py`,
  `projects.services.controller_browser_*`, `agents/*` (D1, D5).
- WebDriver BiDi or CDP-specific features (network interception, console
  streams, cookies API) in v1. The layer uses classic WebDriver, so BiDi can
  be added later on the cells that support it.
- Edge on Linux and macOS, Chrome on iOS older than 115 or on iOS older
  than 16.4, Samsung Internet, Opera, Brave, Safari Technology Preview,
  hybrid apps' web views.
- File upload dialogs and downloads (no dependable cross-cell path; desktop
  downloads stay with the Playwright `browser_download`).
- Attaching to a person's everyday desktop browser window (Chrome refuses
  remote debugging on the default profile; Safari cannot attach at all), or
  to a person-opened Firefox on Android.
- Installing browsers, drivers or Appium drivers on the host. Exception: the
  driver binaries Selenium Manager downloads (D9). `safaridriver --enable`
  and the device settings are the person's.
- Screenshots as evidence. `capture_screenshot` / `add_screenshot` stay the
  only evidence tools; `take_screenshot` stays the preview tool.

## Design

### Protocol per cell (one primary path each)

Family: **W3C WebDriver (classic)**. On desktop and Android it is spoken to
vendor drivers through Selenium. On iOS it is spoken to the existing Appium
XCUITest session, which reaches pages through the WebKit Inspector protocol
over the cable.

| Cell | Primary path | Driver resolution | Mode when page is not inspectable |
|---|---|---|---|
| Windows / Linux / macOS Chrome | chromedriver (WebDriver; CDP inside the driver) | Selenium Manager, matched to the installed Chrome | n/a |
| Windows Edge | msedgedriver | Selenium Manager | n/a |
| Windows / Linux / macOS Firefox | geckodriver (WebDriver → Marionette) | Selenium Manager | n/a |
| macOS Safari | safaridriver (system) | none (needs `safaridriver --enable`) | n/a |
| Android Chrome | chromedriver with `androidPackage=com.android.chrome`, `androidUseRunningApp` (WebDriver → CDP over ADB) | Selenium Manager at the device Chrome's major version (read with adb) | n/a |
| Android Edge | chromedriver with `androidPackage=com.microsoft.emmx` (spike) | same, at Edge's Chromium major | accessibility mode through the UiAutomator2 session (spike) |
| Android Firefox | geckodriver with `androidPackage=org.mozilla.firefox` (WebDriver → Marionette over ADB) | Selenium Manager | n/a |
| iOS Safari | XCUITest web view context (WebKit Inspector over usbmux/RemoteXPC) | Appium (existing) | n/a (Web Inspector setting required) |
| iOS Chrome | XCUITest web view context (`additionalWebviewBundleIds`) | Appium | accessibility mode if its Web Inspector setting is off |
| iOS Edge | XCUITest web view context (`com.microsoft.msedge`) | Appium | accessibility mode if not inspectable (D4) |
| iOS Firefox | XCUITest web view context (`org.mozilla.ios.Firefox`) | Appium | accessibility mode if not inspectable (D4) |

Why this family:

- Classic WebDriver is the only protocol all four vendors ship and keep
  stable. Safari has no BiDi, and CDP covers Chromium only.
- Appium is itself a WebDriver server, so the page operations
  (`execute_script`, find by CSS/XPath, click, send keys, window handles)
  are one code path for desktop, Android and iOS.
- On Android the page path does not touch the native UiAutomator2 session,
  so native tools and screenshots cannot be left in a web context.

### Open and attach (D8, D10)

**One launcher, two callers.** On desktop the Test Runner has one browser
launcher (`controller_client/web_browser_launch.py`). It starts Chrome, Edge
or Firefox as a detached process that outlives any driver session:

- Chrome / Edge: `--user-data-dir=<runner>/web_profiles/<browser>` and
  `--remote-debugging-port=0`, bound to 127.0.0.1;
- Firefox: `-profile <runner>/web_profiles/firefox -marionette -no-remote`
  with `marionette.port` 0 in the profile's `user.js`.

It has two callers:

- **The MCP client:** `browse_open(browser, url?)` asks the runner to run
  the launcher, then attaches.
- **A person:** runs the same launcher from a shortcut that setup and
  self-update write next to `start-test-runner.sh|.cmd`:
  - `<base>/open-test-browser.sh|.cmd <chrome|edge|firefox> [url]`;
  - it calls `python -m controller_client.web_browser_launch` in the
    runner's venv;
  - it works whether the runner is running or not.

So a person-opened browser and an agent-opened browser are the same thing:
the dedicated profile (D10) with remote control on.

Why this design:

- It is the only way a person-opened desktop browser can be attached at
  all. Chrome 136+ ignores remote debugging on the everyday profile, and
  Firefox needs Marionette at start.
- It reuses the persistent profile, so logins persist.
- It needs no runner-side registry: discovery reads the browser's own port
  file. Chrome writes `DevToolsActivePort` into the profile and Firefox
  writes `MarionetteActivePort`, and the port is probed before use.
- Closing a driver session never closes the person's browser.

Rejected alternatives:

- **A button in the person window.** That window is optional
  (`CONTROLLER_PERSON_WINDOW`) and Tk-only.
- **Asking people to add flags themselves.** Error-prone, and Chrome would
  still refuse the default profile.

**What "open and controllable" means, per cell** (the runner's
`web_browser_presence.py` answers it on every call, cheaply):

| Cell | Who can open it | Open means | Not attachable when |
|---|---|---|---|
| Desktop Chrome / Edge | `browse_open` or the shortcut | the dedicated profile's `DevToolsActivePort` port answers `/json/version` | started any other way (everyday profile, Start menu) |
| Desktop Firefox | `browse_open` or the shortcut | the profile's `MarionetteActivePort` port accepts a connection | started any other way |
| macOS Safari | `browse_open` only | the runner's safaridriver session is alive | a person's own Safari window: never (safaridriver cannot attach) |
| Android Chrome / Edge | `browse_open` (starts the app, then attaches) or a person opening the app normally | the app's process runs and its DevTools socket is listed by ADB | Edge without a DevTools socket: attachable in accessibility mode only (spike) |
| Android Firefox | `browse_open` only | the runner's geckodriver session is alive | a person-opened Firefox: never (geckodriver starts its own) |
| iOS Safari / Chrome / Edge / Firefox | `browse_open` (launches the app) or a person opening the app normally | the app is running (XCUITest app state). Page mode if an inspectable web view of it exists, else accessibility mode | never unreachable while running; at worst accessibility mode (D4) |

**Which browser a call acts on** (every tool except `browse_open`):

1. With `browser` named: that browser, if it is open and controllable.
   Otherwise `[browser_not_open]`, naming how to open it (`browse_open`,
   or the shortcut or the app for a person).
2. Without `browser`: the single open, controllable browser in this
   target's table.
3. None open → `[browser_not_open]`.
4. Several open → `[browser_ambiguous]`, listing them (for example
   "chrome, firefox"); the caller repeats the call with `browser`.

No tool other than `browse_open` ever starts or brings up a browser. On a
phone, acting on an open browser does bring it to the front
(`activate_app`); that is not a launch. `browse_open` on a browser that is
already open attaches to it (and navigates if `url` is given) instead of
starting a second one.

**`browse_close(browser?)`** (same choice rule):

- desktop Chrome, Edge and Firefox: quits the browser process from the
  dedicated profile;
- macOS Safari and Android Firefox: ends the runner's session, which
  closes that browser;
- Android Chrome/Edge and iOS: only detaches; the app keeps running (the
  existing `close_app` tool closes it).

### Data model

None. No migrations.

### Protocol (`controller_client/protocol.py`)

- **`WebBrowser` StrEnum** (separate from the Playwright `BrowserName`,
  which stays unchanged): `chrome`, `edge`, `firefox`, `safari`.
  `WEB_BROWSER_DISPLAY_NAMES`.
- **One support table**:
  `web_browsers_for(target: RunnerTarget, host_os: str) -> frozenset[WebBrowser]`
  encodes D2: Windows {chrome, edge, firefox}, Linux {chrome, firefox},
  Darwin {safari, chrome, firefox}, android {chrome, edge, firefox},
  ios {safari, chrome, edge, firefox}. Every gate and the runner hub use
  this one definition. There is no default browser (D8).
- **Capability `WEB_BROWSER_V1`**, CLIENT_VERSION **0.22.0**, advertised by
  every build on every target. Not required at handshake. Older runners get
  `[machine_outdated]` from the server before any round trip.
- **New messages** (server → runner; replies `web_browser_result`, carrying
  `success`, `content` (text or JSON), `mode` (`page` | `accessibility`),
  `duration_ms`):
  - `web_browser_open {browser, url?}`: `browser` is required. The runner
    launches the browser (desktop launcher, phone app, safaridriver or
    geckodriver session), or attaches when it is already open. It answers
    the version and mode. Other open browsers are left alone.
  - `web_browser_list {}`: for every browser in the target's table it
    answers `installed`, `open`, `mode` (`page` | `accessibility` | none)
    and `how_to_open` (`shortcut` | `app` | `open_tool_only`).
  - Every message below carries an optional `browser`. The runner picks
    the browser by the rule in Open and attach, and answers
    `WEB_BROWSER_NOT_OPEN` or `WEB_BROWSER_AMBIGUOUS` (with `details` = the
    open browsers, comma-separated enum values) when it cannot.
  - `web_browser_navigate {url}`, `web_browser_history {action}`,
    `web_browser_tabs {action: list|switch|close|new, index?}`.
  - `web_browser_elements {scope_css?, max}` returns an indexed element list
    (stamps `data-ph-idx`, its own attribute, never the Playwright one).
  - `web_browser_act {action: click|double_click|hover|type|clear|select|press_key, target, text?, value?, key?}`.
    `target` is exactly one of `{index}`, `{css}`, `{xpath}`; a description
    is resolved to an index on the server.
  - `web_browser_read {what: text|url|title|element_text|attribute, target?, name?}`
    (page text capped by `max_chars`).
  - `web_browser_wait {target, state: present|visible|gone, timeout_ms}`.
  - `web_browser_evaluate {script, args_json, timeout_ms}` (below).
  - `web_browser_close {}`: behaviour per cell is in Open and attach.

  Per-message parse limits live in the protocol, as for the other
  payloads: URL ≤ `OPEN_URL_MAX_CHARS`, selector ≤ 2 000 chars, typed text
  ≤ 10 000, script ≤ 20 000, args JSON ≤ 20 000, `timeout_ms` ≤ 10 000.
- **Error codes**:
  - `WEB_BROWSER_SETUP_NEEDED`, with `details` = one `WebBrowserSetupStep`:
    - `safari_remote_automation_off`;
    - `ios_web_inspector_off`;
    - `chrome_ios_web_inspector_off`;
    - `driver_unavailable` (Selenium Manager could not resolve the driver,
      or it is offline with no cached driver);
    - `android_devtools_unavailable` (no debuggable socket for the app);
    - `firefox_android_remote_debugging_off`.
  - `WEB_BROWSER_UNSUPPORTED`: the action does not exist in this mode or
    cell (e.g. hover on a phone, evaluate in accessibility mode).
  - `SCRIPT_FAILED`: the page script threw an error or timed out; the
    message carries the JS error class only.
  - `WEB_BROWSER_NOT_OPEN` (`details` = the named browser or empty) and
    `WEB_BROWSER_AMBIGUOUS` (`details` = the open browsers).
  - Existing `BROWSER_NOT_INSTALLED` (the app or browser is absent),
    `ELEMENT_NOT_FOUND`, `DEVICE_DISCONNECTED`.
- URL rule for `web_browser_open` / `navigate`: refuse `javascript:`,
  `data:`, `vbscript:` on every target (evaluate is the script path), and
  `_REFUSED_URL_SCHEMES` on phones.

### Runner (`controller_client/`): the new layer

Nothing here imports Playwright or `browser_executor.py`, and nothing
mentions MCP.

- `web_browser_backend.py`: the `WebBrowserBackend` Protocol: `open`,
  `navigate`, `history`, `tabs`, `elements`, `act`, `read`, `wait`,
  `evaluate`, `close`, `mode`, `supports(op)`.
- `web_browser_page_ops.py`: page operations over any Selenium/Appium
  `WebDriver` in page mode:
  - its own element script (`web_browser_page_script.py`): visible
    interactive elements across open shadow roots, plus same-origin
    iframes one level deep through `switch_to.frame`;
  - CSS/XPath lookup, scoped to the frame of the last listing;
  - click and double click via W3C actions, hover (desktop), fill via
    clear + `send_keys`, `<select>` via `Select`, key press via actions;
  - text and attributes, waits via `WebDriverWait` capped by `timeout_ms`;
  - tabs via `window_handles`.

  Element-not-found and not-actionable texts are fixed.
- `web_browser_evaluate.py`: the evaluate rules, shared by every page-mode
  backend:
  - the script runs as a function body through `execute_script(script,
    *args)`, with `set_script_timeout(timeout_ms)`;
  - the result is serialised to JSON on the runner (DOM nodes become
    `{tag, id, text≤100}`, cycles and functions become `null`), then
    capped at `WEB_EVAL_RESULT_MAX_BYTES` (64 KiB) with a `truncated` flag;
  - the script and the result are never logged above debug (they may hold
    test credentials).
- `web_browser_launch.py`: the desktop launcher (Open and attach). It is
  also a `__main__` entry point for the shortcuts.
  - Each browser's executable is found by a plain lookup: the standard
    install paths per OS, then PATH. Missing → `BROWSER_NOT_INSTALLED`.
  - It starts the browser detached with the dedicated profile
    `<runner>/web_profiles/<browser>/` (D10; excluded from the ZIP and the
    update bundle; kept across self-updates by moving it with `.env` in
    `update_installer`) and remote control on 127.0.0.1.
  - If the profile's browser is already running it opens a new window in
    that instance (Chrome and Edge hand over to it) or answers "already
    open" (Firefox, `-no-remote`).
- `web_browser_presence.py`: answers "which browsers are open and
  controllable" per cell, as defined in Open and attach:
  - desktop: probes the port files;
  - Android: `adb shell pidof` plus the DevTools socket list from
    `/proc/net/unix`;
  - iOS: `query_app_state` through `MobileSession.call`, plus the web view
    contexts;
  - Safari and Android Firefox: whether the runner's session is alive.

  Results are cached for 2 s, so one MCP call (list, then act) checks once.
- `web_browser_desktop.py`: Selenium sessions on desktop.
  - Chrome and Edge attach via `debuggerAddress`. Firefox attaches via
    geckodriver `--connect-existing --marionette-port`. Safari starts a
    `webdriver.Safari` session (attaching is not possible).
  - Selenium Manager resolves the drivers (D9). With
    `CONTROLLER_WEB_DRIVER_DOWNLOAD=false` it uses PATH or its cache only.
  - Page-load strategy `eager`, so navigation answers within budget.
  - A session whose browser went away is dropped; the next call reports
    `WEB_BROWSER_NOT_OPEN`.
- `web_browser_android.py`: chromedriver or geckodriver started by the
  runner for the device (`androidDeviceSerial` = the runner's device).
  - The app's version is read with `adb shell dumpsys package`. Selenium
    Manager resolves chromedriver at that major version.
  - Chrome and Edge attach to the running app (`androidUseRunningApp`), so
    the person's tabs and logins stay. `browse_open` first starts the app
    (`am start`, with the URL when given).
  - Firefox: `browse_open` starts a geckodriver session, which starts
    Firefox with its test profile (D7).
  - The native UiAutomator2 session is never touched.
  - Accessibility mode (Android Edge without a DevTools socket, if the spike
    shows it): through `MobileSession.call`, it reads `WebView`
    descendants from the native page source and taps or types on them.
- `web_browser_ios.py`: web view contexts of the existing XCUITest
  `MobileSession`.
  - Every operation runs inside `MobileSession.call` (action lock, 24 s
    budget, recreate on loss): bring the app to the front, pick the
    context whose bundle id matches the browser, run the page op, and
    **switch back to `NATIVE_APP` in `finally`**.
  - Capabilities added at session creation: `includeSafariInWebviews`,
    `additionalWebviewBundleIds` (Chrome, Edge, Firefox ids),
    `enableWebviewDetailsCollection`, `webviewConnectTimeout`. A 0.22.0
    runner's first session has them.
  - Accessibility mode when no inspectable context exists for the
    foreground browser: XCUITest elements under the app's
    `XCUIElementTypeWebView` (links, buttons, static texts, text fields)
    become the element list. Tap and type use the element, the URL comes
    from the address bar element, and CSS/XPath-in-page, evaluate, hover
    and tabs answer `WEB_BROWSER_UNSUPPORTED`.
  - The mode is re-detected on every `open` and `navigate`, so a person who
    turns Web Inspector on does not need a restart.
- `web_browser_hub.py`: the one owner the client talks to.
  - It picks the backend for `(target, host OS, browser)` from
    `web_browsers_for`, and the browser by the rule in Open and attach
    (through `web_browser_presence`).
  - It keeps one driver session per open browser and drops a session whose
    browser is gone. Several browsers may be open at once.
  - It holds the element listing (index → frame or native element) and
    answers `BROWSER_NOT_INSTALLED` for a browser that is absent.
  - Calls run on their own single worker thread, `web-browser`, separate
    from `playwright`, so the two stacks never block each other.
  - Every reply fits the runner budget (24 s; `open` 80 s, because a first
    driver download or a phone attach is slow). A still-starting driver
    answers a retryable error, never a late reply.
- `client.py`:
  - handlers for the new messages;
  - `web_browser_*` is not in `_DESKTOP_ONLY_MESSAGE_TYPES`;
  - the person window still hides before every action
    (`hidden_for_action`);
  - new exceptions in `exceptions.py` map to the new codes.
- `cleanup.py` and `MobileBackend.stop()` (self-update) end the hub's
  driver sessions. Cleanup also closes desktop browsers started from the
  dedicated profiles, as `browse_close` does; it does not close phone apps
  (the existing device cleanup does). The ZIP and update bundle exclude
  `web_profiles/`; the side-by-side update moves it to the new folder, as
  it does with `.env`.
- Shortcuts: `update_layout` (and the setup scripts) write
  `<base>/open-test-browser.sh|.cmd` next to the start script, for desktop
  targets only. No other OS integration (no desktop icons).
- `diagnose --web-browsers` prints one line per cell for this runner's
  target and OS:
  - installed and version;
  - driver resolution;
  - one launch (shortcut path), attach, list and close round trip;
  - the mode;
  - the setup hint per `WebBrowserSetupStep`.
- `example.env`: `CONTROLLER_WEB_DRIVER_DOWNLOAD`.

### Services (server)

- `projects/controller_web_browser_actions.py` (new leaf module): one
  function per message, with explicit timeouts (30 s; `open` 90 s) and
  typed results (`WebBrowserResult`).
- `projects/controller_protocol.py`: event → `MessageType` and the allowed
  fields of the new messages. `projects/controller_consumer.py`: the
  forwarders.
- `projects/services.py::_raise_for_error_reply`: new codes →
  `WebBrowserSetupNeededError(setup_step)`, `WebBrowserUnsupportedError`,
  `WebBrowserScriptError`, `WebBrowserNotOpenError(named)`,
  `WebBrowserAmbiguousError(open_browsers)` (new, `projects/exceptions.py`;
  the browser lists are parsed back into `WebBrowser` values, never runner
  text; plain
  `ControllerActionError` subclasses: not agent infrastructure faults, and
  the agent never sees them). The `BROWSER_NOT_INSTALLED` and
  `ELEMENT_NOT_FOUND` branches are unchanged.
- `projects/runner_capability_services.py`: pure helper
  `web_browser_check(capabilities, target, host_os, browser) -> WebBrowserCheck`
  (ok / outdated / unsupported).

### MCP surface (`mcp_gateway`)

All tools take `project_id`, `pivot_id`, `claim_token` and run inside the
caller's own live MCP claim through the existing gate.

| Tool | Args (besides claim) | Targets | Kind |
|---|---|---|---|
| `browse_open` | `browser` (required), `url?` | all | CONTROL (own deadline) |
| `browse_list_browsers` | none | all | CONTROL |
| `browse_navigate` | `url` | all | CONTROL |
| `browse_history` | `action` back/forward/reload | all | CONTROL |
| `browse_tabs` | `action` list/switch/close/new, `index?` | all (page mode) | CONTROL |
| `browse_list_elements` | `scope_css?` | all | CONTROL |
| `browse_click` / `browse_double_click` | one of `index` / `description` / `css` / `xpath` | all | AI with description, else CONTROL |
| `browse_hover` | target | desktop | same |
| `browse_type` | target, `text`, `clear?` | all | same |
| `browse_select` | target, `value` or `label` | all (page mode) | same |
| `browse_press_key` | `key` | all | CONTROL |
| `browse_read` | `what`, `target?`, `name?` | all | CONTROL |
| `browse_wait` | target (no description), `state`, `timeout_ms?` | all (page mode) | CONTROL |
| `browse_evaluate` | `script`, `args?` (JSON) | all (page mode) | EVAL (new throttle) |
| `browse_close` | none | all | CONTROL |

Every tool except `browse_open` and `browse_list_browsers` also takes an
optional `browser`, resolved by the rule in Open and attach.

These are 16 new tools: 52 machine tools, 67 in all. The asserted count
is updated, and `MCP_AGENT_TOOL_NAMES` is unchanged (none dispatch to the
agent).

- `machine_tools.py`:
  - the rows use `dispatches=False` and `uses_visual_engine=False`;
  - `effective_kind(tool, arguments)` gains an `ai_with_description` flag,
    next to `ai_with_expected_result`;
  - new `CallKind.EVAL`, with its own throttle (e.g. 30/min per claim).
- Services, one use case each:
  - `services/browse_choice.py`: `check_web_browser(session, requested)`,
    with no query (the gate already loaded capabilities and the OS) and no
    server state. It does not pick a browser: which browser is open is only
    known on the test machine, so the runner applies the D8 rule. The
    checks:
    - no `WEB_BROWSER_V1` → `[machine_outdated]`;
    - a named browser outside the table → `[browser_unavailable]`.
  - `services/machine_call.py`: the slot, lock, no-reconnect and deadline
    block, as a **new** helper. `run_agent_tool.py` is left as is (D1), so
    the small duplication is accepted. A later plan may point
    `run_agent_tool` at it.
  - `services/browse_actions.py`: one function per tool. It validates the
    target, resolves descriptions, calls the `projects` seam, fences
    `content` as data and adds the `mode` line ("Page mode" /
    "Accessibility mode: selectors and scripts are not available here").
  - `services/browse_element_pick.py`: plain words → index over the listed
    elements with one LLM call, through `agents.services.llm_client` and
    `llm_config`. Its own small prompt; the agent's
    `browser_element_finder` stays untouched (D1). Ambiguous or not found →
    `[element_not_found]` with the reason in neutral words.
  - `services/browse_evaluate.py`: the evaluate guard:
    - checks the script and args sizes and that `args` is valid JSON
      (≤ `MCP_BROWSE_SCRIPT_MAX_CHARS`, `MCP_BROWSE_ARGS_MAX_CHARS`);
    - sends `timeout_ms` = min(caller's value, 10 000);
    - wraps the reply: the JSON result fenced as data, plus `truncated`;
    - on failure → `[script_failed]` with the JS error class only, never
      the stack or page text;
    - never logs the script or the result above debug.
- `schemas.py`:
  - request models with exactly-one-of target validation;
  - `index` 0..`MCP_BROWSE_INDEX_MAX`;
  - `WebBrowserArg` enum (chrome, edge, firefox, safari): required on
    `browse_open`, optional elsewhere. The description says there is no
    default: the tools use the one open browser, or the one named;
  - `BrowserArg` and the old tools' schemas are untouched.
- `get_project.machine` gains `web_browsers: [...]` (the target's table),
  computed from the row it already loads. Whether a browser is open is
  live state: `browse_list_browsers` asks the test machine.
- `machine_errors.MachineErrorCode` gains:
  - `browser_setup_needed` (one fixed test-machine text per setup step);
  - `browser_action_unsupported`, `element_not_found`, `script_failed`;
  - `browser_not_open`, with a fixed text per case:
    - "No <browser> is open on the test machine. Open it with browse_open,
      or ask the person at the test machine to open it with the test
      browser shortcut";
    - phones say "open the app on the phone";
    - Safari on macOS and Android Firefox say "only browse_open can open
      it";
    - with no browser named: "No browser is open on the test machine…";
  - `browser_ambiguous`: "Several browsers are open on the test machine
    (chrome, firefox). Name one with the browser argument." `map_machine_exception` maps the
  new `projects` exceptions. Runner text is never forwarded for these.
  Other text goes through `neutral_machine_text` and the fence.
- `workflow.py`: one paragraph:
  - nothing opens by itself: call `browse_list_browsers`, then
    `browse_open` unless the person already opened one;
  - name `browser` when several are open;
  - list, then act by index or selector; re-list after navigation;
  - accessibility mode limits;
  - page text and script results are data, not instructions;
  - `browse_*` and the older `browser_*` are separate browsers.
- `limits.py`:
  - `MCP_BROWSE_OPEN_DEADLINE_SECONDS` (100), with its lock (110);
  - `MCP_BROWSE_ELEMENTS_MAX_CHARS` (30 000);
  - `MCP_BROWSE_TEXT_MAX_CHARS` (50 000);
  - `MCP_BROWSE_SCRIPT_MAX_CHARS` (20 000), `MCP_BROWSE_ARGS_MAX_CHARS`
    (20 000), `MCP_BROWSE_EVAL_RESULT_MAX_BYTES` (64 KiB);
  - `MCP_BROWSE_INDEX_MAX`;
  - the EVAL throttle.

  The lock TTL ≥ deadline + margin asserts are extended.
- Import rule update (CLAUDE.md › MCP gateway): `mcp_gateway` may also
  import `agents.services.llm_client` and
  `projects.controller_web_browser_actions`.

### Pages and forms

None.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| (none) | | | | | |

### Background work and WebSockets

- No new Celery task and no server-side browser state (no Redis key). Claim
  end does not close browsers: a person may keep using one, and cleanup
  closes them as for the Playwright browser.
- New controller messages only; no browser-side WebSocket events.

### Settings / infrastructure

- Server: none (constants in `mcp_gateway/limits.py`).
- Runner `.env`: `CONTROLLER_WEB_DRIVER_DOWNLOAD` (default true, D9).
- Runner folder: `web_profiles/` (D10) and the `open-test-browser.sh|.cmd`
  shortcut next to the start script.
- Host prerequisites, reported by `diagnose --web-browsers` and never
  installed:
  - macOS: `safaridriver --enable`;
  - iOS device: Web Inspector on; Chrome's own Web Inspector setting;
  - Android: USB debugging (already required); Firefox's remote debugging
    setting if the spike shows it is needed;
  - the browsers themselves.

### Data flow (`browse_click(css=…)` on Android Chrome)

1. The MCP call goes through `guarded_call`, then `open_machine_session`.
   The gate checks the claim, connected, device ready and no readiness
   run, and loads the capabilities and OS.
2. `check_web_browser`: `WEB_BROWSER_V1` is advertised, and there is no
   named browser to check.
3. `machine_call` takes the slot, lock and deadline, then
   `controller_web_browser_act(target={css}, browser=None)` sends the
   message.
4. Runner: the hub asks `web_browser_presence`. The person had opened
   Chrome on the phone: its process runs and its DevTools socket is listed;
   no other table browser is open. So the hub uses chrome and attaches
   chromedriver (`androidUseRunningApp`), or reuses its session. Then
   `find_element(css)` → W3C click → reply `mode=page`. Had Firefox also
   been open: `WEB_BROWSER_AMBIGUOUS` with `chrome,firefox`. Had nothing
   been open: `WEB_BROWSER_NOT_OPEN`.
5. The server fences the reply, adds the mode line and answers. With
   `description` instead: `web_browser_elements`, then the LLM pick, then
   `web_browser_act(index)`, all under the same lock and AI throttle.

## Alternatives considered

- **Extend the Playwright tools to phones and Safari** (the first draft).
  Rejected by D1.
- **CDP directly for Chromium cells, BiDi for Firefox.** Rejected as the
  primary path: it gives three protocol stacks, and Safari speaks neither.
  chromedriver already speaks CDP to the browser, so a direct CDP hop adds
  nothing for these tools. BiDi stays possible later on top of the same
  Selenium sessions.
- **Android Chromium through Appium web contexts** of the native session.
  Rejected: a context left active breaks native screenshots. chromedriver
  with `androidPackage` reaches the same DevTools socket over ADB without
  touching the native session.
- **iOS Safari through `safaridriver` on the Mac** (`platformName: iOS`).
  Rejected:
  - it puts the device into an automation session (the person cannot use
    it, and native XCUITest gestures conflict);
  - it covers Safari only;
  - the XCUITest WebKit Inspector path serves all four iOS browsers.
- **ios-webkit-debug-proxy.** Not needed: Appium XCUITest talks to the
  inspector service itself, including over RemoteXPC on iOS 18+.
- **Share the element script or finder with the Playwright and agent code.**
  Rejected by D1: the copy is small (one JS string and one prompt), and
  sharing would mean editing the agent's code.
- **A default browser or auto-launch** (earlier drafts: a per-target
  default, then "the claim's open browser, else the platform default").
  Rejected by D8: the tools act only on an open browser.
- **Server-side record of the open browser** (a Redis key per claim).
  Rejected: a person can open or close browsers at any time, so only the
  test machine knows. The runner decides per call from live presence.
- **A runner registry file for launched browsers.** Rejected: Chrome's
  `DevToolsActivePort` and Firefox's `MarionetteActivePort` already record
  the port inside the dedicated profile, and they also cover browsers a
  person opened with the shortcut while the runner was down.

## Risks and mitigations

- **Spike-dependent cells**: Android Edge (DevTools socket), Android
  Firefox (device settings), iOS Edge and Firefox (`isInspectable`), iOS
  Chrome (setting). Mitigation:
  - Step 0 records each result;
  - a cell that cannot reach page mode runs in accessibility mode, so it
    stays in scope per D2, with fewer tools;
  - the tool reply always states the mode.
- **Evaluate** runs arbitrary JavaScript in the page under test, on the
  test machine.
  - It is accepted by D6, in the same trust class as `execute_command`
    (D1 of the machine-tools plan).
  - Bounds: size caps, a 10 s script timeout inside the runner budget, a
    result cap, the result fenced as data, an EVAL throttle, no debug
    logging of the content, and only in page mode on the claim's own test
    machine.
  - It cannot reach the server: the server never evaluates anything and
    fetches no URL.
- **Prompt injection** through page text, element lists and script
  results: all fenced and capped. Element text is cut to 100 chars.
- **iOS native context leak** (web context left active). Mitigation:
  - the switch back happens in `finally` under the session lock;
  - a recreated session starts in native;
  - Step 0 measures it, and `diagnose` checks it.
- **Timing**: a first driver download or phone attach can be slow.
  Mitigation: `browse_open` has its own longer deadline, the other calls
  answer "still starting" within budget, and `diagnose --web-browsers`
  warms the Selenium Manager cache.
- **Selenium Manager downloads binaries** (Chrome for Testing, Microsoft,
  Mozilla CDNs) onto the host. Mitigation: a `.env` switch and the cache
  directory (D9). It does not need Appium's insecure features.
- **Desktop Safari automation window**: separate from the person's windows;
  touching it may end the session. This is documented in the tool
  description; the session closes on `browse_close` and cleanup.
- **Remote control port on desktop**: a launcher-started browser listens on
  127.0.0.1 (DevTools or Marionette). Any process of the same OS user could
  drive it while it runs, the same trust level as the runner itself.
  - It is bound to loopback only, and used only with the dedicated profile,
    never the person's everyday one; Chrome enforces the latter anyway.
  - Firefox shows its remote-control look, so the person can see it.
- **Person-opened browsers can change under the tools.** A person may close
  a browser or open a second one between calls. Mitigation:
  - presence is checked on every call (2 s cache), so a closed browser
    gives `[browser_not_open]` and a second one gives
    `[browser_ambiguous]`;
  - the runner never guesses.
- **Not attachable by design**: a person's own Safari on macOS, a
  person-opened Firefox on Android, and any desktop browser started outside
  the shortcut. Mitigation: `browse_list_browsers` says `how_to_open`, and
  the `[browser_not_open]` texts name the way to open each.
- **Stale port files** (a crash leaves `DevToolsActivePort` /
  `MarionetteActivePort`): the port is always probed, and a dead one counts
  as not open. The launcher removes a stale `MarionetteActivePort` before
  starting Firefox (Mozilla bug 1755312).
- **Two browser stacks on one desktop** (Playwright `browser_*` and
  `browse_*`): separate profiles and processes, so no shared state. Both
  close on cleanup.
- **Persistent profiles hold test logins** on the test machine (D10). They
  stay under the runner folder, and no cookie or storage content is ever
  returned except through evaluate.
- **Controller compatibility**: older runners get `[machine_outdated]` from
  the server, before any round trip. A 0.22.0 runner on an older server is
  simply never sent the new messages.
- **XSS, secrets, N+1**:
  - no templates are touched;
  - Connection keys and OAuth are untouched;
  - browser checks use only the gate's one row read; no extra query and
    no Redis.

## Implementation steps

Workflow per `.claude/rules/workflow.md`. Step 1 is shared code written
before the parallel parts.

0. **Device test (spike)**, with throwaway scripts outside the repo. Run it
   on Windows, Linux and macOS desktops, an Android emulator plus a real
   Android device, and an iOS Simulator plus a real iPhone (iOS 18+). Per
   cell, check:
   - open, list, click by CSS, type, evaluate, tabs;
   - Android Edge's DevTools socket and Android Firefox's required
     settings;
   - **iOS Edge and Firefox inspectability on current App Store builds**
     (does a context appear with `additionalWebviewBundleIds`?);
   - iOS Chrome with the setting on and off;
   - the accessibility-mode element quality where page mode fails;
   - native screenshots right after each iOS call;
   - attach: Chrome and Edge started with the dedicated profile and port
     0 (`DevToolsActivePort` written, `debuggerAddress` works; Edge honours
     the same policy);
   - Firefox with `marionette.port` 0 (`MarionetteActivePort` written,
     `--connect-existing` works);
   - Android Chrome/Edge opened by hand then attached with
     `androidUseRunningApp`;
   - iOS apps opened by hand then found.

   Record the results in this plan's Changelog and adjust the table's
   "mode" column.
1. **Shared protocol** (orchestrator) (files: `controller_client/protocol.py`):
   - `WebBrowser`, `web_browsers_for`;
   - `WEB_BROWSER_V1`, CLIENT_VERSION 0.22.0;
   - the messages (including `web_browser_list` and the optional
     `browser`), payloads and parsers with their limits;
   - the error codes (including `WEB_BROWSER_NOT_OPEN` /
     `WEB_BROWSER_AMBIGUOUS`) and `WebBrowserSetupStep`.
2. **Runner page layer and desktop** (sub-agent A) (files:
   `controller_client/web_browser_backend.py`, `web_browser_page_ops.py`,
   `web_browser_page_script.py`, `web_browser_evaluate.py`,
   `web_browser_launch.py`, `web_browser_presence.py` (desktop part),
   `web_browser_desktop.py`, `web_browser_hub.py`, `exceptions.py`).
3. **Runner phones** (sub-agent B) (files:
   `controller_client/web_browser_android.py`, `web_browser_ios.py`,
   `web_browser_presence_mobile.py` (the phone part of presence; A owns
   the desktop module, so no file is shared), `mobile_ios.py`
   (capabilities), `mobile_backend.py` (`stop()` ends the hub's phone
   sessions)).
4. **Runner wiring, diagnose, packaging** (orchestrator after A+B) (files:
   `controller_client/client.py`, `cleanup.py`, `diagnose.py`, new
   `web_browser_diagnostics.py`, `config.py`, `example.env`,
   `update_archive.py` / the ZIP builder and `.dockerignore` excludes for
   `web_profiles/`, `update_installer.py` (move `web_profiles/` with
   `.env`), `update_layout.py` and `controller_client/scripts/setup.{sh,ps1,bat}` (write
   `open-test-browser.sh|.cmd`)).
5. **Server seams** (sub-agent C) (files:
   `projects/controller_web_browser_actions.py`,
   `projects/controller_protocol.py`, `projects/controller_consumer.py`,
   `projects/services.py` (`_raise_for_error_reply` branches only),
   `projects/exceptions.py`, `projects/runner_capability_services.py`).
6. **MCP surface** (sub-agent D) (files: `mcp_gateway/machine_tools.py`,
   `machine_handlers.py`, `schemas.py`, `limits.py`, `workflow.py`,
   `services/call_guard.py` (`CallKind.EVAL`), `services/machine_call.py`,
   `services/browse_choice.py`, `services/browse_actions.py`,
   `services/browse_element_pick.py`, `services/browse_evaluate.py`,
   `services/machine_errors.py`, `services/read_project.py`).
7. **Integration and docs** (orchestrator):
   - `pre-commit run --all-files` and both mypy configs;
   - CLAUDE.md › MCP gateway (tool count 67, `browse_*` paragraph, import
     rule) and › controller_client (`WEB_BROWSER_V1`, 0.22.0, the layer);
   - `docs/changed/<date>-mcp-browse-tools.md`.

## Verification

No automated tests in this project.

- Checks:
  - `pre-commit run --all-files`;
  - `mypy .`;
  - `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`;
  - `python manage.py check`;
  - `scripts/verify_server_image.sh` where Docker exists.
- **Untouched code**: `git diff` shows no change to
  `controller_client/browser_executor.py`, `agents/`,
  `projects/browser_selection_services.py`, the
  `projects.services.controller_browser_*` functions, the nine `browser_*`
  rows and handlers, `BrowserArg`, `run_agent_tool.py`.
- **Playwright regression**: one run of the old `browser_navigate` /
  `browser_click` on desktop still works.
- **Open and attach (D8)**, on each target:
  - with nothing open, `browse_click` without `browser` →
    `[browser_not_open]`, and nothing is launched (check the process list
    or the phone);
  - `browse_list_browsers` shows installed, open, mode and how to open;
  - `browse_open` without `browser` → schema error;
  - desktop: a person runs `open-test-browser chrome`, then `browse_read`
    without `browser` attaches to it, with the dedicated profile's logins
    still there after closing and reopening;
  - the same with Firefox;
  - Chrome started normally from the Start menu or Dock → not attachable
    (`[browser_not_open]`);
  - Android: Chrome opened by hand on the phone, then `browse_read` works
    and the person's tabs are kept;
  - iOS: Safari opened by hand, then it works;
  - macOS Safari: only `browse_open` makes it usable;
  - two browsers open, no `browser` → `[browser_ambiguous]` listing both;
    with `browser` named → works;
  - the browser closed by hand between calls → `[browser_not_open]`;
  - `browse_open` on an already open browser attaches and does not start
    a second one;
  - the runner restarted while a shortcut-opened browser runs → attach
    still works (port file).
- **Per cell** (MCP Inspector, live MCP claim), for each of the 14 D2 cells:
  - `browse_open(browser)`, then `browse_navigate` to a test page;
  - `browse_list_elements`, then `browse_click` by index, by CSS, by XPath
    and by description;
  - `browse_type`, `browse_select`, `browse_press_key`;
  - `browse_read` text, URL and title;
  - `browse_wait`;
  - `browse_history` back, forward and reload;
  - `browse_tabs` list and switch;
  - `browse_evaluate("return document.title")`;
  - `browse_close`.

  Then `take_screenshot` works and shows the page (on iOS, right after a
  `browse_*` call).
- **Accessibility mode** (iOS Edge or Firefox build that is not
  inspectable, or with Web Inspector off): list, click, type and read work;
  CSS, XPath, evaluate and tabs → `[browser_action_unsupported]`; the reply
  says "Accessibility mode".
- **Refusals**:
  - `safari` on Windows → `[browser_unavailable]`;
  - Edge on Linux → `[browser_unavailable]`;
  - `browse_hover` on a phone → `[wrong_target]`;
  - a 0.21.0 runner → `[machine_outdated]`;
  - Safari before `safaridriver --enable` → `[browser_setup_needed]`;
  - with `CONTROLLER_WEB_DRIVER_DOWNLOAD=false` and no cached driver →
    `[browser_setup_needed]`;
  - a stale index → `[element_not_found]`.
- **Evaluate bounds**:
  - an over-size script → refused before any round trip;
  - `while(true){}` → `[script_failed]` within the deadline, and the
    runner keeps answering;
  - a 1 MB result → truncated with the flag;
  - a thrown error → error class only;
  - the script and result are absent from the runner and server logs at
    info.
- **No trace**:
  - `grep -ri mcp controller_client/` finds nothing new;
  - no `browse_*` error text names AI, MCP, an agent, Appium, Selenium or
    the Test Runner;
  - page content comes back fenced.
- **Claim end**: after hand-in the browser stays open. The next claim's
  `browse_*` attaches to it only when it is still open, the same rule as
  always.
- **Self-update**: `web_profiles/` moves to the new runner folder, and the
  shortcut is rewritten to point to it.
- `diagnose --web-browsers` on each OS and phone target lists its cells
  with the right hints.
- Then run `reviewer` and `verifier` in parallel (give `verifier` the
  verbatim request, D1–D10 and this plan path); loop until clean.

## Open questions

None. Step 0 can still change a cell's mode (page or accessibility) or its
attach path. Such a result is recorded in the Changelog and the tables; it
needs a new question only if a D2 cell would become unreachable even in
accessibility mode.

## Changelog

- 2026-11-08: created (Draft); first design extended the Playwright
  `browser_*` tools.
- 2026-11-08: revised with the user's answers (D1–D7):
  - separate `browse_*` tools on a new non-Playwright layer (W3C WebDriver
    family: Selenium + vendor drivers on desktop and Android, Appium
    XCUITest WebKit Inspector contexts on iOS);
  - full D2 matrix, with an accessibility-mode fallback;
  - CSS/XPath and evaluate in scope;
  - the agent unchanged;
  - earlier Q1–Q7 closed. Q4 (per-call browser) and Q6 (driver download)
    re-opened as Q1 and Q2 because the design changed. Q3 (profiles) is new.
- 2026-11-08: Q1–Q3 answered (D8–D10). Status Ready.
  - D8: no default browser and no auto-launch. `browse_open(browser)`
    (required) is the only tool that opens one; the others act on an open,
    controllable browser and fail `[browser_not_open]` or
    `[browser_ambiguous]` otherwise.
  - New section "Open and attach": one desktop launcher
    (`web_browser_launch.py`), used by `browse_open` and by the person's
    `open-test-browser.sh|.cmd` shortcut, with the dedicated profile and
    remote control on loopback. Discovery reads the browser's own
    `DevToolsActivePort` / `MarionetteActivePort`.
  - Per-cell attach table: a person's own macOS Safari, a person-opened
    Android Firefox and any desktop browser started outside the shortcut
    are not attachable, stated as such.
  - New tool `browse_list_browsers` (16 new, 67 in all); new runner
    messages `web_browser_list`, optional `browser` on every message, codes
    `WEB_BROWSER_NOT_OPEN` / `WEB_BROWSER_AMBIGUOUS`.
  - Removed the per-target default (`default_web_browser`) and the Redis
    claim key.
  - D9: Selenium Manager downloads on, `CONTROLLER_WEB_DRIVER_DOWNLOAD`
    default true.
  - D10: persistent profiles in `web_profiles/`, moved on self-update.

- 2026-11-08 (implementation, review round 1). Implemented without the Step 0
  device test (the user chose "Implement now"); every cell is unverified on
  real browsers and devices. Review/verify fixes:
  - profiles move by the new runner after the old one exits (`update_cleanup`,
    atomic rename) instead of during the update;
  - the shortcut is written on desktop targets only (`--write-shortcut` skips
    phones);
  - hub: daemon worker, busy refusal per stuck browser (at most 2 stuck
    calls), any non-ExecutionError becomes a fixed text, phone budget
    `WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS` (28), cleanup ends profile
    processes without attaching (≤ 9 s);
  - desktop sessions: HTTP timeout 20 s, page-load and script 15 s;
  - Android: page-load 15 s, read timeouts are not a lost session, no
    retries, chromedriver's adb forwards removed on detach; iOS: switch-back
    on every path, lost session recreated without repeating a started action;
  - MCP: new codes `browser_action_failed` (any other runner failure; runner
    text only in the server log) and `browser_out_of_time` (not sent when the
    call has too little time left); empty element listing →
    `element_not_found`; `file:` refused on phones before the round trip;
    whitespace-only typed text allowed.
  - Accepted, not fixed (known limits for Step 0): the loopback remote
    control port has no authentication (same trust as the runner's OS user);
    an iOS call makes two phone-session calls (presence + action), each with
    the session's 24 s, so a worst case can pass 28 s (the hub then refuses
    that browser until it returns; a deadline-aware `web_call` is a
    follow-up); an Android accessibility tap may still be repeated by the
    phone session's own recreate; a first Selenium Manager download on
    Android is bounded only by the hub's budget.
- 2026-11-08 (review round 2). Fixes: a stuck call only makes its browser
  busy until it returns (no session drop, so Safari / Android Firefox are
  never quit by a slow call); on phones any stuck call makes every browser
  busy (one phone session); `evaluate` restores the 15 s script timeout
  (`WEB_SCRIPT_TIMEOUT_SECONDS`); a cancelled handler (socket closed) is set
  aside like a timeout and a job abandoned before it starts is skipped;
  cleanup scans processes only when a profile's port file exists, Safari and
  Android Firefox quit with short timeouts and stop their driver process
  themselves, cleanup/shutdown count toward the stuck-call bound; desktop
  Selenium Manager gets `--timeout 30`; the new runner ends the old
  install's test browsers before moving the profiles. Accepted: with Chrome,
  Edge and Firefox sessions all open on Android, cleanup can take about
  12 s; the hub stops waiting at 9 s and logs.
- 2026-11-08 (review round 3). Fixes: the profile move finds a manual
  install's profiles (its old target is the `controller_client` folder);
  old-install test browsers are matched with the launcher's own
  `find_profile_processes`; desktop Safari keeps its session on a read
  timeout (`_DROP_ON_READ_TIMEOUT`); Android uses the shared
  `WEB_SCRIPT_TIMEOUT_SECONDS`. Accepted: on iOS, `evaluate` leaves the shared
  XCUITest session's script timeout at 15 s (Appium's default is 30 s);
  harmless for the current callers.
- 2026-11-08 (review rounds 4-5). `update_cleanup` never raises (guarded
  profile listing). Desktop Safari: one unanswered presence probe keeps the
  session, two in a row (about 40 s silent) end a wedged safaridriver; a
  timeout in the closed-tab path keeps the session; every session end resets
  the per-session state (`_drop` override). Accepted: if safaridriver is
  already wedged when `browse_open` runs, that call may briefly report
  "attached" before the session is ended; the next open starts a new one.

