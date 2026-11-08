# Window capture: terminals may be captured

Date: 2026-11-09
Plan: inline (user asked to drop the policy)

## What changed
- `controller_client/window_capture_policy.py`: the deny list no longer holds terminals (Linux terminal names, macOS terminal names and bundle ids, Windows terminal/console exes) or the runner's ancestor pids. Still denied: the runner's own process and helpers (person window), and the OS shell.
- `templates/projects/settings.html`: Screenshot capture card no longer says terminals are never captured.
- `controller_client/protocol.py`: `CLIENT_VERSION` 0.23.1 → 0.23.2.

## Why
Single-process terminals (Ptyxis on Ubuntu 26.04, gnome-terminal-server, Windows Terminal, Terminal.app, ...) own every window from one pid, so the ancestor rule hid all of them when the runner was started from one, and the name rule hid them even when allowlisted. Terminal-only cases (Sencha Cmd) could capture no evidence.

## Behaviour and contract changes
- An allowlisted terminal is listed and capturable, including the window running the Test Runner. No protocol change.

## Notes for future changes
- If hiding the runner's own terminal window is wanted again, identify that window (e.g. a title token) instead of denying the terminal's pid or name.
