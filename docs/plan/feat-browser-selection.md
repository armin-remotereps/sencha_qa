# feat: Run tests in any Playwright browser

Status: Done
Created: 2026-10-22
Request: "Add ability for all browsers on playwright". Today the Test Runner
always launches bundled Chromium, and the agent prompt says "Chromium browser is
available". Let a test case run in Chromium, Firefox, WebKit, Google Chrome or
Microsoft Edge on the test machine. The browser is named in the test case text
itself, and the agent picks it. Chromium is the default when the case names
none.

## Decisions (open questions resolved 2026-10-22)

- **Q1, where the choice lives:** in the **test case text** (steps or
  preconditions, as imported from TestRail). There is no run, project or
  case-level browser field, no Create-run dropdown and no wizard change.
- **Q2, who switches:** the **agent**. `browser_navigate` gains an optional
  `browser` enum argument. The choice is remembered on the server for the rest
  of the case, and sent on every browser action. It resets to Chromium at the
  start of each case.
- **Q3, older runners:** the capability is **optional**. Old (0.3.0) runners
  still connect. A non-Chromium request to an old runner blocks the case with
  the message "download a fresh Test Runner".
- **Q4, browsers offered:** five values: `chromium`, `firefox`, `webkit`,
  `chrome`, `msedge`.
- **Q5:** no browser inventory in the handshake. A missing browser shows up when
  the browser is first used, and the `diagnose --browsers` command helps fix it.
- **Q6:** no installer changes. Firefox and WebKit already come with the default
  `playwright install`. Chrome and Edge are documented as "install normally on
  the test machine".

## Context

- `controller_client/browser_executor.py`: `BrowserSession.ensure_page` launches
  `playwright.chromium.launch(headless=False, args=["--no-sandbox",
  "--disable-gpu"])` once, and keeps one context
  (`viewport 1280x720, accept_downloads=True`) and one page.
  - The rest is cross-engine Playwright API: element stamping
    (`_COLLECT_ELEMENTS_JS`, `data-at-idx`, open shadow roots), the per-frame
    index map (`page.frames`), downloads, `inner_text`, `screenshot`,
    `viewport_size`.
  - **There is no CDP and no Chromium-only API.** Only the `args` are
    Chromium-specific.
- `controller_client/cleanup.py`: `execute_cleanup` closes the browser before and
  after every case (`projects/services.py::_safe_cleanup`). A case therefore
  starts with no browser open, which matches "reset to Chromium per case" on
  the runner side.
- `controller_client/client.py`: one `BrowserSession` lives on
  `ControllerClient` and survives reconnects. There are ten browser handlers,
  each with its own except chain mapping errors to `EXECUTION_FAILED`,
  `ELEMENT_NOT_FOUND` or `SCREENSHOT_FAILED`. The protocol parsers ignore
  unknown keys, so a 0.3.0 runner silently ignores a new `browser` field.
- `controller_client/protocol.py`:
  - `CLIENT_VERSION = "0.3.0"`.
  - `REQUIRED_CLIENT_CAPABILITIES = frozenset(ClientCapability)`; its comment
    already anticipates optional capabilities.
- Server dispatch:
  - `projects/controller_protocol.py`: the `*ActionEvent` TypedDicts and
    `ActionTypeRegistry._PAYLOAD_KEYS` decide which keys are forwarded.
  - `projects/services.py`: the `controller_browser_*` functions,
    `_raise_for_error_reply`, and `execute_test_run_test_case` (which calls
    `run_orchestrator`).
- Agent:
  - `agents/types.py`: `ToolContext` is frozen. It is built once per sub-agent
    in `agents/services/sub_agent.py`, and in `agent_loop.py::run_agent`.
    `ToolParameter.enum` already exists, and `llm_serializer` emits it as a JSON
    schema `enum`.
  - Browser tools are in `agents/services/tools_controller.py` and
    `agents/services/browser_element_finder.py`, dispatched by
    `tool_registry.py`.
  - Tool schemas are in `tool_definitions.py::get_browser_tool_definitions`.
  - Prompts are in `prompt_parts.py` (`build_environment_context` hard-codes
    "Chromium browser is available" in three OS branches; the taxonomy and
    examples) and `orchestrator_prompts.py` (the planner that splits the case
    into sub-tasks).
- Blocked results (`docs/changed/2026-10-22-blocked-result.md`):
  - `InfrastructureError` subclasses propagate through `safe_tool_call`.
  - `projects/case_blocking.py` maps them to a `BlockedReason`, with safe text
    that is also pushed to TestRail.
- **Playwright 1.50.0** is pinned in `requirements.txt` and
  `controller_client/requirements.txt`. Checked in the installed driver
  (`browsers.json`, `lib/server/registry/index.js`):
  - The default install is chromium 133, chromium-headless-shell, firefox 134,
    webkit 18.2 and ffmpeg. The setup scripts' `playwright install --with-deps`
    therefore already installs Firefox and WebKit.
  - The `chrome` and `msedge` channels are looked up only at the system install
    path (`/opt/google/chrome/chrome`, `/Applications/Google Chrome.app/...`,
    `%LOCALAPPDATA%`/`%PROGRAMFILES%`...). Edge ships with Windows 10/11.
    `playwright install chrome|msedge` reinstalls the system browser (macOS:
    `rm -rf` the app, Windows: msiexec with admin, Linux: apt with root), so we
    never run it.
  - Errors when a browser is missing:
    - bundled engine: `Executable doesn't exist at <path> ...`;
    - channel: `Chromium distribution '<name>' is not found at <path>\nRun "playwright install <name>"`.
  - `chromium_sandbox` defaults to False, so Playwright already adds
    `--no-sandbox` for Chromium.
  - WebKit gets no updates on Ubuntu 20.04 / Debian 11 since 1.49. WebKit on
    Windows is Playwright's own port and has gaps in media and other features.

## Goals

- A test case that names one of the five browsers runs its web steps in that
  browser. Every other case runs in Chromium, as today.
- The browser chosen by the agent persists across all sub-agents and steps of
  one case, and resets to Chromium for the next case.
- A missing browser, or a runner too old to switch, blocks the case with a clear
  reason rather than failing it.
- `python -m controller_client.diagnose --browsers` checks the browsers on the
  test machine.
- 0.3.0 runners keep working for Chromium cases.

## Non-goals

- Any persisted browser setting: on the run, the project or the case model.
- A cross-browser matrix (one case run across several browsers).
- Beta, dev and canary channels; headless mode; `executable_path`; Firefox
  BiDi.
- Installing Chrome or Edge from the setup scripts.
- A browser inventory in the handshake.
- Reporting the browser in TestRail comments. The agent summary mentions it.

## Design

### Data model

- No new model field.
- `projects/models.py`: `BlockedReason.BROWSER_UNAVAILABLE = ("browser_unavailable",
  "Browser unavailable on the test machine")`.
- Migration `projects/migrations/0019_blocked_reason_browser.py`:
  `AlterField TestRunTestCase.blocked_reason`. It changes choices only, emits no
  SQL, and is reversible. Before reverting, update rows that use the new value.

### Shared protocol vocabulary (`controller_client/protocol.py`)

These are written first; every part depends on them.

- `class BrowserName(StrEnum)`: `CHROMIUM = "chromium"`, `FIREFOX = "firefox"`,
  `WEBKIT = "webkit"`, `CHROME = "chrome"`, `MSEDGE = "msedge"`. The values are
  Playwright's own names; `chrome` and `msedge` go straight to `channel=`.
- `class BrowserEngine(StrEnum)`: `CHROMIUM`, `FIREFOX`, `WEBKIT`.
- `BROWSER_ENGINES: Final[dict[BrowserName, BrowserEngine]]`. `chrome` and
  `msedge` map to `CHROMIUM`.
- `BROWSER_DISPLAY_NAMES: Final[dict[BrowserName, str]]`: "Chromium", "Firefox",
  "WebKit (Safari engine)", "Google Chrome", "Microsoft Edge".
- `DEFAULT_BROWSER: Final[BrowserName] = BrowserName.CHROMIUM`.
- `ClientCapability.BROWSER_SELECTION_V1 = "browser_selection_v1"`.
- `REQUIRED_CLIENT_CAPABILITIES` becomes an explicit frozenset of the four
  existing members, without the new one. `CLIENT_CAPABILITIES` stays
  `tuple(ClientCapability)`.
- `CLIENT_VERSION = "0.4.0"`.
- `ErrorCode.BROWSER_NOT_INSTALLED = "BROWSER_NOT_INSTALLED"`.
- Payloads:
  - `browser: BrowserName` is added to `BrowserNavigatePayload`,
    `BrowserClickPayload`, `BrowserTypePayload`, `BrowserHoverPayload` and
    `BrowserDownloadPayload`.
  - New `@dataclass(frozen=True) class BrowserTargetPayload: browser:
    BrowserName` for get_elements, get_page_content, get_url and
    take_screenshot.
  - New `parse_browser_target_payload(data: dict[str, object]) ->
    BrowserTargetPayload`.
  - A private `_extract_browser(data) -> BrowserName`: a missing key gives
    `DEFAULT_BROWSER`, so a newer runner still works with an older server. An
    unknown value raises `ProtocolError`, and the runner replies
    `INVALID_MESSAGE`.
  - `browser_list_downloads` is unchanged; it never launches a browser.
- `controller_client/exceptions.py`: `class BrowserNotInstalledError(ExecutionError)`.

### Server shared interfaces (written first, with the protocol)

- `projects/exceptions.py`: `class BrowserUnavailableError(InfrastructureError)`
  with `__init__(self, safe_message: str, *, detail: str = "")` and attributes
  `safe_message` and `detail`.
  - `safe_message` is shown in the UI and TestRail.
  - `detail` goes to the server log only; it carries the runner text, which
    includes local paths with the OS user name.
- `agents/types.py`: `@dataclass class BrowserSelection: current: BrowserName =
  DEFAULT_BROWSER`. It is mutable on purpose: one instance per case, shared by
  every sub-agent's `ToolContext`.
- `ToolContext` gains the field `browser_selection: BrowserSelection`. It is
  required and placed right after `project_id`, so mypy flags every
  construction site.

### Services

- `projects/browser_selection_services.py` (new, one use case):
  - `ensure_runner_supports_browser(project_id: int, browser: BrowserName) ->
    None`. It returns at once for `chromium`. Otherwise it reads only
    `agent_capabilities` and `agent_client_version`
    (`values_list(...).get()`, one query per browser change), and raises
    `BrowserUnavailableError` when `BROWSER_SELECTION_V1` is missing.
  - The safe message: "This Test Runner (version X) can only drive Chromium;
    this case needs <label>. Download a fresh Test Runner from the Environment
    page."
- `projects/services.py`:
  - These functions gain a **required** keyword `browser: BrowserName`, sent as
    payload key `browser` (value `browser.value`):
    `controller_browser_navigate`, `controller_browser_click`,
    `controller_browser_type`, `controller_browser_hover`,
    `controller_browser_get_elements`, `controller_browser_get_page_content`,
    `controller_browser_get_url`, `controller_browser_take_screenshot`,
    `controller_browser_download`.
  - `_raise_for_error_reply`: code `BROWSER_NOT_INSTALLED` raises
    `BrowserUnavailableError(safe_message=<fixed text>, detail=message)`. The
    fixed text is: "The browser this case asked for is not installed on the test
    machine. Install it (Chrome/Edge: install normally; Firefox/WebKit:
    `playwright install firefox webkit` in the Test Runner venv), or run
    `python -m controller_client.diagnose --browsers` there."
    `_raise_for_error_reply` does not know the browser; the agent log already
    shows which one was requested.
  - `execute_test_run_test_case`: unchanged apart from what step 5 needs. The
    `BrowserSelection` is created inside `run_orchestrator`, so the reset per
    case is structural.
- `projects/case_blocking.py`:
  - Add `(BrowserUnavailableError, BlockedReason.BROWSER_UNAVAILABLE)` to
    `_REASON_BY_TYPE`.
  - `blocked_message` returns `exc.safe_message` for that reason.

### Agent tools and prompts

- `agents/services/orchestrator.py`: `run_orchestrator` creates one
  `BrowserSelection()` per call (one call per case) and hands it to
  `_build_sub_agent_execution_config` / `AgentConfig`, so every `run_sub_agent`
  and recovery sub-agent shares it.
  `agents/services/sub_agent.py` and `agent_loop.py::run_agent` put it on
  `ToolContext`. `run_agent`, outside test runs, creates its own instance.
- `agents/services/tool_definitions.py`: `browser_navigate` gains
  `ToolParameter(name="browser", type="string", required=False,
  enum=tuple(BrowserName))`. Its description: "Browser to open the page in.
  Only set this when the test case names a browser; otherwise omit (Chromium).
  Changing it closes the current browser and its page state." No other tool
  takes the argument, since they all act on the page navigate opened.
- `agents/services/tool_registry.py::_handle_browser_navigate` turns the
  argument into `BrowserName | None`. A value outside the enum returns a tool
  error listing the allowed values, without dispatching.
- `agents/services/tools_controller.py`:
  - `browser_navigate(project_id, *, url, browser: BrowserName | None,
    selection: BrowserSelection)`. When `browser` is given and differs from
    `selection.current`, it calls `ensure_runner_supports_browser` (which may
    raise; `safe_tool_call` re-raises the infrastructure error, and the case is
    Blocked) and then sets `selection.current = browser`. It always sends
    `selection.current`.
  - The selection is updated **before** the dispatch. A not-installed error
    blocks the case anyway, so nothing is left to roll back.
  - The other browser tools (`browser_click/type/hover/get_page_content/get_url/take_screenshot/download`)
    take `selection: BrowserSelection` and send `selection.current`.
  - The success text of `browser_navigate` names the browser: "Navigated
    <label> to <url>".
- `agents/services/browser_element_finder.py`: `find_element_index(...)` gains
  `browser: BrowserName` and passes it to `controller_browser_get_elements`.
- `agents/services/prompt_parts.py`:
  - `build_environment_context`: the three Chromium lines become "A browser is
    available via browser tools (Playwright): chromium (default), firefox,
    webkit, chrome (Google Chrome), msedge (Microsoft Edge)."
  - New `build_browser_selection_rule() -> str`, included in the tool
    guidelines: "If the test case names a browser (e.g. 'in Firefox', 'using
    Edge', 'Safari' means webkit), pass browser=... on your first
    browser_navigate and keep using it; it stays selected for the rest of the
    case. Otherwise omit it to use Chromium. Do not switch browser unless the
    test case says so."
  - The `browser_navigate` example gains a `browser="firefox"` variant.
- `agents/services/sub_agent_prompts.py`: the sub-agent prompt states "Current
  browser for this case: <label>", read from the shared selection when the
  sub-agent starts. Later steps then don't re-pick a browser, and know which
  one is open.
- `agents/services/orchestrator_prompts.py`: a planning rule says "If the test
  case names a browser, keep that browser name in the description of the first
  sub-task that opens a web page." Without it, the planner could drop the
  mention before any sub-agent sees it.

### Runner (`controller_client/`)

- `browser_executor.py`:
  - `BrowserSession.ensure_page(browser: BrowserName) -> Page`. The session
    keeps `_browser_name: BrowserName | None`. If a page is open in the same
    browser, it is reused.
  - If the browser differs, `_close_browser()` closes the context and the
    browser but keeps the Playwright driver, and resets the page, the element
    frames and the download records. Then `_launch(browser)` runs.
  - `_launch`:
    - picks the engine from `BROWSER_ENGINES`;
    - sets `channel=browser.value` for `chrome` and `msedge`;
    - passes `args=["--disable-gpu"]` only for the Chromium engine, dropping
      `--no-sandbox`, which Playwright adds itself;
    - passes no args for Firefox and WebKit;
    - always uses `headless=False` and the same context options.
  - A launch error whose text matches `Executable doesn't exist` or
    `distribution '...' is not found` / `is not supported on` raises
    `BrowserNotInstalledError`, with the display name and install hint. Any
    other error stays `ExecutionError`.
  - `close()` resets `_browser_name`.
  - All `execute_browser_*` functions that call `ensure_page` take a payload
    that carries `browser`. The four payload-less ones now take
    `BrowserTargetPayload`.
- `client.py`: the handlers parse with the new parsers. One helper,
  `_browser_error_code(exc, fallback: ErrorCode) -> ErrorCode`, maps
  `BrowserNotInstalledError` to `BROWSER_NOT_INSTALLED`,
  `BrowserElementNotFoundError` to `ELEMENT_NOT_FOUND`, and everything else to
  `fallback`. It replaces the per-handler except chains.
- Diagnostics:
  - `browser_diagnostics.py` (new): `build_browser_steps(browsers:
    Sequence[BrowserName]) -> tuple[DiagnosticStep, ...]`. Each step launches
    the browser headless, opens `about:blank`, reports `browser.version`, and
    closes it. It reuses `DiagnosticStep` / `run_diagnostics` from
    `omniparser_diagnostics.py`.
  - `diagnose.py` gains `--browsers [name,...]`, defaulting to all five when
    given with no list. With it, only the browser steps run.
- Setup scripts `scripts/setup.{sh,ps1,bat}`: comments and a final hint only;
  no behaviour change.

### Pages and forms

No new page, URL, form or view. Two template changes:

| Surface | Change |
|---|---|
| `templates/partials/runner_download_card.html` | Static help block: Chromium, Firefox and WebKit come with the Test Runner; Chrome and Edge must be installed normally on the test machine (Edge ships with Windows); name the browser in the test case ("Open the app in Firefox"); `python -m controller_client.diagnose --browsers` checks them. |
| Run detail / case detail | Nothing new. The Blocked reason label and message render through the existing blocked card. The agent log shows "Navigated Firefox to ...". |

### Background work and WebSockets

- No new Celery task or queue. No consumer event change: the existing case
  events carry `blocked_reason` and `blocked_reason_label`.
- `projects/controller_protocol.py`: add `browser: str` to the nine browser
  `*ActionEvent` TypedDicts (all except `BrowserListDownloadsActionEvent`), and
  `"browser"` to their `_PAYLOAD_KEYS`.
- Old runners (0.3.0) still connect, because the capability is optional.
  Chromium cases are unaffected (they ignore the field and launch Chromium).
  The first `browser_navigate(browser=<non-chromium>)` hits
  `ensure_runner_supports_browser`, and the case is Blocked
  (`browser_unavailable`, "download a fresh Test Runner").
- Reconnects and restarts: the server sends the browser on every action, so an
  action re-sent after a reconnect, or after a runner restart mid-case, opens
  the browser the case selected. Page state is lost, as today.

### Settings / infrastructure

None. No env vars and no `example.env` change. Browsers are needed only on the
runner.

### Data flow

1. A case starts. Cleanup closes any browser. `run_orchestrator` creates
   `BrowserSelection(current=chromium)`. The planner keeps "in Firefox" in the
   first web sub-task.
2. The sub-agent calls `browser_navigate(url, browser="firefox")`. The handler
   validates the enum, then `ensure_runner_supports_browser` runs: it passes on
   a 0.4.0 runner and raises on 0.3.0, which blocks the case. Then
   `selection.current = firefox`, and
   `controller_browser_navigate(..., browser=firefox)` is dispatched.
3. The runner's `ensure_page(firefox)` launches Firefox and navigates. A
   missing install gives `BROWSER_NOT_INSTALLED`, which becomes
   `BrowserUnavailableError`, then `safe_tool_call` re-raises it and the case
   is Blocked.
4. Every later browser tool, in this sub-agent or the next, sends
   `selection.current`, so it stays Firefox.
5. Cleanup after the case closes the browser. The next case gets a fresh
   `BrowserSelection`, so it starts on Chromium.

## Alternatives considered

- **`browser` on every browser tool**: it lets the LLM switch by accident
  mid-case, for example on a click. A single entry point on navigate plus
  server-side memory is deterministic.
- **The runner remembers the browser** (a dedicated select action, or
  navigate-only): state is lost when the runner restarts mid-case, and the
  result is silent Chromium. Sending the browser on every action keeps the
  runner stateless, at about 20 bytes per message.
- **Persisted run or project setting**: rejected by the user (Q1).
- **Required capability**: rejected (Q3).
- **Not added**: a strategy class per engine. One dict and one `_launch` branch
  covers five values.

## Risks and mitigations

- **The LLM misses or invents a browser.** The prompt rule says to switch only
  when the case names one. The enum rejects unknown values. The planner rule
  keeps the browser mention in the sub-task. Verify with cases phrased "in
  Firefox", "using Microsoft Edge", "on Safari".
- **A case-text browser no runner can provide.** For example "Safari": the
  prompt maps it to `webkit` and the summary says so. "Opera" is not in the
  enum, so the agent stays on Chromium. Its verdict follows the QA rules, and
  the summary should explain the mismatch.
- **Fragile not-installed detection.** It matches Playwright error text, pinned
  at `~=1.50.0`. Re-check it on any Playwright bump. Unmatched errors still
  surface as `EXECUTION_FAILED` tool errors.
- **Path leaks.** The runner's text carries local paths. It goes to the server
  log only (`detail`). The Blocked text and TestRail get the fixed message.
- **Branded Chrome or Edge infobar.** Playwright passes `--no-sandbox` while
  `chromium_sandbox=False`, so Chrome or Edge may show an "unsupported flag"
  bar that shifts the page. Check during verification. If it appears, use
  `chromium_sandbox=True` for those channels on Windows and macOS.
- **Mid-case switch.** It closes the old browser, its page state and any
  in-flight Playwright downloads. This is documented in the tool description.
- **Engine differences.** WebKit on Windows and Ubuntu 20.04 has gaps; Firefox's
  first launch is slower. The 10 s action cap and 30 s reply timeout are
  unchanged. Raise the navigate timeout only if first launches get near 30 s.
- **Migration.** Choices only, no data change.

## Implementation steps and ownership

**Step 0, shared code: write it first, directly and not by a sub-agent.**

- `controller_client/protocol.py`: everything under "Shared protocol vocabulary".
- `controller_client/exceptions.py`: `BrowserNotInstalledError`.
- `projects/exceptions.py`: `BrowserUnavailableError`.
- `agents/types.py`: `BrowserSelection` and `ToolContext.browser_selection`.
- `projects/models.py` + `projects/migrations/0019_blocked_reason_browser.py`:
  `BlockedReason.BROWSER_UNAVAILABLE`.

**Then three parallel sub-agents, with disjoint files:**

- **A, Runner:** `controller_client/browser_executor.py`,
  `controller_client/client.py`, `controller_client/browser_diagnostics.py`
  (new), `controller_client/diagnose.py`,
  `controller_client/scripts/setup.sh`, `setup.ps1`, `setup.bat` (comments and
  final hint only).
- **B, Server dispatch and blocking:** `projects/controller_protocol.py`,
  `projects/services.py` (the `controller_browser_*` signatures and
  `_raise_for_error_reply`), `projects/browser_selection_services.py` (new),
  `projects/case_blocking.py`,
  `templates/partials/runner_download_card.html`.
- **C, Agent:** `agents/services/tool_definitions.py`,
  `agents/services/tool_registry.py`, `agents/services/tools_controller.py`,
  `agents/services/browser_element_finder.py`,
  `agents/services/orchestrator.py`, `agents/services/sub_agent.py`,
  `agents/services/agent_loop.py`, `agents/services/prompt_parts.py`,
  `agents/services/sub_agent_prompts.py`,
  `agents/services/orchestrator_prompts.py`. C consumes B's signatures and
  depends on the interfaces below, not on B's code.

**Integration (orchestrator):** check the call sites line up, then run
`pre-commit run --all-files` (both mypy configs) and
`python manage.py makemigrations --check --dry-run`. Update the Commands section
of `CLAUDE.md` with `diagnose --browsers`, and add a line on browser selection
to `agents/CLAUDE.md`.

**Interfaces C relies on from B:**

```
controller_browser_navigate(project_id: int, url: str, *, browser: BrowserName, timeout: float = 30.0) -> ActionResult
controller_browser_click(project_id: int, element_index: int, *, browser: BrowserName, timeout: float = 30.0) -> ActionResult
controller_browser_type(project_id: int, element_index: int, text: str, *, browser: BrowserName, timeout: float = 30.0) -> ActionResult
controller_browser_hover(project_id: int, element_index: int, *, browser: BrowserName, timeout: float = 30.0) -> ActionResult
controller_browser_get_elements / _get_page_content / _get_url(project_id: int, *, browser: BrowserName, timeout: float = 30.0) -> BrowserContentResult
controller_browser_take_screenshot(project_id: int, *, browser: BrowserName, timeout: float = ...) -> (unchanged return type)
controller_browser_download(project_id: int, url: str, save_path: str = "", *, browser: BrowserName, timeout: float = 120.0) -> ActionResult
ensure_runner_supports_browser(project_id: int, browser: BrowserName) -> None   # raises BrowserUnavailableError
```

Wire contract between B and A: the payload key is `"browser"`, its value is
`BrowserName.value`, and the error code is `ErrorCode.BROWSER_NOT_INSTALLED`.

## Verification

No automated tests in this project. Check by hand on a real Test Runner.

1. Run `mypy .` and `controller_client/.venv/bin/mypy --config-file
   controller_client/setup.cfg controller_client/` (both clean), then
   `python manage.py check`.
2. Run `python -m controller_client.diagnose --browsers` on macOS, Windows and
   Linux runners: the installed browsers report versions, and a missing Chrome
   shows the hint.
3. Run one case per wording ("Open <url> in Firefox", "using Microsoft Edge",
   "in Safari", and no browser named). In each:
   - the right browser window appears;
   - the agent log shows "Navigated <label> ...";
   - later steps (click, type, hover, iframe element, shadow-DOM element,
     download plus list_downloads, screenshot) stay in that browser.
4. In a run of two cases, case 1 "in Firefox" and case 2 with no browser: case 2
   runs in Chromium (the reset per case works).
5. A case naming Chrome on a machine without Chrome:
   - the case is Blocked, labelled "Browser unavailable on the test machine",
     with the fixed text;
   - the server log has the runner detail;
   - TestRail push gives Blocked (status 2).
6. A 0.3.0 runner:
   - it connects;
   - a no-browser case passes in Chromium;
   - a Firefox case is Blocked with "download a fresh Test Runner".
7. On a Firefox case, restart the runner mid-case: the re-sent action reopens
   Firefox.
8. On Windows, check Chrome and Edge for the unsupported-flag infobar.
9. Run the `reviewer` and `verifier` agents in parallel with this plan and the
   original request.

## Changelog

- 2026-10-22: created (Draft; Q1-Q6 open).
- 2026-10-22: user answers applied; status set to Ready.
  - Q1: the browser comes from the test case text; the `TestRun.browser` field,
    the Create-run dropdown, the wizard change and the browser migration are
    removed.
  - Q2: the agent selects the browser through an optional enum `browser` on
    `browser_navigate`. The server-side `BrowserSelection` lasts one case and
    is sent on every browser action; it resets to Chromium per case.
  - Q3: optional capability, checked when the agent first switches browser.
  - Q4: the enum is reduced to chromium, firefox, webkit, chrome, msedge.
  - Q5, Q6: assumptions kept. The migration is now only the `BlockedReason`
    choices change (0019).
- 2026-10-22: review pass 1 fixed: relaunch after a crashed browser resets its
  dead context; a launch timeout in `browser_download` no longer hits the
  download-timeout handler; a browser switch keeps the case's download records;
  the old-runner Blocked text no longer embeds the runner-reported version.
  Rejected:
  - Naming the browser in the not-installed safe message: `_raise_for_error_reply`
    has no browser context, and the agent log line "Navigated <label> ..." /
    the tool call already show which browser was asked for.
  - Passing `selection.current` instead of the `BrowserSelection` to the
    read-only browser tools: one uniform way to thread the selection from
    `ToolContext` is simpler than two; only `browser_navigate` mutates it.
