# feat: Screenshot capture, pick phone apps from the device

Status: Done
Created: 2026-11-08
Request: On the Settings tab's "Screenshot capture" card, a writable member of a
project whose Test Runner drives a phone or tablet presses "Pick from the phone's
apps". The server asks the connected runner for the device's installed apps, the
card shows them by visible name with the app id as secondary text (Android: ids
only, no names), the member ticks some, and the ticked ids are merged into
`Project.capture_allowed_processes` (ids only, no duplicates, existing cap). The
card's hint explains phones (bundle id on iOS, package on Android, the picker).
`CLIENT_VERSION` is bumped so Update Test Runner offers this feature together with
the already-committed Appium exit-order fix (23db5cf), which shipped without a bump.
Matching by app name was rejected (names are not unique and can be spoofed; the
allowlist is a privacy boundary).

## Context

- Card: `templates/projects/settings.html` `#screenshot-capture` (textarea form,
  POST `projects:settings_capture_apps`); context built by
  `projects/testrail_views.render_project_settings` (`capture_apps_form`,
  `capture_apps`). View `projects/project_capture_apps_views.settings_capture_apps`,
  form `projects/forms.ProjectCaptureAppsForm`, the only writer
  `projects/project_capture_apps_services.set_capture_allowed_processes`
  (`CaptureAppsError`). Limits: `controller_client/protocol.py`
  `CAPTURE_ALLOWED_PROCESSES_MAX` = 20, `CAPTURE_PROCESS_NAME_MAX_CHARS` = 64,
  `normalize_process_name` (lower case, `[a-z0-9 ._+-]`, strips one `.exe`/`.app`).
- Phone matching: `controller_client/window_capture.py` `_list_phone` /
  `_phone_grab` compare `normalize_process_name(device.foreground_app())` with the
  allowlist. So an id that does not normalize can never be captured, and the stored
  form is the normalized id (iOS `com.apple.MobileSMS` -> `com.apple.mobilesms`).
- Installed apps: `controller_client/mobile_ios.py` `_list_apps` (two
  `mobile: listApps` calls, `User` + `System`, names from `CFBundleDisplayName` /
  `CFBundleName`) and `controller_client/mobile_android.py` `_list_apps` (package
  ids only). Both are private and used only inside the platform modules
  (`resolve_app`, `install_app`). **No controller message returns the app list
  today**: `check_app_installed` answers for one app, the agent's phone tools
  (`agents/services/tools_mobile.py`) have no list tool. A new message is needed.
- UiAutomator2 `mobile: listApps` returns a map package -> undocumented properties
  (driver >= 7.0.0; a list in 6.9.x; API >= 26); no label is documented, so Android
  stays ids only. Appium 3 / XCUITest `mobile: listApps` with `applicationType`.
- Runner seam: `MobileDevice` (`controller_client/mobile_device.py`) implemented by
  `controller_client/mobile_backend.py` through `self._session.call(...)`
  (`mobile_session.py`: 24 s action budget, 10 s lock wait, 11 s per HTTP command);
  platform protocol `controller_client/mobile_platform.MobilePlatform`.
- Server seam: phone actions in `projects/controller_mobile_actions.py` over
  `projects.services.dispatch_controller_action` (runner-local, no resend);
  result-object pattern of `projects/controller_capture_actions.controller_list_windows`
  (`window_list_result`, consumer size cap in `projects/controller_consumer.py`
  `_handle_window_result`, `projects/controller_reply_tracker.send_window_result`,
  event TypedDicts in `projects/controller_protocol.py`).
- Phone/online/version state without a query: `projects/runner_target.device_offline`,
  `RunnerDeviceState.NONE`, `projects/runner_capability_services.capabilities_include`;
  `projects/runner_capture_readiness.py` shows the same rules for MCP.
- Update offer: `projects/runner_update_offer.get_runner_update_offer` offers
  `AVAILABLE` only when `runner_is_older(agent_client_version)`, i.e. the runner's
  reported version is below the server's `CLIENT_VERSION`;
  `runner_update_request._check_gates` refuses otherwise. The bundle
  (`projects/runner_bundle.update_bundle`, `functools.cache`, built from the
  server's `controller_client/`) carries whatever source the server has, but a
  0.22.0 runner against a 0.22.0 server is `UP_TO_DATE` and gets no Update button.
  **So the bump is what makes Update Test Runner deliver 23db5cf**; without it the
  fix only reaches runners that are downloaded again.

## Goals
- Writable member, phone runner connected, device READY, runner new enough: one
  button loads the device's apps; ticked ids are merged into the allowlist.
- Names shown as escaped, bounded, untrusted text; only ids are stored.
- Hint text explains phone ids and the picker.
- `CLIENT_VERSION` 0.22.0 -> 0.23.0.

## Non-goals
- Matching by app name; storing names.
- Any change to MCP tools, `list_windows` / `capture_window`, or the no-trace rules.
- Removing entries through the picker (the textarea still edits the whole list).
- Android app labels (would need `aapt`/`dumpsys` on the host).
- A picker for desktop runners (process names stay typed).

## Design

### Protocol (`controller_client/protocol.py`)
- `CLIENT_VERSION = "0.23.0"` (new optional capability; minor bump as for 0.20-0.22).
- `MessageType.LIST_DEVICE_APPS = "list_device_apps"` (server -> runner, only
  `request_id`) and `MessageType.DEVICE_APPS_RESULT = "device_apps_result"`
  (runner -> server, same `request_id`, `success: true`, result object under
  `result`, like `window_list_result`; a refusal is a `status`, never `error`).
- `ClientCapability.DEVICE_APPS_V1`: optional, checked where used (the Settings
  card), never in `REQUIRED_CLIENT_CAPABILITIES`. Advertised only on a phone target:
  `advertised_capabilities` drops it together with `MOBILE_DEVICE_V1` (one
  `_MOBILE_CAPABILITIES` set). Comment wording: "the installed apps of the phone";
  no mention of capture tools from MCP.
- `DeviceAppsStatus` (StrEnum): `ok`, `unsupported` (driver cannot list apps),
  `device_unavailable` (device gone or starting), `failed`.
- Dataclasses: `DeviceAppPayload(app_id: str, name: str)`,
  `DeviceAppsResultPayload(status, detail, apps: tuple[DeviceAppPayload, ...],
  names_available: bool, skipped: int, truncated: bool)`.
- Limits: `DEVICE_APPS_MAX` = 1000 entries, `DEVICE_APP_NAME_MAX_CHARS` = 64,
  `DEVICE_APPS_RESULT_MAX_BYTES` (sized from the two, with an `assert` that it fits
  `MAX_MESSAGE_SIZE`, as `WINDOW_CAPTURE_RESULT_MAX_BYTES` does).
- `serialize_device_apps_result` / `serialize_device_apps_result_message` (runner),
  `parse_device_apps_result` (server). The parser re-checks: every `app_id` must
  equal `normalize_process_name(app_id)`, no duplicate ids, count <= max, names
  stripped of control and format characters (Cc/Cf) and cut to the max; detail cut
  like `CAPTURE_DETAIL_MAX_CHARS`. Invalid -> `ProtocolError`.
- `serialize_list_device_apps_payload()` (server; empty dict).

### Test Runner (`controller_client/`)
- `mobile_platform.MobilePlatform.installed_apps(driver) -> dict[str, str] | None`;
  `mobile_ios.py` / `mobile_android.py` rename `_list_apps` to `installed_apps`
  (internal callers updated, behaviour unchanged).
- `mobile_device.MobileDevice.installed_apps() -> dict[str, str] | None`;
  `mobile_backend.py` implements it as one `self._session.call(...)`.
- New `controller_client/device_apps.py`: `list_device_apps() ->
  DeviceAppsResultPayload` (blocking). Normalizes each id with
  `normalize_process_name`, drops ones that do not normalize (counted in
  `skipped`), dedupes, sorts by name (casefold) then id, caps at `DEVICE_APPS_MAX`
  (`truncated`), `names_available` = target is iOS. `None` from the platform ->
  `unsupported`; device missing/still starting -> `device_unavailable`;
  `ExecutionError` -> `failed` with a fixed detail. Logs counts only.
- `client.py`: `_handle_list_device_apps` runs it in a thread and sends
  `device_apps_result`; `LIST_DEVICE_APPS` added to `_WINDOW_SAFE_MESSAGE_TYPES`
  (reads nothing on screen, so the person window stays up) and
  `_PHONE_ONLY_MESSAGE_TYPES` (desktop answers TARGET_MISMATCH).

### Services (server)
- `projects/controller_mobile_actions.py`: `controller_list_device_apps(project_id,
  *, timeout: float) -> DeviceAppList` (frozen dataclass mirroring the payload),
  `runner_local=True`, `resend_on_reconnect=False`; constant
  `DEVICE_APPS_TIMEOUT_SECONDS = 30` (runner budget 24 s + margin). Raises
  `ControllerNotConnectedError`, `TestRunnerUnresponsiveError`,
  `ControllerActionError` (error reply or invalid result).
- New `projects/capture_phone_apps_services.py` (use case "offer the phone's apps
  for the capture allowlist"):
  - `phone_app_picker(project) -> PhoneAppPicker(show: bool, enabled: bool,
    reason: str)`, no query, on the loaded project: `show` = connected phone runner
    (`agent_connected` and `agent_device_state != NONE`); `enabled` additionally
    needs `capabilities_include(..., DEVICE_APPS_V1)` and
    `not device_offline(project)`; `reason` is fixed text ("Update the Test Runner
    to pick apps from the phone.", `DEVICE_NOT_READY_MESSAGE`).
  - `load_phone_apps(project) -> PhoneAppList` re-checks `enabled` (raises
    `CapturePhoneAppsError`), calls `controller_list_device_apps` (no transaction
    held), maps controller exceptions and non-ok statuses to
    `CapturePhoneAppsError` with fixed user text, and returns rows
    `PhoneAppRow(app_id, name, allowed)` (`allowed` = already in the allowlist),
    `names_available`, `skipped`, `truncated`, `remaining` (cap minus current count).
- `projects/project_capture_apps_services.py` (stays the only writer):
  `add_capture_allowed_processes(*, project, app_ids) -> list[str]` in
  `transaction.atomic` with `select_for_update` on the project row; normalizes,
  appends new ids after the existing ones in order, no duplicates; more than
  `CAPTURE_ALLOWED_PROCESSES_MAX` -> `CaptureAppsError`, nothing written.
- `projects/exceptions.py`: `CapturePhoneAppsError(Exception)`.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<id>/settings/capture-apps/phone-apps/` `projects:settings_capture_phone_apps` | POST | `@project_membership_required` (writable; read-only gets 403) | none (CSRF only) | `projects/settings.html` via `render_project_settings(..., phone_apps=PhoneAppList)`; status 200, no redirect | `CapturePhoneAppsError` -> same page with `phone_apps_error` on the card |
| `/projects/<id>/settings/capture-apps/add/` `projects:settings_capture_apps_add` | POST | same | `ProjectCaptureAppsAddForm` (`apps`: list of ticked ids via `CheckboxSelectMultiple`'s `getlist`; `clean_apps` normalizes each, refuses invalid ids and more than the cap) | success: message + redirect `#screenshot-capture` | invalid / `CaptureAppsError` -> settings page with `capture_apps_add_error` |

- `render_project_settings` gains keyword args `phone_apps: PhoneAppList | None`,
  `phone_apps_error: str`, `capture_apps_add_error: str` and the context key
  `phone_app_picker` (`phone_app_picker(project)`). Update the template's context
  contract comment.
- Card: for writable members when `phone_app_picker.show`: a small form with the
  button "Pick from the phone's apps" (disabled with `reason` when not enabled).
  With `phone_apps`: a list of checkboxes (name bold, id in `<code>` beneath; on
  Android the id only and a note "Android does not report app names; the list shows
  package ids"), already allowed rows ticked and disabled, a filter text box and a
  "N of M left" counter (Alpine `x-data` on the card only: filter and count; no
  fetch), notes for `skipped` / `truncated`, and "Add selected". Names rendered
  with autoescape only, never `|safe`. Read-only members see the chips as today.
- Hint text: add one paragraph: on a phone or tablet, list the app's bundle id
  (iOS, e.g. `com.apple.mobilesafari`) or package (Android, e.g.
  `com.android.chrome`), or use "Pick from the phone's apps" while the Test Runner
  is connected to the device.
- Classes: reuse `.checklist`, `.field`, `.chip`; add any missing class to
  `static/css/punk-hazard.css`, no inline styles.

### Background work and WebSockets
- No Celery task. The load is a synchronous view call bounded by
  `DEVICE_APPS_TIMEOUT_SECONDS` (Daphne runs sync views in its thread pool).
- Consumer: `controller_list_device_apps` forwards the event
  (`ListDeviceAppsActionEvent(BaseActionEvent)` in `projects/controller_protocol.py`);
  `DEVICE_APPS_RESULT` joins the `_handle_window_result` branch with its own cap
  (`DEVICE_APPS_RESULT_MAX_BYTES`) and the same reply-tracker forward (generalize
  the cap selection and log wording, no new tracker method).
- No machine lock: the call reads the app list only; the runner serializes Appium
  calls in its own session lock. Not gated by runs, MCP claims or setup turns.

### Settings / infrastructure
- No env vars. Server processes rebuild `update_bundle()` on restart (normal deploy).

### Data flow
1. GET Settings -> `phone_app_picker(project)` decides button state.
2. POST phone-apps -> view -> `load_phone_apps` -> `controller_list_device_apps`
   -> consumer -> runner `list_device_apps` -> `device_apps.list_device_apps` ->
   `MobileDevice.installed_apps` -> `device_apps_result` -> parse -> rows -> page.
3. Member ticks -> POST add -> form -> `add_capture_allowed_processes` (row lock,
   merge, cap) -> redirect.
4. MCP `list_windows` / `capture_screenshot` match the stored ids unchanged.

## Alternatives considered
- Reuse `list_windows` with a flag: mixes "what may be captured now" with "what
  is installed" and changes a message the MCP path relies on. Rejected.
- Reuse `check_app_installed`: one app per call, no names. Rejected.
- Celery task + WebSocket push of the list: more parts (task, queue, socket
  event, client state) for a rare, bounded, read-only call. Rejected.
- Alpine fetch of a JSON endpoint: a REST surface the project forbids. Rejected.
- Keep the fetched list in the session between load and add: unnecessary; the add
  form validates ids on its own (a member may type any id in the textarea anyway).

## Risks and mitigations
- Untrusted runner text in a page: parser strips Cc/Cf and bounds length;
  autoescape only. Ids pass `normalize_process_name` on both ends.
- Blocking request thread up to 30 s: rare, writable-only action; bounded timeout;
  runner-local so no wait for reconnects.
- Large Android lists (hundreds of packages): count and byte caps, consumer refuses
  oversized replies, client-side filter box.
- Lost update on concurrent allowlist edits: merge under `select_for_update`.
- Older runners: `DEVICE_APPS_V1` absent -> button disabled with the update reason;
  the server never sends the message to them. Desktop runners never advertise it.
- Version bump: every connected runner (desktop and phone) is offered an update;
  intended. Phone runners need `SELF_UPDATE_V1` (all >= 0.8.0 have it).
- Listing apps during a running case waits for the runner's session lock (up to
  10 s) and may then answer `failed`/busy; shown as a retryable message.

## Implementation steps
1. Protocol: version, messages, capability + `advertised_capabilities`, status,
   dataclasses, limits, serializers/parsers (files: `controller_client/protocol.py`).
2. Runner: `installed_apps` on platform/device/backend, `device_apps.py`, client
   handler and message sets (files: `controller_client/mobile_platform.py`,
   `mobile_ios.py`, `mobile_android.py`, `mobile_device.py`, `mobile_backend.py`,
   new `device_apps.py`, `client.py`).
3. Server transport: event type, consumer forward and result routing, action
   function (files: `projects/controller_protocol.py`,
   `projects/controller_consumer.py`, `projects/controller_mobile_actions.py`).
4. Services: exception, picker state + load, merge writer (files:
   `projects/exceptions.py`, new `projects/capture_phone_apps_services.py`,
   `projects/project_capture_apps_services.py`).
5. Form, views, URLs, settings render, template, CSS (files: `projects/forms.py`,
   `projects/project_capture_apps_views.py`, `projects/urls.py`,
   `projects/testrail_views.py`, `templates/projects/settings.html`,
   `static/css/punk-hazard.css` if needed).
6. Docs: CLAUDE.md controller paragraph (`DEVICE_APPS_V1`, CLIENT_VERSION 0.23.0)
   and the `docs/changed/` note naming 23db5cf as shipped in 0.23.0.

Steps 1 first; then 2 and 3+4 can run in parallel (different files); 5 after 4.

## Verification
- `pre-commit run --all-files`; `mypy .`; controller mypy with its config.
- iOS simulator runner on 0.23.0: card shows the button; load lists apps with
  names; tick Safari -> `com.apple.mobilesafari` stored; MCP `list_windows` with
  Safari in front lists the device.
- Android emulator: ids only with the note; filter works; add merges, no duplicates.
- Cap: with 19 stored, ticking 2 -> error, nothing written.
- Read-only member: no button; direct POST -> 403.
- 0.22.0 phone runner: button disabled with the update reason; Environment card
  offers Update Test Runner (0.22.0 -> 0.23.0); after update the button works and
  `diagnose --mobile` ends without the urllib3 retries (23db5cf).
- Desktop runner: no button; card text unchanged except the phone paragraph.
- Device unplugged: button disabled with `DEVICE_NOT_READY_MESSAGE`; unplugged
  between page load and press -> inline error.
- A name with markup/control characters renders as text.
- Run `reviewer` and `verifier`.

## Open questions
- Q1 (resolved 2026-11-08, user): Android shows every installed package, sorted,
  with the filter box; no user-installed-only code path.

## Changelog
- 2026-11-08: created
- 2026-11-08: Q1 resolved (all packages + filter box); status In progress
- 2026-11-08: implemented. Review fixes: `load_phone_apps` refuses while an agent run is Started and holds a per-project cache lock (one load at a time); one reader `stored_capture_apps`; "Added N" counts only new ids. Rejected: the redundant length check in `protocol._parse_device_app` (harmless, kept as defence in depth); keeping the loaded list after a refused add (plan already chose not to keep it between requests).
