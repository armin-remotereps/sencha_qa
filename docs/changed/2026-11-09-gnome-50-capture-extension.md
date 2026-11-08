# GNOME 50 for the Test Runner capture extension

Date: 2026-11-09
Plan: inline (trivial fix)

## What changed
- `controller_client/gnome_capture_helper/metadata.json`: `shell-version` adds `"50"`.
- `controller_client/protocol.py`: `CLIENT_VERSION` 0.23.0 → 0.23.1, so Update Test Runner is offered and reinstalls the extension.

## Why
Ubuntu 26.04 ships GNOME Shell 50, which marked the extension OUT OF DATE and never loaded it, so `list_windows` / `capture_screenshot` answered `helper_missing` while full-screen screenshots (Mutter ScreenCast) worked.

## Behaviour and contract changes
- none (no protocol or capability change).

## Notes for future changes
- Confirmed on GNOME Shell 50.1 (Ubuntu 26.04 VM): the extension loads (State ACTIVE) with no code change.
- Each new GNOME major needs its number added here; check `gnome-extensions info test-runner-capture@punk-hazard` for OUT OF DATE first when the helper is reported missing.
- After the update, the person must log out and back in once so GNOME loads the reinstalled extension.
