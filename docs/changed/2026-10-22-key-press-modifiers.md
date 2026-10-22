# Press modifier keys in key_press combinations

Date: 2026-10-22
Plan: inline (one-file fix)

## What changed
- `controller_client/executor.py` `execute_key_press` / `_normalize_key`: each key name is mapped through a per-platform alias table (`_KEY_ALIASES`) and validated with `pyautogui.isValidKey` before pressing.

## Why
pyautogui's macOS backend silently skips key names it does not know. It has `command` but not `cmd`, so `cmd+n` / `cmd+t` pressed a bare `n` / `t`.

## Behaviour and contract changes
- macOS: `cmd`/`meta`/`super`/`win` → `command`, `opt` → `option`. Windows/Linux: `cmd`/`command`/`meta`/`super` → `win`, `opt`/`option` → `alt`. Everywhere: `control` → `ctrl`.
- Unknown or empty key names (e.g. a typo, or `ctrl++`) now fail with `ExecutionError` instead of being dropped silently.
- Single letters inside a combination are lowercased (`cmd+N` = Cmd+N, not Cmd+Shift+N). A lone `A` still types shift+a.

## Notes for future changes
- Only the controller changed: test machines need the new `executor.py` plus a restart. The ZIP download picks it up once the server is redeployed.
- On Windows, `cmd` means the Win key, not Ctrl. A Mac-written "cmd+t" will press Win+T there.
- On X11, a keysym the display lacks maps to keycode 0, so it passes `isValidKey` (pyautogui quirk).
- The `+` key itself still can't be pressed through `key_press`.
