# feat: Phone and tablet testing through the Test Runner (Android, iOS, iPadOS)

Status: Done
Created: 2026-11-06
Request: Let Punk Hazard test apps on Android phones, iPhones and iPads with the
existing Test Runner (`controller_client/`), using one source tree and no separate
phone app. The phone does not run the controller: iOS sandboxing does not allow
input injection or screenshots of other apps. The runner keeps running on a host
Mac or PC and drives one phone attached by USB or Wi-Fi, or one emulator or
simulator, through a new mobile backend. The runner's WebSocket, API key and
handshake stay as they are. The handshake additionally reports the target kind
and the device, plus a new capability. The server stores and shows that
information, and the agent gets phone tools (tap, swipe, long press, type, dismiss
the keyboard, hardware buttons, rotate, launch/close/install an app, open a URL)
in place of the desktop-only tools. Screenshots keep the same pipeline:
`screenshot()` returns a PIL image, then the vision worker, then evidence. A phone
that drops off mid-run is a new infrastructure fault that opens Need help. Older
runners must keep working unchanged. iOS real devices need a macOS host; Android
works from any host OS.

## Context

What exists today. All of it was read in the code.

**Runner seam.**
- `controller_client/desktop_backend.py::DesktopBackend` is a Protocol: `click`,
  `double_click`, `move_to`, `drag`, `scroll`, `type_text`, `resolve_key`,
  `press_keys`, `screenshot`, `screen_size`, `capture_size`, `input_size`,
  `session_info`, `diagnostic_steps`. Pointer methods take input-space
  coordinates. The executor maps screenshot pixels first with
  `pointer_mapping.map_to_input`, which scales each axis from
  `capture_size()` to `input_size()`.
- `controller_client/desktop.py` picks the backend **at import time** by
  `sys.platform` (`desktop_gnome` on Linux, `desktop_pyautogui` elsewhere).
  `executor.py` freezes it in a module constant, `_DESKTOP: Final =
  get_desktop_backend()`. `screen_capture.capture_screen()`,
  `system_info.gather_system_info()`, `main.py` and `diagnose.py` all call
  `get_desktop_backend()`. `main.py` loads the config *after* these imports
  have run, so today a target cannot be chosen from `.env`.
- `controller_client/executor.py`: every input action calls
  `ensure_input_not_blocked()` (Windows UIPI) first. `execute_screenshot()`
  encodes `capture_screen()` as PNG. `execute_command` and
  `execute_command_streaming` run `shell=True` on the host.
- `controller_client/client.py`: `_handlers` maps `MessageType` to a handler.
  `_dispatch` wraps each handler in `_window_hidden()`, which hides the person
  window for every message outside `_WINDOW_SAFE_MESSAGE_TYPES`.
  `_send_handshake` sends `client_version`, `advertised_capabilities(person_window=)`,
  `system_info` (a flat `dict[str, str | int]`) and the optional `person_email`.
- `controller_client/system_info.py`: `os`, `os_version`, `architecture`,
  `hostname`, `screen_width/height`, plus the backend's `session_info()` keys.
- `controller_client/manual_capture.py`: every Manual capture runs hide, ack,
  settle, `capture_screen()`, show.
- `controller_client/main.py::_with_person_email` is the pattern for "ask at the
  first interactive start, save to `.env` with `config.save_env_value`, never ask
  on a detached start". `config.py` reads `CONTROLLER_*` through
  `python-decouple` (allowed in the runner, which has its own config).
- `controller_client/update_installer.py` builds the new venv with `pip install
  -r requirements.txt` and `playwright install`. It never installs OS packages.
- `controller_client/cleanup.py::execute_cleanup` closes the Playwright browser,
  terminates interactive sessions, kills tracked processes and clears
  `CONTROLLER_CLEANUP_DIR`.
- `controller_client/protocol.py` (stdlib only; the server imports it):
  `CLIENT_VERSION = "0.18.1"`, `MessageType`, `ClientCapability`,
  `advertised_capabilities`, `REQUIRED_CLIENT_CAPABILITIES` (interactive
  commands and cleanup), `ErrorCode`, and the payload dataclasses and parsers.
- mypy for the runner: `controller_client/setup.cfg` (strict, local stubs in
  `controller_client/stubs/`).

**Server.**
- `projects/controller_consumer.py::_handle_handshake` parses capabilities, the
  in-flight update id and the person email, then `claim_agent_connection`
  (`projects/services.py`) stores `agent_system_info`, `agent_client_version`
  and `agent_capabilities` on `Project`. `_disconnected_agent_fields` clears
  them, and `broadcast_agent_status` pushes `agent.status` to the project
  status socket (`projects/consumers.py`). Templates read `agent_system_info`
  through `json_script` (`templates/projects/environment.html`, `overview.html`,
  `wizard/step_machine.html`, `partials/badge_machine.html`).
- Controller actions: `projects/services.py::_dispatch_controller_action` (private)
  → channel event `controller.<action>` → `controller_consumer.controller_<action>`
  → runner message. Event TypedDicts live in `projects/controller_protocol.py`.
  `_raise_for_error_reply` maps `ErrorCode.BROWSER_NOT_INSTALLED` to
  `BrowserUnavailableError` and every other code to `ControllerActionError`.
- Faults: `projects/case_faults.py` (`_REASON_BY_TYPE`, `_ASK_BY_REASON`,
  `_RESTORE_FAULTS`, `help_continue_mode`), `projects/case_pause_policy.py`
  (`_UNTRUSTED_SCREEN_FAULTS`) and `projects/models.BlockedReason`.
- Machine badge: `projects/services.py::MachineState` and `get_machine_state`
  (DISCONNECTED, STARTING, READY, FAILED, VISUAL_ENGINE_OFFLINE). Run, resume and
  setup gates compare it with READY (`run_reopen_services`,
  `machine_setup/services/send_gates.py`, `case_auto_fix.py`, `chat_page.py`,
  `dashboard/all_projects_views.py`).
- Capability checks: `projects/runner_capability_services.py` (`runner_supports`,
  `runner_os_name`).
- Agent tools: `agents/services/tool_definitions.py` (`get_controller_tool_definitions`,
  `get_browser_tool_definitions`, `get_all_tool_definitions`).
  `agents/services/tool_registry.py` (`_TOOL_HANDLERS`, `dispatch_tool_call`,
  `tool_definitions_named`, `allowlisted_dispatcher`).
  `agents/services/agent_loop.py` falls back to `get_all_tool_definitions()`
  when no list is given. `agents/types.py::ToolContext` has no target.
  `machine_setup/services/setup_tools.py::machine_tool_definitions` also returns
  `get_all_tool_definitions()`. `agents/services/person_step_prompts.py` uses the
  tool names to strip executor vocabulary from person steps.
- Prompts: `agents/services/prompt_parts.py` (`build_agent_persona`,
  `build_environment_context`, `build_tool_guidelines`, `build_desktop_tool_examples`,
  `build_shell_rules`) and `agents/os_shell.py` (`get_os_name`, `shell_label`).
  `projects/person_step_rows.command_shell_label` uses the latter for person
  steps.
- Readiness: `machine_readiness/runner_info.py` and
  `run_stage0_state.runner_host` (the hostname is the identity that statuses are
  trusted for). `projects/machine_readiness_ports.py` injects the runner calls.

**Research (versions checked 2026-11-06).**
- Appium server 3.9.0 (npm, Apache-2.0) needs Node `^20.19 || ^22.12 || >=24`.
  appium-uiautomator2-driver is 9.0.1 (Apache-2.0). appium-xcuitest-driver is
  13.0.7 (Apache-2.0) and needs Node `^22.22.2 || ^24.15.0 || >=26`, so the
  effective floor is **Node 22.22.2 or 24.15+**.
- Appium-Python-Client 6.1.5 (PyPI, Apache-2.0, Python >= 3.10, ships `py.typed`)
  depends on `selenium>=4.51,<5` and `typing-extensions`. No local stub is
  needed.
- pymobiledevice3 11.50.0 is **GPL-3.0-or-later**. It is pure Python and supports
  iOS 17+ over a tunnel, but it would be a GPL dependency imported into a runner
  we distribute as a ZIP.
- XCUITest needs WebDriverAgent (WDA, BSD) on the device. On real devices the
  driver builds and signs it with `xcodebuild`, using `xcodeOrgId` (Team ID)
  plus `xcodeSigningId`, or an `xcconfig`. Alternatives are
  `usePreinstalledWDA` + `updatedWDABundleId` (devicectl on iOS 17+/Xcode 15+)
  and `prebuiltWDAPath`. A free Apple account adds a bundle prefix, and its
  profiles expire after 7 days. Real-device WDA is known to be flaky (a reboot
  often fixes it). Simulators need no signing.
- iOS taps use points and screenshots use pixels. The scale is 2x or 3x, and an
  app in iPad compatibility mode is offset. Android UiAutomator2 taps use
  pixels.

## Goals
- `CONTROLLER_TARGET=android|ios` turns the same runner into a phone runner for
  one device. `desktop` (the default) behaves exactly as today.
- The agent sees a phone tool set when the runner reports a phone target. Every
  shared tool (verified `click`, `click_at`, screenshots, OmniParser) works
  unchanged through the backend seam.
- The handshake reports the target and the device. The Environment page,
  overview, wizard and machine badge show the device and its live status.
- A phone that drops off mid-case opens Need help with the fault "Phone or tablet
  disconnected". It never fails a step.
- Human in the loop, Manual runs (tester screenshots of the phone), self-update,
  readiness Check machine and Machine setup all work with a phone target.
- `python -m controller_client.diagnose --mobile` covers reachability,
  screenshot, a test tap and the Appium/Node/Xcode prerequisites.
- Runners older than 0.19.0, and desktop runners, are unaffected.

## Non-goals
- A runner process driving more than one device. Device farms and parallel
  devices per project are also out.
- Cloud device vendors (BrowserStack, Sauce): their capabilities, auth and
  upload flows. The design keeps a remote Appium URL possible (Q8).
- DOM-level mobile web tools (`browser_*` mapped onto Appium web contexts) in
  this plan (Q2).
- Installing Node, Appium, drivers, Android SDK or Xcode from the runner or the
  self-update.
- Uploading app builds from the server or KB to the host (Q2).
- Automatic WDA signing management or Apple account handling.

## Design

### Key decisions

- **D1 Seam: a mobile backend implements `DesktopBackend`, plus a separate
  `MobileDevice` protocol for phone-only actions.** All shared paths go through
  `DesktopBackend` unchanged: screenshot, `capture_size`/`input_size` with
  `pointer_mapping`, tap via `click`, double tap, drag, scroll as a swipe,
  `type_text`, a small key set, diagnostics and `session_info`. That keeps the
  verified click flow, `click_at`, OmniParser, `controller_find_elements`,
  `screenshot_response`, Manual capture and evidence free of any change. The
  phone-only gestures and app lifecycle (`swipe`, `long_press`, `press_button`,
  `rotate`, `hide_keyboard`, `launch/close/install/check app`, `open_url`,
  device status) go on a second protocol. A desktop backend never has to stub
  them. Rejected: a new "target" abstraction replacing `DesktopBackend`. It
  would rewrite every desktop path for no gain. The protocol keeps its name;
  renaming it to `ScreenBackend` is churn this plan avoids.
- **D2 Target selected at startup, not import.** `desktop.py` keeps the host
  desktop backend. The person window, hotkeys and app closer still need it. A
  new `controller_client/target.py` holds the **selected** backend, set once by
  `main.py` after `load_config`. `get_target_backend()` returns it (the host
  desktop when nothing was selected, so `diagnose` and the old paths behave as
  today), and `get_mobile_device()` returns the device or None. Callers that
  drive the target switch from `get_desktop_backend()` to
  `get_target_backend()`: `executor`, `screen_capture`, `system_info` and
  `env_check`. `executor._DESKTOP` becomes a per-call lookup. No lazy imports:
  `target.py` imports the mobile modules at top level, and those import Appium
  at top level, which is fine because the runner venv always has the
  requirements. The server never imports them, only `protocol.py`.
- **D3 Driver: Appium 3 through Appium-Python-Client**, with plain `adb` and
  `xcrun devicectl`/`xcrun simctl` used only for discovery and the status
  heartbeat (Q3). The runner manages a local Appium server (`appium --address
  127.0.0.1 --port <free>`, no `--relaxed-security`, killed on exit) unless
  `CONTROLLER_APPIUM_URL` names one. One Appium session per runner process,
  created after startup with no app (home screen), `noReset=true`, free
  `systemPort`/`wdaLocalPort`/`mjpegServerPort` and `newCommandTimeout`=3600.
  It is recreated when the session is invalid. A lock serializes session calls,
  since the device monitor does not use the session.
- **D4 Server decides "phone" from the handshake.** The runner is a phone
  runner when `system_info.target` is `android` or `ios` **and** it advertises
  `MOBILE_DEVICE_V1`. A runner without the key (every older runner and every
  desktop runner) is `desktop`. `system_info.os` stays the **host** OS: shell
  commands, artifact paths, the readiness profile OS and Windows shell checks
  are about the host.
- **D5 One tool-set switch on the server.** `ToolContext.runner_target` is set
  wherever a context is built, from the system info the caller already passes.
  `tool_definitions_for(target)` gives the model its tools, and the dispatcher
  refuses a tool outside the target's set. Shared names keep their names, with
  phone wording on a phone target (Q4).
- **D6 Device status is its own small state on `Project`,** not folded into
  `agent_system_info`. It changes often and is cleared on disconnect. A stale
  status (no heartbeat within `RUNNER_DEVICE_STALE_SECONDS`) reads as
  disconnected.
- **D7 Device-disconnected fault.** The runner answers any target action whose
  device is gone with `ErrorCode.DEVICE_DISCONNECTED`. The server raises
  `DeviceDisconnectedError(InfrastructureError)`, which maps to
  `BlockedReason.DEVICE_DISCONNECTED` and opens Need help. The default continue
  mode is `current_screen`: the phone keeps its screen when a cable drops, and
  the person may still pick restore or from scratch. It is not in
  `_RESTORE_FAULTS` or `_UNTRUSTED_SCREEN_FAULTS`. There is no server-side wait
  in v1 (Q5e).
- **D8 No window hiding for a phone target.** Agent actions and Manual
  captures read the phone's screen, so the person window on the host never
  needs to hide. `client._window_hidden` and `manual_capture` skip the hide when
  `get_mobile_device()` is set. The person keeps seeing the step while the
  agent works.

### Protocol (`controller_client/protocol.py`), shared, written first
- `CLIENT_VERSION = "0.19.0"`.
- `class RunnerTarget(StrEnum)`: `DESKTOP`, `ANDROID`, `IOS`.
- `class DeviceKind(StrEnum)`: `PHYSICAL`, `EMULATOR`, `SIMULATOR`.
- `class DeviceState(StrEnum)`: `READY`, `STARTING` (session being created,
  e.g. a WDA build), `DISCONNECTED`, `UNAUTHORIZED` (USB debugging not
  authorised, or the iPhone does not trust the host), `UNAVAILABLE` (Appium or
  the session failed; see detail).
- `ClientCapability.MOBILE_DEVICE_V1`. It is optional, not in
  `REQUIRED_CLIENT_CAPABILITIES`, and advertised only on a phone target:
  `advertised_capabilities(person_window: bool, mobile: bool)`.
- System info keys (flat, bounded strings): `target`, `device_platform`
  (`Android`/`iOS`/`iPadOS`), `device_kind`, `device_model`, `device_name`,
  `device_os_version`, `device_id` (serial or UDID) and `device_scale`. On a
  phone target `screen_width`/`screen_height` are the **device** screenshot
  pixels in portrait. `parse_runner_target(system_info) -> RunnerTarget` and
  `parse_device_info(system_info) -> DeviceInfoPayload | None` (each value
  ≤ 100 chars, else dropped) are the one definition every server reader uses.
- New `MessageType`s (server → runner, answered by `action_result` /
  `error`): `SWIPE`, `LONG_PRESS`, `PRESS_BUTTON`, `ROTATE_DEVICE`,
  `HIDE_KEYBOARD`, `OPEN_URL`, `INSTALL_APP`. Runner → server, no reply:
  `DEVICE_STATUS` {`state`, `detail` ≤ 500}, sent on change and every
  `DEVICE_STATUS_INTERVAL_SECONDS` (30).
- Payloads and parsers: `SwipePayload` (direction as the existing
  `ScrollDirection`, optional start point in screenshot pixels, `distance`
  0.1–0.9 of the screen, `duration`), `LongPressPayload` (x, y, `duration`
  0.5–10 s), `PressButtonPayload` (`MobileButton`: `back`, `home`,
  `app_switch`, `volume_up`, `volume_down`, `power`), `RotateDevicePayload`
  (`DeviceOrientation`: `portrait`, `landscape`), `OpenUrlPayload` (url ≤
  `PERSON_STEP_URL_MAX_CHARS`, http/https or a custom scheme, no `javascript:`
  or `file:`), `InstallAppPayload` (host path ≤ `ARTIFACT_PATH_MAX_CHARS`,
  network paths refused as in artifact scan) and `DeviceStatusPayload`.
  `launch_app`, `close_app` and `check_app_installed` keep their payloads: on a
  phone `app_name` is a package/bundle id or a visible app name.
- `ErrorCode.DEVICE_DISCONNECTED` and `ErrorCode.TARGET_MISMATCH`. The second
  answers a desktop-only message on a phone target (hover, `close_tab`,
  `browser_*`) and a phone message on a desktop target.

### Runner modules (`controller_client/`)
| Module | Responsibility |
|---|---|
| `config.py` | `CONTROLLER_TARGET` (`desktop`), `CONTROLLER_DEVICE` (empty = the only device), `CONTROLLER_APPIUM_URL` (empty = managed local server), `CONTROLLER_APPIUM_COMMAND` (default `appium`), `CONTROLLER_IOS_TEAM_ID`, `CONTROLLER_IOS_SIGNING_ID` (default `Apple Development`), `CONTROLLER_IOS_WDA_BUNDLE_ID`, `CONTROLLER_IOS_PREINSTALLED_WDA` (bool). Validated into `ClientConfig`. `ios` on a non-macOS host is a startup error for real devices. |
| `target.py` (new) | `select_target(config)`, `get_target_backend()`, `get_mobile_device()`, `current_target()`. It holds the one selected backend. |
| `mobile_discovery.py` (new) | List devices without Appium: `adb devices -l`, `xcrun devicectl list devices --json-output`, `xcrun simctl list devices booted -j`. Returns `DiscoveredDevice` (id, platform, kind, name, model, os version, state). Bounded subprocess timeouts, never `shell=True`. |
| `mobile_appium_server.py` (new) | Start, health-check (`/status`) and stop the managed Appium server. Reports a missing `appium`/Node or a missing driver with the exact install commands. Never installs anything. |
| `mobile_session.py` (new) | Builds the Appium options per platform (UiAutomator2Options / XCUITestOptions), creates or recreates the session under a lock, and translates WebDriver errors: device gone gives `DeviceDisconnectedError(ExecutionError)`, a dead session is recreated once. |
| `mobile_backend.py` (new) | `MobileBackend` implements `DesktopBackend` and `MobileDevice` on top of `mobile_session`. `click(button="right")` and `move_to` raise `ExecutionError` (no right click or hover on a phone). `scroll` becomes a swipe in the opposite direction, amount × 10% of the screen. `resolve_key` accepts only enter/return, backspace, delete, tab, space and escape (Android only). `screenshot()` re-reads `input_size` per call (rotation) and rejects an all-zero frame with `ScreenCaptureError("The app on the phone blocks screenshots (secure window)")`. |
| `mobile_android.py`, `mobile_ios.py` (new) | Platform specifics: capability sets, `input_size` (Android: the screenshot pixels; iOS: the window size in points), hardware buttons (iOS: home and volume only; back → `ExecutionError` naming the alternative), app id resolution (visible name → package/bundle id via `pm list packages`/`mobile: listApps`), `open_url` (`mobile: startActivity` VIEW vs `mobile: deepLink`), app install/terminate/activate, black-frame check. |
| `mobile_app_policy.py` (new) | Never closes or terminates the Appium helper apps (`io.appium.uiautomator2.server*`, `io.appium.settings`, the WDA runner bundle) or the launcher/SpringBoard. Tracks the apps launched since the last cleanup. |
| `mobile_executor.py` (new) | `execute_swipe`, `execute_long_press`, `execute_press_button`, `execute_rotate_device`, `execute_hide_keyboard`, `execute_open_url`, `execute_install_app`, and the phone versions of `execute_launch_app`/`close_app`/`check_app_installed`. They map points with `pointer_mapping` like `executor.py`, and their messages name the screenshot point. |
| `device_monitor.py` (new) | A background asyncio task: polls `adb get-state` / devicectl / simctl every 5 s (no session use), and reports a `DeviceState` change at once plus a 30 s heartbeat through a callback the client gives it. It also triggers session re-creation when the device comes back. |
| `mobile_diagnostics.py` (new) | `DiagnosticStep`s for `diagnose --mobile`: Node version, Appium and driver presence, `adb`/Xcode tools, device discovery, session creation, screenshot (size, black frame), a test tap at the screen centre (interactive only, asks first), and the points/pixels scale. |
| `executor.py` | Backend lookup per call. `ensure_input_not_blocked` only on the desktop target. `execute_command`/streaming/interactive add `RUNNER_DEVICE_ID` and `RUNNER_DEVICE_PLATFORM` to the child env on a phone target (Q1). |
| `client.py` | Handlers for the new message types (`TARGET_MISMATCH` on the wrong target). `launch_app`/`close_app`/`check_app_installed`/`cleanup_environment` route to the phone path on a phone target. `close_tab`, `hover` and `browser_*` are refused on a phone target. `_window_hidden` is a no-op on a phone target. Starts and stops `device_monitor` with the session and sends `device_status`. `DeviceDisconnectedError` becomes `ErrorCode.DEVICE_DISCONNECTED`. The handshake sends `advertised_capabilities(..., mobile=)`. |
| `system_info.py` | Adds the device keys from `get_mobile_device().device_info()`. Screen size comes from the target. |
| `screen_capture.py`, `env_check.py` | Use `get_target_backend()`. On a phone target, startup reports a failed screenshot as a warning (the device may still be starting). |
| `manual_capture.py` | No hide/ack/settle on a phone target. The capture comes straight from the target backend. |
| `cleanup.py` | On a phone target, after the host cleanup: terminate the apps launched since the last cleanup (policy-filtered), hide the keyboard and press home (Q5a). |
| `update_handover.py` | Before spawning the new runner, quit the Appium session and stop the managed server, so the new process can claim the device. |
| `main.py` | `_with_device(config)`: on an interactive start with a phone target and an empty or unknown `CONTROLLER_DEVICE`, list the devices and ask for a number, then save with `save_env_value`. A detached start uses the only device or fails with a clear log line. Then `select_target`, start the managed Appium server, and log `Target: Android, Pixel 8 (emulator-5554)`. |
| `diagnose.py` | `--mobile` flag. |
| `requirements.txt` | `Appium-Python-Client~=6.1.5` and `selenium~=4.<x>` (pinned explicitly because the backend catches its exceptions; take the current 4.x ≥ 4.51 from PyPI when implementing). |
| `example.env`, `scripts/setup.{sh,ps1,bat}` | New keys. Setup scripts only *print* the phone prerequisites (Node ≥ 22.22.2, `npm i -g appium`, `appium driver install uiautomator2`/`xcuitest`, Android platform-tools, Xcode) when the target is not desktop. |

### Data model (`projects/models.py`, one migration)
- `class RunnerDeviceState(models.TextChoices)` mirrors `protocol.DeviceState`
  and adds `""` (no phone target).
- `Project.agent_device_state` CharField(16, choices, blank, default "").
  `Project.agent_device_detail` CharField(500, blank, default "").
  `Project.agent_device_reported_at` DateTimeField(null). All three are cleared
  by `_disconnected_agent_fields`.
- `BlockedReason.DEVICE_DISCONNECTED = "device_disconnected", "Phone or tablet
  disconnected"` (an AlterField on choices only, no SQL change).
- Migration: three `AddField`s with constant defaults. Postgres adds them
  without a table rewrite. No data migration and no data loss.

### Services
- `projects/runner_device_status_services.py` (new):
  `record_device_status(project_id, channel_name, state, detail)` is one
  conditional UPDATE, applied only while `agent_channel_name == channel_name`
  (a superseded consumer writes nothing). It returns whether the state changed;
  only then does the consumer broadcast.
- `projects/runner_target.py` (new, leaf, no ORM beyond one `values_list`):
  `load_runner_target(project_id) -> RunnerTarget` and
  `device_offline(project) -> bool` (no query; reads loaded fields and
  staleness).
- `projects/controller_mobile_actions.py` (new): `controller_swipe`,
  `controller_long_press`, `controller_press_button`, `controller_rotate_device`,
  `controller_hide_keyboard`, `controller_open_url` and
  `controller_install_app`. All are `runner_local=True` except install (long
  timeout, `INSTALL_APP_TIMEOUT_SECONDS` = 300). They go through a public
  `dispatch_controller_action` that `projects/services.py` exposes as a
  thin alias of the private one. This plan adds no new use case to the legacy
  module.
- `projects/services.py` changes: `MachineState.DEVICE_OFFLINE`
  (`get_machine_state` returns it, or `STARTING` for `starting`, when the
  target is a phone and the device is not READY or is stale; pages listing
  projects use the loaded row, no extra query); `_raise_for_error_reply` maps
  `DEVICE_DISCONNECTED` → `DeviceDisconnectedError` and `TARGET_MISMATCH` →
  `RunnerOutdatedError`-style `ControllerActionError` with fixed text;
  `_disconnected_agent_fields` and `_build_agent_status_event` gain the device
  fields; `claim_agent_connection` sets the device state to `starting` for a
  phone runner.
- `projects/exceptions.py`: `DeviceDisconnectedError(TestRunnerUnavailableError)`
  so existing "runner unavailable" handling (retry, logging) applies.
- `projects/case_faults.py`: type → `DEVICE_DISCONNECTED`, with the ask "Reconnect
  the phone or tablet (cable, Wi-Fi debugging, or start the emulator) and check
  the Test Runner shows it as connected, then Continue." Not a restore fault.
- `projects/person_step_rows.command_shell_label` and `agents/os_shell.shell_label`:
  on a phone target, "Terminal (bash) on the Test Runner computer" (Windows:
  "Command Prompt (cmd.exe) on the Test Runner computer") (Q1).
- `machine_readiness/run_stage0_state.runner_host`: on a phone target, returns
  `"<host>#<device_id>"`, so a requirement status recorded on another phone
  reads unknown (Q5d). `runner_info.RunnerInfo` gains `target`. Readiness pages
  show the device. `quick_check` needs no change: `check_app_installed` is
  answered by the device on a phone runner.
- Manual runs: the `manual_runner` readiness check also requires
  `not device_offline(project)` on a phone target.

### Agents
- `agents/types.py::ToolContext.runner_target: RunnerTarget = RunnerTarget.DESKTOP`.
  It is set by `sub_agent.run_sub_agent`, `person_steps`, `case_teardown`,
  `machine_setup/services/setup_tools` (and any other `ToolContext` builder)
  from the system info they already receive (`parse_runner_target`).
- `agents/services/tool_definitions.py`: `get_mobile_tool_definitions()` (phone
  wording for the shared names, plus `swipe`, `long_press`, `press_button`,
  `rotate_device`, `hide_keyboard`, `open_url`, `install_app`) and
  `tool_definitions_for(target)`. Desktop is today's list, unchanged. The phone
  list keeps `take_screenshot`, `click`, `double_click`, `click_at`, `drag`,
  `type_text`, `key_press` (restricted keys), `scroll`, `launch_app`,
  `close_app`, `check_app_installed`, the command tools, `wait`, web search and
  the knowledge tools. It drops `hover`, `close_tab` and every `browser_*`.
  `get_all_tool_definitions()` stays the **union** (vocabulary stripping in
  `person_step_prompts`) and is no longer used as a runtime default.
- `agents/services/tool_registry.py`: handlers for the new tools.
  `dispatch_tool_call` refuses a tool outside `tool_definitions_for(context.runner_target)`.
  `tool_definitions_named(names, target)` intersects with the target's set, so
  `TEARDOWN_TOOL_NAMES`/`CONFIRM_TOOL_NAMES` keep working on both targets.
  `agent_loop._run_agent_loop` defaults to `tool_definitions_for(context.runner_target)`.
- `agents/services/tools_mobile.py` (new): tool functions. `long_press` reuses
  the verified flow of `click` (`verified_element_actions` with a long-press
  actuator). `swipe` and `rotate_device` take a before/after outcome check only
  with `expected_result`, like `click_at`. `install_app`, `open_url`,
  `press_button` and `hide_keyboard` are plain.
- Prompts: `prompt_parts.build_agent_persona`, `build_environment_context` and
  `build_tool_guidelines` branch on the target: "operating an Android phone
  (Pixel 8, Android 15, emulator) through a Test Runner on a macOS computer;
  screenshots show the phone; shell commands run on the computer; the device id
  is in `$RUNNER_DEVICE_ID`". Also `build_mobile_tool_examples` and
  `build_shell_rules` (adb/devicectl/simctl hints). `orchestrator_prompts`: the
  planner's vocabulary for a phone (tap, swipe, app ids), and close clauses
  keep `close_app`. `person_step_prompts`: hand-offs say "on the phone"; the
  vocabulary stripping includes the new names.
- `machine_setup/services/setup_tools.machine_tool_definitions(target)` gives the
  setup sub-agents the target's tool set. The setup lead's prompt names the
  device.

### Pages and forms
No new URL, form or POST. Read-only display changes:

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:environment` (existing) | GET | `@project_membership_required` | none | `environment.html`: Connection card shows "Target: Android phone · Pixel 8 · Android 15 · emulator", device id, and device status (READY / Starting / Disconnected / Not authorised / Unavailable + escaped detail); live via `agent.status` | none |
| `projects:overview`, wizard `step_machine`, `partials/badge_machine.html`, `dashboard:all_projects`, project table | GET | unchanged | none | badge state `device_offline` ("Phone disconnected") | none |
| `machine_readiness` overview | GET | unchanged | none | shows the device next to the host | none |

Device strings come from the runner. They are rendered with autoescape and
`json_script` only, never `|safe`.

### Background work and WebSockets
- **Runner → server `device_status`**: `controller_consumer` parses it with
  `parse_device_status_payload` and ignores it before authentication, on a
  desktop runner, or more often than once per second. It calls
  `record_device_status` and broadcasts `agent.status` on change.
- **`agent.status` event**: gains `agent_device_state`, `agent_device_detail`
  and `agent_device_reported_at`. `projects/consumers.py` forwards them. The
  Alpine components in the four templates read them.
- **Server → runner**: seven new `controller.<action>` events (TypedDicts in
  `projects/controller_protocol.py`, handlers in `controller_consumer.py` that
  serialize the new `MessageType`s).
- No new Celery task or queue. Phone actions run in the existing `execution`
  and `assist` workers like desktop ones.

### Settings / infrastructure
- `RUNNER_DEVICE_STALE_SECONDS` (default 90), read in `settings.py` via decouple
  and added to `example.env`.
- `INSTALL_APP_TIMEOUT_SECONDS` is a constant in `controller_mobile_actions.py`,
  not a setting.
- Host prerequisites (documented in `controller_client/README` or the
  Environment page help, plus `diagnose --mobile`): Node ≥ 22.22.2 (or 24.15+),
  `npm i -g appium@3`, `appium driver install uiautomator2` / `xcuitest`.
  Android: platform-tools (`adb`) and `ANDROID_HOME`; Java only if the driver
  asks for it. iOS: macOS + Xcode, and for real devices an Apple Team ID for
  WDA signing.
- The server image is unchanged: it imports only `protocol.py`, which stays
  stdlib-only. `scripts/verify_server_image.sh` must still pass.

### Data flow
1. A person sets `CONTROLLER_TARGET=android` and starts the runner. `main`
   discovers the devices, asks for one, saves `CONTROLLER_DEVICE`, selects the
   target, starts Appium on 127.0.0.1 and creates the session.
2. The handshake sends `system_info` with the host OS + `target` + device
   keys, and `MOBILE_DEVICE_V1`. The server stores it, sets the device state
   to `starting`, and the runner's first `device_status` sets READY. The badge
   turns READY when the visual engine is also ready.
3. A run starts. `execute_test_run_test_case` passes `agent_system_info` as
   today. The orchestrator plans with the phone vocabulary, and each sub-agent's
   `ToolContext.runner_target = ANDROID` gives it the phone tools.
4. `click("Login button")`: `controller_find_elements` → `screenshot_request` →
   `MobileBackend.screenshot()` (PNG of the phone) → `parse_screenshot` → vision
   confirm → `controller_click(x, y)` → runner `to_input` (pixels → points on
   iOS) → Appium W3C tap → outcome check.
5. The cable drops. `device_monitor` sends `device_status=disconnected`, and the
   badge shows "Phone disconnected". The next action raises
   `DeviceDisconnectedError` on the runner and the server gets
   `DEVICE_DISCONNECTED`. `case_faults` gives `device_disconnected`: the case
   goes Need help and the run Needs help. Night mode hands it to auto-fix, which
   may run `adb reconnect` or boot the emulator, else ntfy. The person replugs,
   the monitor reports READY, the session is recreated and the person presses
   Continue (current screen).

## Alternatives considered
- **Direct drivers (adb + WDA over HTTP + pymobiledevice3) instead of Appium.**
  The footprint is smaller (no Node) and Android is faster (`adb exec-out
  screencap`). But iOS input still needs WDA built and signed by Xcode, so the
  hard part stays. We would own WDA lifecycle, port forwarding and iOS 17+
  tunnelling ourselves, and pymobiledevice3 is GPL-3.0-or-later. Appium gives
  one API for both platforms and handles session and WDA management. A remote
  Appium URL (self-hosted grid, later clouds) also comes almost for free. Cost:
  Node 22.22+/24.15+ and ~150 MB of npm packages on the host, and slower Android
  screenshots (to be measured in the spike).
- **Running a controller on the phone.** Rejected by the user and by the iOS
  sandbox.
- **Mapping everything onto existing tool names** (`scroll` for swipes,
  `key_press home`). This hides phone semantics from the model and makes
  `key_press` ambiguous. Shared names stay only where the semantics truly match
  (D5, Q4).
- **A separate `PhoneBackend` protocol replacing `DesktopBackend`.** Rejected in
  D1: it would duplicate the verified click, screenshot and Manual paths.
- **Device status inside `agent_system_info`.** Rejected (D6): it is rewritten
  every heartbeat, and readers treat that dict as the immutable handshake.

## Risks and mitigations
- **OmniParser on tall phone screenshots** (1080×2400, 1179×2556, iPad
  2048×2732): small icons may be missed, and latency grows with pixels.
  *Mitigation:* Phase 0 spike. If it helps, the runner downscales phone
  screenshots to a max edge, and `pointer_mapping` absorbs it because capture
  and input sizes already differ.
- **iOS points vs pixels, rotation, compatibility-mode apps.** *Mitigation:*
  `input_size` comes from the live window size per screenshot, and
  `diagnose --mobile` prints the scale. iPad compatibility-mode offsets are a
  documented limitation (spike S2).
- **WDA signing on real iPhones** (Team ID, 7-day free profiles, flaky WDA).
  *Mitigation:* simulators and Android first (phase gate), `diagnose --mobile`
  explains the failures, and the device state `unavailable` carries the
  driver's message (bounded, escaped, server log keeps the full text).
- **Appium session creation is slow** (a WDA build takes minutes the first
  time). It must never happen inside a 30 s action. *Mitigation:* the session
  is created at startup and on device return, the `starting` state keeps the
  badge off READY, and actions during `starting` answer `DEVICE_DISCONNECTED`
  with "still starting".
- **Concurrency on one session** (agent action, Manual capture, readiness check).
  *Mitigation:* the session lock in `mobile_session`. The monitor never uses the
  session.
- **Self-update handover on the same device.** *Mitigation:*
  `update_handover` quits the session and Appium before the spawn, and the new
  runner retries session creation for 60 s.
- **Two runners on one host for two projects.** *Mitigation:* free ports per
  managed Appium and per session (`systemPort`/`wdaLocalPort`/`mjpegServerPort`).
  The same device in two runners is unsupported, and the second session fails
  with a clear `unavailable` detail.
- **Runner reconnects with a different target mid-case.** The prompts were built
  for the old target. *Mitigation:* the runner refuses wrong-target messages
  with `TARGET_MISMATCH`, the step fails with a clear tool error, and the
  evaluate step may ask for help. Accepted for v1.
- **Security.** The Appium server is bound to 127.0.0.1 with no relaxed
  security, so `adb_shell` and similar stay off. `open_url` refuses
  `javascript:`/`file:`. `install_app` refuses network paths and only reads
  host paths the agent names, like the shell already can. Device strings are
  length-bounded on parse and escaped in templates. `device_status` is
  throttled and owner-only. The managed Appium logs are written under
  `controller_client/logs/` and not uploaded.
- **Licensing:** Appium, the drivers, WDA and the Python client are Apache-2.0
  or BSD. No GPL dependency (pymobiledevice3 avoided).
- **FLAG_SECURE false positives:** only an exactly all-zero frame is refused, so
  a dark UI still passes.
- **Black-box `selenium` exceptions in strict mypy:** both packages ship
  types. No stub is needed.

## Implementation steps

Each step is one reviewable commit. **[P-x]** marks steps of the same group that
own disjoint files and can go to parallel sub-agents. Steps without a marker are
sequential.

**Phase 0: Spike (no product code; results appended to this plan)**
0. On a dev Mac: Android emulator + iOS Simulator (iPhone and iPad), Appium 3 +
   drivers, a throwaway script in the scratchpad. Measure:
   - S1: OmniParser recall and latency on 10 screens each, with and without
     downscaling to a 1920 or 1600 max edge.
   - S2: tap accuracy through points/pixels, rotation sizes and screenshot
     orientation.
   - S3: screenshot and tap latency per platform.
   - S4: FLAG_SECURE frame content.
   - S5: an Android USB phone and (if available) an iPhone with a Team ID; does
     the iOS 17+ device need extra tunnel setup for the driver version?
   Record the decisions (downscale edge, timeouts) in the Changelog. (files:
   `docs/plan/feat-mobile-device-runner.md`)

**Phase 1: Shared contracts (sequential, before any sub-agent)**
1. Protocol: `CLIENT_VERSION 0.19.0`, the enums, capability, message types,
   payloads, parsers, error codes, `advertised_capabilities(mobile=)`,
   `parse_runner_target`, `parse_device_info`. (files:
   `controller_client/protocol.py`)
2. Server model + exceptions + fault reason: `RunnerDeviceState`, the three
   Project fields, `BlockedReason.DEVICE_DISCONNECTED`, the migration,
   `DeviceDisconnectedError`. (files: `projects/models.py`,
   `projects/migrations/<new>.py`, `projects/exceptions.py`)
3. Runner seams: the `MobileDevice` protocol, `DeviceDisconnectedError` in the
   runner exceptions, `target.py` with the selection API (the mobile backend
   factory is a stub that raises until step 6), executor backend lookup per
   call, and the `screen_capture`/`env_check`/`system_info` switch to
   `get_target_backend()`. Desktop behaviour must be unchanged. (files:
   `controller_client/desktop_backend.py`, `exceptions.py`, `target.py`,
   `executor.py`, `screen_capture.py`, `env_check.py`, `system_info.py`)

**Phase 2: Runner (parallel after Phase 1)**
4. [P-A] Config + device pick + example.env + setup-script hints. (files:
   `controller_client/config.py`, `main.py`, `example.env`, `scripts/setup.*`)
5. [P-A] Discovery + managed Appium server. (files: `mobile_discovery.py`,
   `mobile_appium_server.py`)
6. [P-A] Session + backend + platform modules + app policy. Wire the factory in
   `target.py` (the only shared touch, done by this sub-agent). (files:
   `mobile_session.py`, `mobile_backend.py`, `mobile_android.py`, `mobile_ios.py`,
   `mobile_app_policy.py`, `target.py`, `requirements.txt`)
7. [P-A] Device monitor. (files: `device_monitor.py`)
8. [P-A] Diagnose `--mobile`. (files: `mobile_diagnostics.py`, `diagnose.py`)
9. After 5–8: the mobile executor + client wiring (handlers, target refusal,
   window-hide skip, monitor lifecycle, `device_status`, error codes, child env)
   + Manual capture skip + cleanup + update handover. (files: `mobile_executor.py`,
   `client.py`, `executor.py`, `manual_capture.py`, `cleanup.py`,
   `update_handover.py`)
   Then run the runner's mypy (`controller_client/.venv/bin/mypy --config-file
   controller_client/setup.cfg controller_client/`).

**Phase 3: Server (parallel with Phase 2, after Phase 1)**
10. [P-B] Controller plumbing: event TypedDicts, consumer action handlers,
    `device_status` handling, the `dispatch_controller_action` alias,
    `controller_mobile_actions.py`, `runner_device_status_services.py`, and the
    error-reply mapping. (files: `projects/controller_protocol.py`,
    `projects/controller_consumer.py`, `projects/services.py` (alias, error
    mapping, `_disconnected_agent_fields`, status event, claim),
    `projects/controller_mobile_actions.py`,
    `projects/runner_device_status_services.py`)
11. [P-B] Fault and machine state: `case_faults`, `MachineState.DEVICE_OFFLINE`
    in `get_machine_state`, `projects/runner_target.py`, and the Manual
    readiness check. Runs after 10, because both touch `projects/services.py`
    (same sub-agent, or sequential). (files: `projects/case_faults.py`,
    `projects/services.py`, `projects/runner_target.py`, the manual readiness
    module)
12. [P-C] Agents tool set: `ToolContext.runner_target`, `tool_definitions_for`,
    registry refusal and handlers, `tools_mobile.py`, the `agent_loop` default,
    the context builders (`sub_agent`, `person_steps`, `case_teardown`), and
    `machine_setup/services/setup_tools.py`. (files: `agents/types.py`,
    `agents/services/tool_definitions.py`, `tool_registry.py`, `tools_mobile.py`,
    `agent_loop.py`, `sub_agent.py`, `person_steps.py`, `case_teardown.py`,
    `machine_setup/services/setup_tools.py`)
13. [P-C] Prompts: the persona/environment/tool guidelines/shell rules per
    target, planner vocabulary, person-step wording, shell labels. (files:
    `agents/services/prompt_parts.py`, `orchestrator_prompts.py`,
    `person_step_prompts.py`, `agents/os_shell.py`, `projects/person_step_rows.py`)
14. [P-D] UI: the status socket fields and the templates (Environment
    connection card, overview, wizard, badge, all projects, readiness overview),
    plus CSS classes in `punk-hazard.css` if needed. (files:
    `projects/consumers.py`, `templates/projects/environment.html`,
    `overview.html`, `wizard/step_machine.html`, `partials/badge_machine.html`,
    `templates/dashboard/all_projects.html`,
    `templates/machine_readiness/overview.html`, `static/css/punk-hazard.css`)
15. [P-D] Readiness identity: `runner_host` with the device id, and
    `RunnerInfo.target`. (files: `machine_readiness/run_stage0_state.py`,
    `machine_readiness/runner_info.py`)
16. Settings: `RUNNER_DEVICE_STALE_SECONDS`. (files: `auto_tester/settings.py`,
    `example.env`)

**Phase 4: Integration and docs (orchestrator, sequential)**
17. Integrate the seams. Run `pre-commit run --all-files`, both mypy configs and
    `scripts/verify_server_image.sh`, then the manual checks below.
18. Docs: a `CLAUDE.md` controller_client paragraph (target, capability,
    fault), `docs/changed/<date>-mobile-device-runner.md`, and a Status update
    here.

Gate: iOS **real-device** support can ship after Android + simulators if S5
shows WDA signing friction (Q7).

## Verification
Manual checks (this project has no automated tests):
- **Desktop regression:** an unchanged desktop runner (0.19.0, `CONTROLLER_TARGET`
  unset) on macOS: a run with click, scroll and a browser step; Manual capture
  still hides the window; `diagnose` and `diagnose --input` unchanged. A 0.18.1
  runner still connects and runs.
- **Android emulator** (`CONTROLLER_TARGET=android`, empty device with one
  emulator):
  - The first interactive start asks for the device and saves it.
  - The Environment page shows target, model, OS and status.
  - `diagnose --mobile` passes.
  - A run on a sample app covers tap, long press, swipe, scroll, type, hide
    keyboard, back, home, rotate, launch/close app, open_url and install_app
    from a host APK path.
  - TestRail evidence shows phone screenshots.
- **iOS Simulator** (iPhone and iPad): the same as Android minus back. Taps land
  on target (points mapping); landscape works after `rotate_device`.
- **Disconnect:** `adb disconnect` / kill the emulator mid-step. The badge shows
  "Phone disconnected", the case goes Need help with the fault "Phone or tablet
  disconnected" (not Failed), reconnect gives READY, and Continue (current
  screen) goes on. Start is refused while the device is offline.
- **FLAG_SECURE app:** take_screenshot returns the secure-window error.
- **HITL:** the person window stays visible during agent actions, the person
  performs on the phone, confirmation screenshots come from the phone, and the
  shell label names the computer.
- **Manual run:** the Cmd+Shift+P mark captures the phone with no window hide;
  the upload works.
- **Machine setup chat** with a phone runner: the tools listed are the phone
  set; `run_setup_task` can `adb install` via execute_command.
- **Self-update** of a phone runner: the new process takes over the device; the
  old one released it.
- **Security spot checks:** a device name with `<script>` renders escaped;
  `open_url("javascript:…")` is refused; the Appium port is not reachable from
  another host.
- Type checks: `mypy .` (server) and the runner's mypy. `pre-commit run
  --all-files`. `scripts/verify_server_image.sh`.
- Then run the `reviewer` and `verifier` agents in parallel.

## Open questions

Resolved 2026-11-06: the user accepted every recommendation and assumption below as stated.

- **Q1 Shell on a phone target.** Recommendation: `execute_command` and the
  interactive command tools keep running **on the host**. That is where `adb`,
  `xcrun` and the builds are. The runner exports `RUNNER_DEVICE_ID` and
  `RUNNER_DEVICE_PLATFORM` to every child, and the shell rules tell the agent to
  use `adb -s "$RUNNER_DEVICE_ID" shell …` (Android) or `xcrun devicectl … /
  xcrun simctl … "$RUNNER_DEVICE_ID"` (iOS). There is no separate device-shell
  tool, and none exists for iOS. `agents/os_shell` keeps the host OS. Person
  steps' shell label becomes "Terminal (bash) on the Test Runner computer", and
  HITL command steps run there. (Assumption used in this draft: as
  recommended.)
- **Q2 What is under test.** Recommendation for v1: **native apps**
  (preinstalled, or `install_app` from a host path: `.apk` on Android; a
  simulator `.app` or a device-signed `.ipa` on iOS, where signing with a
  profile that includes the device is the customer's job), plus **mobile web
  through the visual tools** (`open_url` opens the device's default browser;
  then taps). The DOM `browser_*` tools are not offered on a phone and are
  refused by the runner. A later plan can map them onto Appium web contexts
  (chromedriver for Chrome, the Safari Web Inspector). Builds stored in the KB
  or on the server are not pushed to the host in v1. (Assumption: as
  recommended.)
- **Q3 Appium vs direct drivers.** Recommendation: **Appium 3** (server 3.9.0,
  uiautomator2 9.0.1, xcuitest 13.0.7; Node ≥ 22.22.2 or 24.15+) with
  **Appium-Python-Client 6.1.5** (Apache-2.0, typed). Use plain `adb`/`xcrun`
  only for discovery and the heartbeat, and do not use pymobiledevice3 (GPL).
  The runner manages a localhost Appium server unless `CONTROLLER_APPIUM_URL` is
  set. Not fully clear-cut: if the spike shows Android screenshots via Appium
  are too slow, a direct `adb exec-out screencap -p` fast path for Android
  screenshots only can be added inside `mobile_android.py` without changing the
  design. (Assumption: Appium.)
- **Q4 Tool-set switching and its ripple effects.** Recommendation: the server
  picks the tool set from the handshake target (D4/D5). **Shared names stay**
  (`click`, `double_click`, `click_at`, `drag`, `type_text`, `scroll`,
  `take_screenshot`, `launch_app`, `close_app`, `check_app_installed`,
  restricted `key_press`), described as taps and swipes, so verified flows,
  planner close clauses, teardown/confirm allowlists and digests keep working.
  **Phone-only tools are new** (`swipe`, `long_press`, `press_button`,
  `rotate_device`, `hide_keyboard`, `open_url`, `install_app`). `hover`,
  `close_tab` and `browser_*` are hidden.
  - Planner and prompts: target-aware persona and vocabulary.
  - Person steps (HITL): unchanged flow, the window is not hidden, instructions
    say "on the phone".
  - Manual mode: phone screenshots, no hide; Manual start needs the device
    READY.
  - Readiness: artifacts profile and scan stay on the host, `check_app_installed`
    asks the device, and statuses are keyed by host + device.
  - Prepare machine / setup chat: the phone tool set.
  - Self-update: the venv gets the Appium client through `requirements.txt`.
    Node, Appium and the drivers are OS-level, checked by `diagnose --mobile`
    and startup, never installed by the runner or the updater. A runner whose
    new version cannot start Appium never connects, so the old one reports
    `failed` and stays (existing no-rollback behaviour).
  (Assumption: as recommended.)
- **Q5 Other decisions.**
  - a) **Cleanup between cases** (`cleanup_environment`): recommend terminating
    the apps the agent launched since the last cleanup, hiding the keyboard and
    pressing home. No data reset or uninstall by default. Should a project
    setting "reset app data between cases" (`pm clear` / reinstall) exist?
    (Assumption: no.)
  - b) **App close policy on phones:** never terminate the Appium helper apps,
    WDA or the launcher/SpringBoard. `close_tab` is refused, and `close_app` on
    the app under test while in use follows the existing prompt rule.
    (Assumption: as stated.)
  - c) **Artifacts scan:** keep it on the host, since builds live there; the
    intake asks for the host base directory. (Assumption: keep.)
  - d) **Readiness identity:** key requirement statuses by host + device id, so
    a different phone reads unknown. (Assumption: yes.)
  - e) **Device disconnected:** Need help at once in v1 (continue mode
    `current_screen`, the person may choose restore or from scratch), or should
    the server first wait up to N seconds for the device to return and re-send
    once, like a runner reconnect? (Assumption: immediate Need help.)
  - f) **Night auto-fix** for this fault: allowed (it may `adb reconnect` or
    boot an emulator through the host shell), and a person otherwise.
    (Assumption: allowed.)
  - g) **Badge and gating:** device offline blocks Start/Resume/Re-run/setup
    sends, like visual engine offline. (Assumption: yes.)
- **Q6 Device identity shown in the UI.** The serial or UDID is stored in
  `agent_system_info` and shown to project members (read-only members
  included). Is that acceptable, or should it be shown truncated?
  (Assumption: shown in full to members, never in TestRail or logs sent
  elsewhere.)
- **Q7 iOS real devices in the first release.** WDA signing needs an Apple Team
  ID (`CONTROLLER_IOS_TEAM_ID`) and Xcode on the host; free accounts expire
  after 7 days. Ship Android (emulator + USB/Wi-Fi) and iOS/iPadOS
  **simulators** first, and real iPhones/iPads after the spike confirms the
  signing flow? (Assumption: real iOS devices are in scope but may slip behind
  the gate.)
- **Q8 Remote Appium URL.** Accept `CONTROLLER_APPIUM_URL` for a self-hosted
  Appium (e.g. a Mac mini with devices) now, and leave cloud vendors (auth,
  vendor capabilities, app upload) to a later plan? (Assumption: yes, a
  self-hosted URL is supported but only smoke-checked; clouds are out.)
- **Q9 Screenshot downscaling.** Whether phone screenshots are downscaled
  before OmniParser and the vision model depends on spike S1.
  (Assumption: no downscale until measured.)

## Changelog
- 2026-11-06: created (Draft). Versions checked: Appium 3.9.0, uiautomator2
  9.0.1, xcuitest 13.0.7, Appium-Python-Client 6.1.5, pymobiledevice3 11.50.0
  (GPL-3.0-or-later).
- 2026-11-06: open questions Q1-Q9 resolved as recommended (user). Status In progress.
- 2026-11-06: implemented (Phases 1-4) on branch claude/phone-testing-controller-8b3228. Phase 0 spike and the manual device checks not run: this machine has no Android SDK, Appium, full Xcode or Docker (`verify_server_image.sh` not run either).
- 2026-11-06: deviations: extra runner modules `mobile_platform.py`, `mobile_gestures.py`, `mobile_apps.py`, `mobile_checks.py` (import cycles, typed W3C actions); `mobile_factory.py` builds the backend so `target.py` imports no Appium; selenium pinned `~=4.51.0` (4.52 needs a newer certifi than our pin); detached start waits 60 s for `CONTROLLER_DEVICE`; `intent:` URLs refused too; person-step text keeps the word "swipe".
- 2026-11-06: review fixes: device_status throttle drops only repeats; `reacquire()` after a failed self-update and no start after release; runner timeouts under the server reply timeouts; iOS discovery by kind; fixed Appium log at `--log-level info`; iOS window size cached; Night auto-fix gets a tool error (not a lost turn) for a disconnected phone; one badge rule `runner_target.device_machine_state`; no `Any` in phone actions.
- 2026-11-06: rejected review nit: moving app lifecycle onto the target backend to remove the `get_mobile_device()` branches in `client.py` would force desktop backends to grow phone semantics; the branches stay local to the handlers.
- 2026-11-06: second review fixes: first screenshot allowed once the session is up; `close_app` and the device cleanup each run in one call (cleanup stops starting requests that would not fit); per-request device timeout 11 s so two requests fit the 24 s budget; the post-timeout device lookup is bounded by the budget left; status reads STARTING while an action recreates the session inline (no second start from the monitor); the auto-fix tool error keeps the server's reason and no shell-specific command; `android-app:` refused too.
- 2026-11-06: rejected: a rate floor for changing device statuses (verifier, Low). Only the authenticated owner connection of the project can send them, each write is one conditional UPDATE, and a floor would drop real changes again.
- 2026-11-06: third review fixes: the cleanup deadline is set before the session call (lock wait and an inline recreate share it); keyboard and home steps budget two requests; a cleanup step's read timeout or socket error is a line, a lost session is re-raised for the recreate; `recreating` is set before the old session is quit; the post-timeout lookup needs at least 5 s, else "did not answer in time".
- 2026-11-06: accepted (documented in `mobile_session.py`): resolving an app by visible name can take three or four requests, so on a degraded device such an action may answer after the server's 30 s and read as "Test Runner stopped responding" (Need help, restore). The auto-fix tool error carries the server's fixed text, not the runner's reason (by design: runner text stays in the log).
- 2026-11-06: fourth verification fix: with the deadline set before the call, the two-request keyboard/home steps could never fit. Device cleanup now goes home first (always; it also dismisses the keyboard and records the launcher), then closes the launched apps while one more request fits; the cleanup budget equals the 24 s action budget. Host cleanup runs before it in the same reply, so a slow host cleanup plus a slow phone can still pass the server's 30 s (accepted).
- 2026-11-06: final cleanup fixes: a failed home press is a line and the launched apps are still closed; the launcher is recorded only when that request still fits the deadline. Accepted: the 30 s server wait covers the host cleanup and the device cleanup (up to 24 s) together.
