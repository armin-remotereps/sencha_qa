# Runner capture tools for the MCP gateway

Date: 2026-11-07
Plan: docs/plan/feat-mcp-runner-capture.md

## What changed
- `mcp_gateway`:
  - new tools `list_windows` and `capture_screenshot`, 14 tools in all;
  - services `list_windows.py`, `capture_screenshot.py`, `capture_lock.py` (Redis lock, round-trip cap), `capture_errors.py`, `capture_mapping.py`, and `stage_shot.py` (extracted from `add_screenshot`);
  - `image_preview.py` (JPEG preview returned as MCP image content);
  - `get_project` gains a `capture` block;
  - `McpStagedShot.capture_source` (migration 0003).
- `projects`:
  - controller seam `controller_capture_actions.py`, `runner_capture_readiness.py`, consumer forwarders and size-capped replies;
  - `Project.capture_allowed_processes` and `TestRunScreenshot.capture_source` (migration 0060; never rendered, excluded in admin);
  - Settings card "Screenshot capture" (`projects:settings_capture_apps`, `project_capture_apps_*`, `CaptureAppsError`);
  - `load_manual_image`;
  - runner-window shots are stored with `capture_source="runner"`.
- `controller_client` (CLIENT_VERSION 0.20.0, `WINDOW_CAPTURE_V1`):
  - capture core (`window_capture*.py`, `window_inventory.py`, `window_grab.py`, `capture_freshness.py`, `desktop_switch.py`, `screen_use_lock.py`, `capture_platform.py`, `window_model.py`);
  - platform backends for Windows, macOS, X11, Sway/Hyprland, and GNOME (Wayland and Xorg, through the bundled extension `gnome_capture_helper/`);
  - shared `shell_processes.py`, `session_bus.py`, `process_names.py`;
  - `diagnose --capture`;
  - `MobileDevice.foreground_app`;
  - setup and self-update install the GNOME extension.

## Why
MCP clients performing Manual runs had to capture screenshots themselves, and could not see the real Firefox or desktop app on the test machine. The connected Test Runner now captures them, limited to the project's allowed applications. The preview lets the client check a shot before attaching it to a step.

## Behaviour and contract changes
- **MCP tools:**
  - `list_windows()`: allowlisted windows on every desktop.
  - `capture_screenshot(pivot_id, claim_token, target, window_match?, region?, delay_ms?)` returns `shot_id`, size, `window_title`, `process`, `desktop`, `desktop_switched`, `desktop_restored` and a preview image.
  - Refusals start with `[code]`.
- **Controller protocol:** `list_windows` / `capture_window` → `window_list_result` / `window_capture_result`. Older runners get `[machine_outdated]` until Update Test Runner.
- **Settings:** new POST `/projects/<id>/settings/capture-apps/` (writable members). An empty list turns capture off.
- **Migrations:** `projects/0060_capture_allowlist`, `mcp_gateway/0003_staged_capture_source` (additive).
- **Limits:** captures share `MCP_STAGED_SHOTS_MAX` and the screenshot throttle with `add_screenshot`.
  - Timing: reply 45 s, call deadline 55 s, project lock 60 s.
  - Concurrency: at most 8 round trips per `mcp` process.
- **GNOME machines:** the helper extension needs one log-out and log-in after setup or update. Older helpers answer `[helper_missing]`.

## Notes for future changes
- **Not yet run on a real machine:** nothing has run on Windows, macOS, X11, GNOME, Sway, Hyprland or a phone, or with a real MCP client. Walk the plan's Verification list on staging. Check in particular:
  - mixed-DPI Windows;
  - the Win+Ctrl+Arrow switch;
  - macOS Space switching and the Dock window exclusion with Mission Control;
  - always-on Windows overlays (NVIDIA, Steam, Discord), which block `screen` and force window-only grabs unless allowlisted;
  - stock Ubuntu's desktop-icons window, which blocks `screen`;
  - the preview in Claude and Codex.
- **Fail closed:** anything the runner cannot classify counts as a foreign window. Keep it that way, and keep the deny list ahead of the allowlist.
- **No-trace:** `controller_client/` must never mention MCP. Never render `capture_source`.
- **Accepted residual risks** (plan Changelog):
  - wlroots layer-shell surfaces are not seen;
  - KWin compositor effects are not seen;
  - X11 trusts `_NET_WM_PID` (a client can claim to be a listed panel);
  - libX11 < 1.7 exits the runner on a lost display;
  - same-user unsandboxed processes can read the GNOME token.
- `LINUX_SHELL_SURFACE_PROCESSES` is mirrored in `extension.js`: change both together.
