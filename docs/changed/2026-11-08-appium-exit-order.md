# Appium exit order in phone diagnostics

Date: 2026-11-08
Plan: inline (trivial fix)

## What changed
- `controller_client/mobile_appium_server.py` › `ManagedAppiumServer`: the atexit safety net (`stop`) is registered in `__init__` instead of on first spawn; `_atexit_registered` removed.

## Why
`python -m controller_client.diagnose --mobile` ended with ten urllib3 "Connection refused" retries: atexit runs last in, first out, and the server registered on spawn (after `mobile_diagnostics` registered `MobileTargetBackend.stop`), so Appium was stopped before the session was quit.

## Behaviour and contract changes
- none (diagnose now quits the Appium session before stopping Appium; the runner's normal exit already stopped the device in `main.py`'s `finally`).

## Notes for future changes
- Owners that need the session quit first must register their own exit handler after building the server (see the class docstring).
