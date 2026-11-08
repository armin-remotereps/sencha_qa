# Screenshot capture: pick phone apps from the device

Date: 2026-11-08
Plan: docs/plan/feat-capture-phone-app-picker.md

## What changed
- `controller_client/protocol.py`: CLIENT_VERSION 0.23.0; `DEVICE_APPS_V1` (phone runners only), `list_device_apps` / `device_apps_result`, `DeviceAppsStatus`, payloads, limits, serializer and parser.
- `controller_client/device_apps.py` (new), `mobile_platform/ios/android/device/backend.py`: `installed_apps` (was `_list_apps`); `client.py` handler (phone-only, window-safe).
- `projects/controller_mobile_actions.controller_list_device_apps`, consumer forward and result cap table (`_RESULT_MAX_BYTES`).
- `projects/capture_phone_apps_services.py` (new): button state (no query) and the load (refused while an agent run is Started; one load per project via a cache lock).
- `projects/project_capture_apps_services.py`: `add_capture_allowed_processes` (row lock, merge, cap, returns how many were new) and `stored_capture_apps` (one reader, also used by `runner_capture_readiness` and the settings page).
- Settings "Screenshot capture" card: "Pick from the phone's apps" picker (names + ids, Android ids only, filter box) and a phone paragraph in the hint.

## Why
Phone capture matches the foreground app's bundle id / package, which users could not find. The version bump also makes Update Test Runner deliver the Appium exit-order fix (23db5cf), which shipped without one.

## Behaviour and contract changes
- New POST pages `projects:settings_capture_phone_apps` (`settings/capture-apps/phone-apps/`, renders Settings with the list) and `projects:settings_capture_apps_add` (`settings/capture-apps/add/`); writable members only.
- Controller protocol: `list_device_apps` → `device_apps_result` (`DEVICE_APPS_V1`, optional, checked on the card). Every connected runner is offered Update Test Runner (0.23.0).
- No migrations, no settings.

## Notes for future changes
- The load is a synchronous view call (≤ `DEVICE_APPS_TIMEOUT_SECONDS`); runner names are untrusted text, rendered escaped, never stored.
- Not yet run on a real phone or emulator.
