# diagnose --input: live checks for run-speed round 1

Date: 2026-10-28
Plan: docs/plan/feat-run-speed-live-checks.md

## What changed
- `controller_client/diagnose.py`:
  - `--input` runs the new live input checks and ends with a paste-back summary between `----- BEGIN/END PUNK HAZARD INPUT CHECK -----`. It exits 1 on any MISMATCH/FAIL/NO.
  - `--no-interactive` runs only the automatic checks.
  - `--input` cannot be combined with `--browsers`.
- `controller_client/input_diagnostics.py` (new), checks in order:
  - spaces;
  - os-scale (measured sizes vs the OS display scale);
  - pointer read-back and a pointer position a person confirms;
  - scroll direction (person-confirmed);
  - modifiers still held after `press_combination`, read before and after the release and 250 ms later;
  - an opt-in macOS TextEdit reproduction of the emoji-picker sequence, guarded by a frontmost-window check.
- `controller_client/display_scale.py` (new): `read_display_scale()` per OS (never raises) and `compare_spaces()`.
  - macOS: Quartz display mode, pixels vs points.
  - Windows: `EnumDisplaySettingsW` physical size, the true monitor DPI read under a temporary per-monitor thread context, plus DPI awareness and system DPI.
  - GNOME: no reference.
- `controller_client/desktop_pyautogui.py`: public `held_modifiers()` and `press_combination()`, which `press_keys` now calls; `_release_modifiers` returns what it released. Runtime behaviour is unchanged.
- `controller_client/executor.py`: `_to_input` renamed to `to_input`.

## Why
Round 1 left three assumptions that only a real desktop can confirm: the pointer mapping's units (Retina, Windows DPI), scroll signs per OS, and whether modifiers stay held after a key combination (the emoji picker, findings Q4). The OpenAI `tool_choice="none"` assumption was checked on the production api container instead: accepted, plain RESULT text returned.

## Behaviour and contract changes
- New CLI flags only. No protocol change and no `CLIENT_VERSION` bump.

## Notes for future changes
- Run `python -m controller_client.diagnose --input` on each runner OS and paste the summary back. If a scroll direction is wrong, flip it in `_MAC_SCROLL_DELTAS`, `_WIN_SCROLL_EVENTS` or `_SCROLL_AXES`.
- A Windows "no" for scroll is only meaningful with wheel routing on pointer/hybrid; the summary reports it.
- A real Retina Mac read 5120x2880 px / 2560x1440 pt with `pyautogui.size()` 2560x1440, which matches the mapping's assumption.
