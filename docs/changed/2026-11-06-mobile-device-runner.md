# Phone and tablet testing through the Test Runner

Date: 2026-11-06
Plan: docs/plan/feat-mobile-device-runner.md

## What changed
- `controller_client/`: `CONTROLLER_TARGET=android|ios` turns the runner into a phone runner for one device (`CONTROLLER_DEVICE`, picked at the first interactive start). `target.py` holds the selected backend; `mobile_backend.py` implements `DesktopBackend` + `MobileDevice` (`mobile_device.py`) on Appium 3 (`mobile_session.py`, `mobile_android.py`, `mobile_ios.py`, managed localhost Appium in `mobile_appium_server.py`), `mobile_discovery.py` lists devices with adb / devicectl / simctl, `device_monitor.py` creates the session and reports `device_status`, `mobile_executor.py` answers the phone messages, `python -m controller_client.diagnose --mobile` checks prerequisites and the device. CLIENT_VERSION 0.19.0.
- `protocol.py`: `RunnerTarget`, device keys in `system_info` (`parse_runner_target`, `parse_device_info`), `MOBILE_DEVICE_V1`, messages `swipe`, `long_press`, `press_button`, `rotate_device`, `hide_keyboard`, `open_url`, `install_app`, `device_status`, error codes `DEVICE_DISCONNECTED`, `TARGET_MISMATCH`.
- Server: `projects/controller_mobile_actions.py` (phone actions), `runner_device_status_services.py`, `runner_target.py` (`device_machine_state`, the one badge rule), `MachineState.DEVICE_OFFLINE` ("Phone disconnected"), `DeviceDisconnectedError` → fault `BlockedReason.DEVICE_DISCONNECTED` (Need help, continue from current screen).
- Agents: `tool_definitions_for(target)` / `ToolContext.runner_target`; phone tools in `agents/services/tools_mobile.py`; target-aware prompts (`prompt_parts`, `orchestrator_prompts`, `person_step_prompts`, setup prompts); `agents/runner_device.py`.
- UI: device line and live device status on Environment, overview, wizard, Machine setup chat, project tables and all projects; readiness statuses keyed by `host#device_id`.

## Why
Test Android phones, iPhones and iPads with the same Test Runner source: the phone cannot run the controller (iOS sandbox), so the runner on a host computer drives it.

## Behaviour and contract changes
- New runner `.env` keys: `CONTROLLER_TARGET`, `CONTROLLER_DEVICE`, `CONTROLLER_APPIUM_URL`, `CONTROLLER_APPIUM_COMMAND`, `CONTROLLER_IOS_TEAM_ID`, `CONTROLLER_IOS_SIGNING_ID`, `CONTROLLER_IOS_WDA_BUNDLE_ID`, `CONTROLLER_IOS_PREINSTALLED_WDA`. New runner deps: Appium-Python-Client, selenium (pinned 4.51 because of the certifi pin). Host needs Node ≥ 22.22.2 / 24.15+, `appium@3` and its driver (never installed by the runner).
- New server setting `RUNNER_DEVICE_STALE_SECONDS` (90). Migration `projects/0058_mobile_device_status` (three `Project.agent_device_*` fields, choice changes only otherwise).
- `agent.status` event gains `agent_device_state`, `agent_device_detail`, `agent_device_reported_at`.
- A phone runner refuses `hover`, `close_tab` and `browser_*` (`TARGET_MISMATCH`); shell commands run on the host with `RUNNER_DEVICE_ID` / `RUNNER_DEVICE_PLATFORM` in the env. A phone that is not READY blocks Start, Resume, Re-run, Manual runs and setup sends; Night auto-fix may still try to reconnect it.
- Desktop runners and runners < 0.19.0: unchanged (no `target`, same tools and prompts).

## Notes for future changes
- Not yet run on a device: the Phase 0 spike (OmniParser on tall phone screenshots, iOS points vs pixels, latency, FLAG_SECURE) and the plan's manual checks need an Android emulator and an iOS Simulator. Real iPhones need WDA signing (Team ID).
- The server stores the device keys only when MOBILE_DEVICE_V1 is advertised; a non-blank `agent_device_state` marks a phone runner. Keep `device_machine_state` and `punkHazard.deviceMachineState` in step.
- `MobileBackend.stop()` releases the device for a self-update; `reacquire()` undoes it when the update fails.
- Phone action timing: each runner call answers within 24 s (lock wait ≤ 10 s, 11 s per device request; install 270 s) so the server's 30 s / 300 s waits see a real answer. Device cleanup goes home first, then closes launched apps while time remains. An app resolved by visible name on a slow device can still answer late (accepted, see the plan Changelog).
- `browser_*` on a phone (Appium web contexts) and device clouds are later plans.
