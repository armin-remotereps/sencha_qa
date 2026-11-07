# feat: Runner capture tools for the MCP gateway

Status: Done
Created: 2026-11-07
Request: Add two tools to the MCP gateway.

- `capture_screenshot(pivot_id, claim_token, target: window|screen|region, window_match?: {process?, title_contains?}, region?: {x, y, w, h} relative to the matched window, delay_ms?: 0..5000)`. The tool asks the Test Runner that is already connected to the project's machine to capture the screen. The server stages the shot like `add_screenshot` and returns `{shot_id, width, height, bytes, window_title, process}` plus a downscaled preview as MCP image content. The client uses the preview to check the shot (right window, expected result) before it puts `shot_id` into `mark_step`.
- `list_windows()` returns process, title and size, so the client can pick a target instead of guessing.

Rules for the runner:

- **Process allowlist per project**, for example `firefox.exe` and `msedge.exe`. The runner refuses everything else, so a chat window or terminal never ends up in the evidence.
- **Logged-in interactive session.** Session 0 (a Windows service) produces black frames, and macOS needs Screen Recording permission. Both are detected and reported with a clear error.
- **Same limits as uploads.** Captures count against `MCP_STAGED_SHOTS_MAX` and use the same byte and pixel limits. The runner falls back to JPEG on its own.
- **Specific errors**, never an empty shot: runner offline, window not found, window minimised.
- **Neutral capture-source metadata** ("runner capture"), with nothing that says who triggered it.
- `add_screenshot` stays as it is.

Follow-up answers from the user:

- The app under test may be on another virtual desktop (desktop 2, 3, …), not only another monitor. The runner may switch to that desktop, capture once the window has repainted, and switch back.
- Linux must be supported, including the bundled GNOME Shell helper extension.

The Chrome-extension upload fallback is out of scope.

## Context

### MCP gateway
Sources: `docs/changed/2026-11-07-controller-mcp.md`, `docs/changed/2026-11-07-mcp-oauth.md`, `docs/plan/feat-controller-mcp.md`.

- **`mcp_gateway/mcp_app.py`:** 12 tools. Each handler packs a request DTO (`mcp_gateway/schemas.py`) and runs one service through `call_guard.guarded_call` in a worker thread (`database_sync_to_async(thread_sensitive=False)`). Handlers return a pydantic response with `structured_output=True`. `_ToolHandler = Callable[..., Awaitable[BaseModel]]`.
- **`mcp_gateway/services/add_screenshot.py`:** checks the claim (`own_claim.load_own_claim`, `refuse_paused`). Then it checks the base64 length, decodes strictly, and runs `check_manual_image` under `_DECODE_SLOTS` (2). Under the case row lock it enforces the caps `MCP_STAGED_SHOTS_MAX` (60) and `MCP_STAGED_BYTES_MAX` (100 MiB) and inserts an `McpStagedShot` with a server-minted `shot_key`.
- **`mcp_gateway/services/call_guard.py`:**
  - `CallKind` throttles: READ 120/min, MARK 60/min, SHOT `MANUAL_SHOT_RATE_PER_MINUTE`, and ANSWER shared with the runner.
  - Refusals that name the agent or the Test Runner are reworded neutrally (`_NEUTRAL_REFUSALS`).
  - One log line per call, never the arguments.
- **`mcp_gateway/limits.py`:** constants only (no settings; the gateway is always on).
- **`mcp_gateway/services/hand_in.py`:** calls `store_manual_screenshot(image_checked=True)` once per staged shot, inside one transaction.
- **No-trace rule:** the actor is `PersonStepActor.tester()`, the runner window never gets an MCP claim's token, and `manual_claim_source` is never rendered.

### Controller dispatch (server)

- **`projects/services._dispatch_controller_action`** (public alias `dispatch_controller_action`): sends to the project's controller group through the channel layer and waits on a fresh reply channel (`async_to_sync`).
  - `resend_on_reconnect=False`: `_current_owner_or_raise` raises `ControllerNotConnectedError` at once.
  - `runner_local`: a runner that stays silent raises `TestRunnerUnresponsiveError`.
- **Action module example:** `projects/controller_mobile_actions.py`.
- **`projects/controller_protocol.py`:** `ActionTypeRegistry._ACTION_TYPE_MAP` / `_PAYLOAD_KEYS` and the TypedDict events.
- **`projects/controller_consumer.py`:** `_forward_action`, and the reply handlers map (for example `ARTIFACT_SCAN_RESULT` → `controller_reply_tracker.send_artifact_scan_result`).
- **Capability and target helpers:** `projects/runner_capability_services.runner_supports`, `projects/runner_target.device_machine_state`.

### Controller (`controller_client/`)

- **`protocol.py`:** `CLIENT_VERSION` 0.19.0, `ClientCapability`, `advertised_capabilities`, `MANUAL_SHOT_MAX_BYTES` (7 MiB), `MANUAL_SHOT_MAX_PIXELS` (8K), `ManualShotMime`, `MAX_MESSAGE_SIZE` (10 MiB).
- **`client.py`:**
  - `_dispatch` answers TARGET_MISMATCH for messages the target cannot run.
  - `_window_hidden` calls `hidden_for_action` unless the message type is in `_WINDOW_SAFE_MESSAGE_TYPES`.
  - `_DESKTOP_ONLY_MESSAGE_TYPES`.
- **`manual_capture.py`:**
  - `ManualCapturer`: `hidden_for_capture` must confirm the person window is hidden, otherwise no capture; then `EXTRA_SETTLE_SECONDS`.
  - `encode_shot`: downscale to the pixel cap, PNG, then JPEG q85, then further downscale steps.
- **Desktop backends (`desktop.py`):**
  - Windows and macOS use `desktop_pyautogui.py`. pyautogui makes the process system DPI-aware. macOS capture goes through `screen_capture_macos.py` (`screencapture -x`, `MACOS_PERMISSION_HINT`).
  - Linux always uses `desktop_gnome.py`: Mutter's private RemoteDesktop and ScreenCast on GNOME Wayland (`gnome_remote_session.py`, `dbus_fast`), with frames grabbed by `pipewire_frame.py` (`gst-launch-1.0`). pyautogui is never imported on Linux.
- **Window enumeration today:**
  - `app_closer_windows.py`: ctypes `EnumWindows`. `_is_app_window` keeps windows that are visible, unowned, not tool windows and not cloaked; it reads titles and maps pid → exe through psutil. It also holds the terminal-host and OS-shell exe lists.
  - `app_closer_macos.py`: `CGWindowListCopyWindowInfo` through pyobjc Quartz and AppKit (pyautogui pulls these in).
  - `app_close_policy.current_runner_processes()`.
- **Linux D-Bus and X11 patterns already in the client:**
  - `manual_hotkeys_portal.py`: xdg-desktop-portal over `dbus_fast`, on a private asyncio loop in a daemon thread, writing a hidden `.desktop` file for the app id.
  - `manual_hotkeys_x11.py`: libX11 through ctypes with its own display connection and `prepare_x11_threads`.
- **Phone backend:** `mobile_platform.current_app(driver)`.
- **Dependencies:**
  - Pinned: Pillow, psutil, pyautogui, screeninfo, and `dbus-fast~=5.2.0` (Linux).
  - On Linux, pyautogui 0.9.54 and mouseinfo pull in **`python3-Xlib`** (0.15 from 2014, unmaintained). It installs the same `Xlib` import package as the maintained `python-xlib`, so installing both would overwrite each other's files.
  - There is no `mss`.

### Research (2026-11-07)

**MCP SDK `mcp` 2.3.0 (local `venv`):**
- `FuncMetadata.convert_result` passes a returned `CallToolResult` through unchanged and validates its `structured_content` when the return annotation is `Annotated[CallToolResult, ResponseModel]`.
- `mcp_types.ImageContent(type="image", data=<base64>, mime_type=...)`.
- A tool that builds its own `CallToolResult` must add the JSON `TextContent` itself.

**Codex** shows MCP tool-result images to the model since openai/codex PR #5600 (Oct 2025).

**Windows:**
- `EnumWindows` returns windows on every virtual desktop. Windows on inactive desktops are DWM-cloaked with `DWM_CLOAKED_SHELL` (2). Suspended UWP windows are `DWM_CLOAKED_APP` (1).
- `IVirtualDesktopManager` is documented: `IsWindowOnCurrentVirtualDesktop`, `GetWindowDesktopId`. The desktop order and names exist only in undocumented places: the internal COM interfaces (they change between builds) or the registry keys `HKCU\…\Explorer\VirtualDesktops\VirtualDesktopIDs` and `Desktops\{GUID}\Name`.
- `PrintWindow(PW_RENDERFULLCONTENT)` is the only GDI path that reaches DirectComposition content (Chrome, Edge, Electron). A TRUE return can still come with an unpainted frame, so the pixels themselves have to be checked.
- Chromium and Firefox stop painting windows they consider occluded or cloaked. A capture of a window on another desktop can therefore show stale content, which no check can detect. No source documents fresh frames from `PrintWindow` or Windows.Graphics.Capture for a window on another desktop.

**macOS:**
- `CGWindowListCopyWindowInfo` without `OnScreenOnly` lists windows on other Spaces, with `kCGWindowIsOnscreen` false. It does not say which Space or whether the window is minimised. Space ids need private CGS calls. Minimised state is available through Accessibility (`AXMinimized`), and the runner already needs that permission for input.
- `CGWindowListCreateImage` is obsoleted in the macOS 15 SDK.
- ScreenCaptureKit lists off-Space windows, but apps throttle drawing for occluded windows, so frames can be stale.

**X11:**
- EWMH gives `_NET_CLIENT_LIST_STACKING`, `_NET_WM_DESKTOP` (`0xFFFFFFFF` means sticky), `_NET_CURRENT_DESKTOP`, `_NET_NUMBER_OF_DESKTOPS`, `_NET_DESKTOP_NAMES`, `_NET_WM_PID`, `_NET_WM_NAME`, `_NET_WM_STATE_HIDDEN` (minimised), `_NET_ACTIVE_WINDOW`, `_NET_FRAME_EXTENTS`, and `_NET_WM_CM_S<n>` (a compositing manager is running).
- Window managers unmap windows on other workspaces. `XGetImage` on an unmapped window fails, and `XCompositeNameWindowPixmap` works only on mapped windows.

**Wayland:**
- There is no standard window list.
- GNOME 41+ restricts `org.gnome.Shell.Introspect.GetWindows` and `org.gnome.Shell.Screenshot` to portal and system callers (or unsafe mode). Inside the Shell process, an extension can list `global.get_window_actors()` and call `Shell.Screenshot.screenshot_area()`. The QuickSnap extension does this on GNOME 46.
- KDE `org.kde.KWin.ScreenShot2` needs `X-KDE-DBUS-Restricted-Interfaces` in a `.desktop` file whose `Exec` matches the caller's resolved executable. Reports say the file must be in a root-owned directory. For the runner the executable is the Python interpreter.
- wlroots compositors expose their own IPC: `swaymsg -t get_tree` and `hyprctl -j clients` give pid, app id, title, rect, workspace and visibility. `grim -g` captures a screen area through wlr-screencopy.
- xdg-desktop-portal:
  - `Screenshot` gives a full screen only, and GNOME asks for permission once per app id.
  - `ScreenCast` can persist a `restore_token` (`persist_mode=2`, version 4+), but a person picks the source in a dialog, and the stream carries no window identity.
  - With either portal, the allowlist cannot be enforced.

## Goals
- Two MCP tools, `list_windows` and `capture_screenshot`. Both are scoped to the bearer's project and go through the connected Test Runner.
- A per-project process allowlist configured on the web. The server sends it with every request; the runner enforces it against what the OS reports; the server checks the reported process again before staging.
- Window enumeration across every virtual desktop, with each window's desktop (current or not, and its number and name where the OS exposes them). Window, region and screen capture of windows on the current desktop on:
  - Windows (primary);
  - macOS;
  - Linux X11 (any EWMH window manager);
  - Linux Wayland on Sway and Hyprland;
  - Linux Wayland on GNOME, through a small bundled GNOME Shell helper extension (Q12).
- A window on another virtual desktop is captured by switching to its desktop, waiting until the window has repainted and settled, capturing, and switching back to the desktop that was active before. The switch back runs on every path. Supported on Windows (Win+Ctrl+Arrow, checked with the documented `IVirtualDesktopManager`), macOS (activating the window's app), X11 (`_NET_CURRENT_DESKTOP`), Sway and Hyprland (IPC) and GNOME (the helper extension).
- Every unsupported case gets a specific error code: KDE Wayland, other compositors, a missing helper, a desktop switch that cannot be done or verified, a window that does not settle after the switch, a minimised or obscured window that cannot be captured faithfully, a locked or non-interactive session, missing permission, a blank frame.
- Captured shots are staged exactly like `add_screenshot` shots (same caps, checks and hand-in path), with a neutral `capture_source`.
- A downscaled JPEG preview returned as MCP image content alongside the structured result.
- Nothing about the trigger or the claim reaches the runner, its window, its logs, or any surface a user sees.

## Non-goals
- Changing how `add_screenshot`, hand-in, mark or claim work.
- Any change to what a person at the machine sees beyond the person window hiding briefly and, for a window on another desktop, the desktop switch and the switch back:
  - no bringing windows to the front, except what the platform's switch does by itself (macOS activates the target app, then the previous app on the way back);
  - no restoring minimised windows;
  - no input other than the Windows desktop-switch shortcut.
- Capturing a window on another desktop in place, without switching. Frames there can be stale.
- Using the undocumented `IVirtualDesktopManagerInternal` COM interface (see Alternatives).
- An idle gate or per-project toggle for desktop switching. The user chose "Allow switching" outright. The wire field `switch_desktop` keeps a toggle possible later.
- KDE Plasma on Wayland and other Wayland compositors (refused with `[compositor_unsupported]`; KDE on X11 works).
- Changing the agent's desktop backend (Linux agent runs stay GNOME Wayland only). The new Linux support covers the capture tools only.
- Browser-extension upload, video, scrolling or full-page capture.
- Rendering the capture source or window metadata anywhere.
- A runner-local allowlist (`.env`).

## Design

### Decisions from the user's answers
- **Q2–Q9:** accepted as recommended. Details are in the Changelog.
- **Q1, with virtual desktops:**
  - `list_windows` lists allowlisted windows on every desktop, with `desktop: {current: bool, index: int|null, name: str|null}`, plus `current_desktop` and `desktop_count` where known.
  - **Q11 (resolved: "Allow switching").** When the selected window is on another desktop, `capture_screenshot` switches to that desktop, waits until the window is shown and has repainted and settled, captures, and switches back to the desktop that was active before. See "Desktop switching" under Runner. Where a platform cannot switch or cannot confirm the switch, the capture is refused with `[desktop_switch_failed]` and a specific detail, and the desktop is left as it was. `[window_on_other_desktop]` remains only for a server request with `switch_desktop=false`; the server always sends true for now.
  - `target: "screen"` means the monitor holding the matched (or topmost allowlisted) window, on its desktop, after switching if needed. It is refused with `[screen_not_clean]` when any visible window of a non-allowlisted or denied process overlaps that monitor on the desktop being captured. This is evaluated after the switch, by enumerating again. The runner then switches back.
  - Off-desktop windows are never captured in place, because those frames can be stale (see Research). The capture happens only after the switch, once the freshness check passes.
- **Q10, Linux supported:**
  - **X11:** libX11 and libXcomposite through ctypes. This is the pattern `manual_hotkeys_x11.py` already uses, and it adds no pip dependency (see Alternatives on why not `python-xlib` or `mss`).
  - **GNOME Wayland:** the bundled helper extension (Q12, resolved: yes).
  - **Sway and Hyprland:** their IPC CLIs plus `grim`.
  - **KDE Wayland and other compositors:** refused.
  - **Portals:** not used for capture, because they give no window identity to check against the allowlist.

### Data model (additive migrations)
- **`projects` migration `0060_capture_allowlist`:**
  - `Project.capture_allowed_processes`: JSONField(list[str], default list, blank). Normalized process names (`normalize_process_name`), at most `CAPTURE_ALLOWED_PROCESSES_MAX` (20). An empty list means runner capture is off (Q4).
  - `TestRunScreenshot.capture_source`: CharField(16, blank, default ""), with choices `ScreenshotCaptureSource`: "" (unknown or other) and `"runner"` (captured on the test machine by the Test Runner).
    - Both the runner window's tester shots and MCP runner captures write `"runner"`, so the two cannot be told apart (Q6).
    - Code comment: "Internal. Never render a value that is not `runner`."
    - `add_screenshot` shots keep "".
- **`mcp_gateway` migration `0003_staged_capture_source`:** `McpStagedShot.capture_source`, CharField(16, blank, default ""). `capture_screenshot` writes `"runner"`. `hand_in` passes it to `store_manual_screenshot(capture_source=)`.

No data migration. Existing rows keep "".

### Wire protocol (`controller_client/protocol.py`; CLIENT_VERSION 0.19.0 → 0.20.0)
- **Capability `ClientCapability.WINDOW_CAPTURE_V1 = "window_capture_v1"`:**
  - Optional, so it is not added to `REQUIRED_CLIENT_CAPABILITIES`.
  - Every 0.20.0 build advertises it on every OS and target, including runners with the person window off and phone runners.
  - So a missing capability always means the runner is outdated. A platform that cannot capture says so in its result status.
- **Message types:**
  - server → runner: `list_windows`, `capture_window`;
  - runner → server: `window_list_result`, `window_capture_result`. Same `request_id`, with the result object under `result`, as `artifact_scan_result` does.
- **Constants:**
  - `CAPTURE_ALLOWED_PROCESSES_MAX` = 20
  - `CAPTURE_PROCESS_NAME_MAX_CHARS` = 64
  - `CAPTURE_TITLE_MATCH_MAX_CHARS` = 200
  - `CAPTURE_TITLE_MAX_CHARS` = 256
  - `CAPTURE_WINDOW_ID_MAX_CHARS` = 64
  - `CAPTURE_DESKTOP_NAME_MAX_CHARS` = 64
  - `WINDOW_LIST_MAX` = 50
  - `CAPTURE_DELAY_MAX_MS` = 5000
  - `CAPTURE_REGION_MAX_PIXELS_PER_AXIS` = 16384
  - `WINDOW_CAPTURE_RESULT_MAX_BYTES` = the padded base64 size of `MANUAL_SHOT_MAX_BYTES` plus 8 KiB, asserted ≤ `MAX_MESSAGE_SIZE`
- **`normalize_process_name(raw) -> str | None`:**
  - Takes the stripped basename, lower-cased, without a trailing `.exe` or `.app`.
  - Refuses empty names, names that are too long, and names with characters outside `[a-z0-9 ._+-]`.
  - The server uses it in the Settings form and the runner when matching, so `firefox.exe`, `Firefox` and `firefox` all match `firefox`.
  - macOS matches the owner name, for example "Microsoft Edge". Linux matches the process name of `_NET_WM_PID` or the compositor-reported pid, for example `firefox`, `msedge`.
- **Enums:**
  - `CaptureTarget`: `window`, `screen`, `region`.
  - `CaptureStatus`:
    - `ok`
    - `not_allowed`
    - `window_not_found`
    - `window_minimized`
    - `window_on_other_desktop`: only when `switch_desktop` is false.
    - `desktop_switch_failed`: the switch could not be done or verified. Examples: an elevated window in front on Windows (UIPI); desktop order unreadable; macOS Accessibility missing, or "switch to a Space with open windows" turned off; IPC or helper refused.
    - `window_not_settled`: after the switch the window kept changing past the freshness budget, e.g. video or a spinner. Retry with a larger `delay_ms`.
    - `window_obscured`: the window is covered and this platform has no faithful window-only capture.
    - `region_out_of_bounds`
    - `screen_not_clean`
    - `not_interactive`
    - `permission_missing`
    - `capture_blank`
    - `hide_failed`
    - `too_large`
    - `compositor_unsupported`
    - `helper_missing`: the GNOME helper extension is not installed, enabled or current, or a wlroots tool is missing.
    - `unsupported`: on a phone, `window` is unsupported.
    - `invalid`
- **Payload dataclasses**, each with `parse_*` / `serialize_*` functions used on both sides:
  - `ListWindowsPayload(allowed)`.
  - `CaptureWindowPayload(target, process, title_contains, window_id, region: CaptureRegion | None, delay_ms, allowed, max_bytes, max_pixels, switch_desktop: bool)`. The server sends `switch_desktop=True`. The field leaves room for a later per-project toggle without another protocol change.
  - `WindowDesktopPayload(current: bool, index: int | None, name: str | None)`. `index` is 1-based.
  - `WindowInfoPayload(window_id, process, title, width, height, minimized, focused, desktop)`.
  - `WindowListResultPayload(status, detail, windows, others_hidden, current_desktop: int | None, desktop_count: int | None)`.
  - `WindowCaptureResultPayload(status, detail, mime, data, sha256, width, height, window_title, process, other_matches, desktop, desktop_switched: bool, desktop_restored: bool | None)`. `desktop_restored` is null when no switch happened, and false when the switch back failed; this can come with any status, including `ok`. The parser checks the base64, the sha256 and the length against `max_bytes`, the same way `parse_manual_screenshot_upload` does.
- **No identifying data in requests.** Requests carry only the target description, the allowlist and the limits: no pivot, claim, run, user, client or MCP information.

### Runner (controller_client)
One responsibility per module. Everything is strictly typed under the controller's mypy config, and platform imports are guarded by `sys.platform` or the session type, as `app_closer_*` and `manual_hotkeys_*` do. No new pip dependency.

#### Shared modules
- **`window_model.py`:** the plain dataclasses the platform modules return:
  - `DesktopWindow(window_id, pid, process, title, rect, minimized, focused, z, desktop: WindowDesktop, app_cloaked)`;
  - `DesktopInfo(current, count, names)`.
- **`window_inventory.py`:** `list_desktop_windows() -> tuple[DesktopInfo, list[DesktopWindow]]`. It picks the platform module:
  - Windows;
  - macOS;
  - Linux by `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY` / `DISPLAY` and the compositor markers (`SWAYSOCK`, `HYPRLAND_INSTANCE_SIGNATURE`, `XDG_CURRENT_DESKTOP`). KDE Wayland and unknown compositors raise `CaptureUnavailableError(compositor_unsupported)`.
- **`capture_session.py`:** `interactive_session_problem() -> (CaptureStatus, detail) | None`, checked before every list and every capture. It delegates to the platform modules below.
- **`window_capture_policy.py`** (pure):
  - **Allowlist and deny list.** The allowlist matches normalized names. A hard deny list applies even when a name is allowlisted:
    - the runner's own processes (`current_runner_processes()`: the runner, its ancestors, the person window's pid, terminal hosts);
    - the OS shell;
    - known terminal exes (the lists in `app_closer_*`, plus common Linux terminals such as gnome-terminal-server, konsole, xterm, alacritty, kitty, foot, wezterm and tilix).
  - **Window selection:**
    - an explicit `window_id` wins;
    - otherwise filter by process plus `title_contains` (case-insensitive) among allowlisted, non-denied windows;
    - prefer the current desktop, then take the topmost non-minimised match;
    - `other_matches` counts the rest (Q7).
  - **Selection errors:**
    - no allowlisted match, but a non-allowlisted window matched: `not_allowed`;
    - no match at all: `window_not_found`;
    - the only matches are minimised: `window_minimized`;
    - the only matches are on another desktop: that window is selected and flagged `needs_switch` (or `window_on_other_desktop` when `switch_desktop` is false).
  - **Region:** must lie inside the window shot (pixels of a `window` capture), else `region_out_of_bounds` with the window size.
  - **Screen rule:** as in Decisions, Q1.
- **`window_grab.py`:** turns a chosen window or monitor into an image, using "what a person sees" first:
  1. **Unobscured window:** grab the screen rect. This includes the app's own menus, dropdowns and tooltips. A window counts as unobscured when sampled points of its rect resolve to the target or to windows of the same pid.
  2. **Obscured window:** use the platform's window-only image where one exists and is faithful. This never includes other windows, but it misses the app's separate popups. Otherwise refuse with `window_obscured`.
  3. **Blank check:** after grabbing, an all-black or uniform image (Pillow `getextrema`) returns `capture_blank` with a hint and is never sent.
- **`screen_use_lock.py`:** one process-wide `asyncio.Lock`, shared by `ManualCapturer` (window-tester shots and hotkeys) and `WindowCaptureService`, so two screen captures and desktop switches never interleave. Server messages are already handled one at a time: `_receive_messages` awaits each `_dispatch` before reading the next message. So no agent action or other server command can run while a capture switches desktops. Only window-originated captures run in separate tasks, and those take this lock.
- **`window_capture.py`:** `WindowCaptureService` (async, holds the screen-use lock).
  - **`list_windows`:** session check, then enumerate. Returns only allowlisted, non-denied windows on every desktop (Q2), with `others_hidden` as a count. Titles are cut to `CAPTURE_TITLE_MAX_CHARS`.
  - **`capture`:**
    1. session check;
    2. enumerate and select;
    3. refuse a minimised window;
    4. hide the person window with `hidden_for_capture()` when there is one (an unconfirmed hide gives `hide_failed`). The hide happens on the original desktop, before any switch.
    5. run the screen job in one worker thread (`asyncio.to_thread`):
       1. if `needs_switch`: `DesktopSwitcher.switch_to(window)` (see Desktop switching);
       2. wait `EXTRA_SETTLE_SECONDS`, then `delay_ms`;
       3. enumerate again and select the same `window_id`; if it is gone or minimised by now, return the matching status;
       4. apply the screen rule over the desktop now shown;
       5. grab; after a switch, the freshness check below; then the blank check;
       6. `finally`: if a switch happened, `DesktopSwitcher.restore()`.

       The switch and the switch back sit in one thread function with `try/finally`. Cancelling the awaiting task (socket closed, server timeout) never interrupts the thread, so the switch back always runs.
    6. crop the region;
    7. show the person window again;
    8. run `encode_shot(image, max_bytes=, max_pixels=)` in a thread;
    9. reply with `desktop_switched` and `desktop_restored`.
  - **Phone target (Q5):**
    - `list_windows` returns one entry: `window_id` "device", process = the foreground app id from `current_app`, title = the device name, size = the screen, desktop current.
    - Captures accept only `screen` and `region` (of the screen). `window` returns `unsupported`.
    - The foreground app id must be allowlisted.
    - No hide.
  - **Neutral logs:** status, process, size, desktop index and duration only. No titles.
- **`manual_capture.py`:** `encode_shot` gains optional `max_bytes` / `max_pixels` arguments, defaulting to the protocol constants. Running out of downscale steps returns `too_large`.
- **`client.py`:**
  - handlers for `LIST_WINDOWS` and `CAPTURE_WINDOW`;
  - both added to `_WINDOW_SAFE_MESSAGE_TYPES` (capture hides the window itself, with the confirmed variant);
  - neither is desktop-only.
- **`diagnose.py`:** a new `--capture` flag. It prints the session check, the detected platform path (Windows, macOS, X11, GNOME helper, Sway, Hyprland or unsupported), all windows with their desktop, size and minimised state, and makes one test capture of the focused window into a temp file. It never uploads.

#### Desktop switching (Q11)
- **`desktop_switch.py`:** the `DesktopSwitcher` protocol and the shared rules. Platform implementations live in the per-platform modules below. All of them use the same sequence.
  1. **Record:** the current desktop (an identity that can be verified), the foreground window or app, and one "anchor" window that is visible on the current desktop. The anchor is used to verify the switch back, so the empty-desktop case gets special handling per platform.
  2. **Switch** with the platform mechanism, under a budget of `DESKTOP_SWITCH_BUDGET_SECONDS` = 6.
  3. **Verify:** poll every 100 ms until the target window is reported on the current desktop and drawable. On Windows: `IsWindowOnCurrentVirtualDesktop` true and not DWM-cloaked. On macOS: on screen. On X11: `_NET_CURRENT_DESKTOP` equals its desktop and the window is mapped. On Sway and Hyprland: the workspace is visible. On GNOME: the helper reports the workspace active. A timeout gives `desktop_switch_failed`; the switch back still runs.
  4. **Freshness** (`capture_freshness.py`, pure Pillow):
     1. wait at least `DESKTOP_SWITCH_SETTLE_MS` = 500 after verification, then `delay_ms`;
     2. grab the target, either its visible rect or the window-only image;
     3. grab again every 250 ms until two consecutive grabs match. Match means the changed pixels (`ImageChops.difference` against a small threshold) cover at most `FRESHNESS_MAX_CHANGED_FRACTION` = 0.5% of the image, which tolerates a blinking caret or a small spinner;
     4. use the last grab;
     5. if they do not match within `FRESHNESS_BUDGET_SECONDS` = 3, return `window_not_settled`.

     The first grab after the switch is never used alone. A browser resumes painting once the window is shown, and the stability check waits until that repaint is over.
  5. **Switch back** to the recorded desktop with the same mechanism, verified against the recorded identity or anchor. Failure, or no verification within the budget, sets `desktop_restored=false` and logs a warning; the capture's own status is kept. Focus: no explicit `SetForegroundWindow`, raise or activate beyond what the switch back does, except on macOS, where the switch back *is* activating the previously front app (recorded window raised through AX).
  6. **Allowlist after the switch:**
     - the desktop now shown is enumerated again;
     - window and region targets use the visible-rect grab only if the target is unobscured on that desktop, otherwise the window-only image (or `window_obscured`);
     - `screen` is refused with `screen_not_clean` when foreign windows are shown there.

     Non-allowlisted windows on the switched-to desktop therefore never enter a shot.
- **Windows** (`win32_desktop_switch.py`). The mechanism is the system shortcut **Win+Ctrl+Left / Right**, pressed |target − current| times through the existing pyautogui key path (`press_combination`, modifiers released afterwards).
  - **Before:** `input_guard.ensure_input_not_blocked()`. An elevated foreground window (UIPI would drop the keys silently) gives `desktop_switch_failed`, with the detail "an administrator window is in front; run the Test Runner as Administrator".
  - **Indexes:** current and target come from the registry order (`VirtualDesktopIDs`) and the documented `GetWindowDesktopId`. Unreadable registry gives `desktop_switch_failed` ("desktop order unavailable").
  - **Verification:** after every step, `IsWindowOnCurrentVirtualDesktop` for the target, and for the anchor on the way back. An empty original desktop has no anchor; then the current desktop's GUID is checked through the registry value `CurrentVirtualDesktop`.
  - **Not used:** the undocumented `IVirtualDesktopManagerInternal` (see Alternatives).
- **macOS** (`macos_desktop_switch.py`). The mechanism is AX raising the target window (`kAXRaiseAction`), then `NSRunningApplication.activateWithOptions` of its app. macOS then switches to the window's Space (Dock setting `workspaces-auto-swoosh`, on by default).
  - **Preconditions:** Accessibility trusted (`AXIsProcessTrusted`) and the auto-switch setting not off (`defaults read com.apple.dock workspaces-auto-swoosh`). Otherwise `desktop_switch_failed` with the detail.
  - **Switch back:** activate the recorded front app and raise its recorded focused window (AX), verified by the anchor being on screen again. An original Space with no windows cannot be addressed this way; macOS has no public Space API. The switch is then refused up front with `desktop_switch_failed` ("the current Space has no window to return to"), so the desktop is never left changed.
  - **Not used:** Ctrl+Arrow / Ctrl+Number shortcuts (Space index unknown; Ctrl+Number is off by default).
- **X11** (`x11_desktop_switch.py`). A `_NET_CURRENT_DESKTOP` ClientMessage to the root window (SubstructureRedirect | SubstructureNotify), sent for the target index and for the recorded index on the way back. Verified by reading `_NET_CURRENT_DESKTOP`. A window manager that ignores it within the budget gives `desktop_switch_failed`.
- **Sway / Hyprland** (inside `wayland_wlroots.py`).
  - Sway: record the focused workspace and the visible workspace of each output (`swaymsg -t get_workspaces`). Switch with `swaymsg workspace --no-auto-back-and-forth <name>`. Switch back by restoring each output's visible workspace, then the focused one.
  - Hyprland: record the active workspace per monitor and the focused monitor (`hyprctl -j monitors`). Switch with `hyprctl dispatch workspace <id>`. Switch back by restoring each monitor's workspace, then `hyprctl dispatch focusmonitor <name>`.
  - Verified through the same queries.
- **GNOME** (helper extension). New D-Bus methods `ActiveWorkspace() -> index` and `ActivateWorkspace(index)`, using `Meta.Workspace.activate(timestamp)` (not `activate_with_focus`). Verified with `ActiveWorkspace`. An older helper without them gives `helper_missing`.
- **KDE Wayland** remains `compositor_unsupported`, so it never reaches the switch. KDE on X11 uses the X11 path.
- **Runner shutdown mid-switch.** SIGTERM or Ctrl+C during the screen job waits for the thread's `finally` (bounded by the budgets) before exiting. A hard kill can leave the switched desktop active (see Risks).

#### Windows
- **`win32_windows.py`:** the read-only enumeration primitives, moved verbatim from `app_closer_windows.py`, which then imports them with no change in behaviour. The new enumeration:
  - keeps windows cloaked with `DWM_CLOAKED_SHELL` (other desktop), but `app_closer` keeps excluding every cloaked window, as today;
  - drops `DWM_CLOAKED_APP` (suspended UWP);
  - adds `IsIconic`, `GetForegroundWindow`, `GetWindowRect` and the z-order.
- **`win32_virtual_desktops.py`:**
  - `IVirtualDesktopManager` (documented; `CoCreateInstance` and vtable calls through ctypes, COM initialised per worker thread): `IsWindowOnCurrentVirtualDesktop` and `GetWindowDesktopId`.
  - Index and name: read-only from `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops` (`VirtualDesktopIDs` order, `Desktops\{GUID}\Name`). If unreadable, they are null.
  - The current desktop's GUID is the desktop id of any uncloaked window on the current desktop.
- **`capture_session_windows.py`:** returns `not_interactive` when:
  - `ProcessIdToSessionId` is 0: "running as a Windows service in session 0; start it from the logged-in desktop";
  - `OpenInputDesktop` fails or the input desktop is not `Default`: "the desktop is locked or a UAC or secure desktop is shown";
  - the WTS connection state is `WTSDisconnected`: "the remote desktop session is disconnected; nothing is drawn".
- **`window_grab_windows.py`:**
  - The visible rect via Pillow `ImageGrab.grab(bbox, all_screens=True)`, in `GetWindowRect` space (see Risks: DPI). The unobscured check uses `WindowFromPoint` plus `GetAncestor(GA_ROOT)`.
  - The window-only image via `PrintWindow(PW_RENDERFULLCONTENT)` and GDI through ctypes, then the pixel check.

#### macOS
- **`window_inventory_macos.py`:**
  - `CGWindowListCopyWindowInfo(kCGWindowListOptionAll | excludeDesktopElements)` at layer 0. Owner name is the process, `kCGWindowName` the title, plus bounds and pid.
  - A window that is on screen is on the current desktop.
  - A window that is not on screen is checked through Accessibility (`AXUIElementCreateApplication(pid)` windows, matched by title and bounds). `AXMinimized` true means `minimized`; otherwise the window is on another Space (`desktop.current = false`, index and name null; Space numbers need private APIs).
  - Without Accessibility, a window that is not on screen cannot be classified, and switching needs Accessibility anyway. A capture of it gives `desktop_switch_failed` with the detail "minimised or on another Space; grant Accessibility to switch".
- **`capture_session_macos.py`:**
  - `CGPreflightScreenCaptureAccess()` false: `permission_missing` (reuses `MACOS_PERMISSION_HINT`).
  - Locked screen (`CGSessionCopyCurrentDictionary`): `not_interactive`.
- **`window_grab_macos.py`:**
  - the visible rect via `screencapture -x -R`;
  - the window-only image via `screencapture -x -o -l <CGWindowID>`, used only for windows that are on screen.

#### Linux X11 (any EWMH window manager: GNOME/KDE/Xfce/Cinnamon on Xorg)
- **`x11_lib.py`:** ctypes bindings for libX11 and libXcomposite (`ctypes.util.find_library`). Each call runs on its own `Display` connection, opened and closed in the worker thread. `manual_hotkeys_x11.prepare_x11_threads` runs once at startup.
  - If `libXcomposite` is missing, there is no window-only fallback, so obscured windows get `window_obscured`.
  - If the window manager has no EWMH support (no `_NET_SUPPORTING_WM_CHECK`): `compositor_unsupported`.
- **`window_inventory_x11.py`:**
  - windows from `_NET_CLIENT_LIST_STACKING`;
  - pid from `_NET_WM_PID`, mapped to a process name through psutil;
  - title from `_NET_WM_NAME` (falling back to `WM_NAME`);
  - geometry from `XGetWindowAttributes` plus `XTranslateCoordinates` and `_NET_FRAME_EXTENTS`;
  - minimised from `_NET_WM_STATE_HIDDEN`;
  - focused from `_NET_ACTIVE_WINDOW`;
  - desktop from `_NET_WM_DESKTOP` versus `_NET_CURRENT_DESKTOP` (sticky counts as current), with `_NET_NUMBER_OF_DESKTOPS` and `_NET_DESKTOP_NAMES`.
- **`window_grab_x11.py`:**
  - the visible rect via `XGetImage` on the root window (ZPixmap, then Pillow `frombuffer` BGRX);
  - the unobscured check from the stacking order and rects;
  - the window-only image only while a compositing manager runs (`_NET_WM_CM_S<screen>` has an owner): `XCompositeNameWindowPixmap` on the frame window, then `XGetImage`. Otherwise `window_obscured`.

#### Linux Wayland: Sway and Hyprland
- **`wayland_wlroots.py`:**
  - Sway: `swaymsg -t get_tree -r`. Gives pid, `app_id` or the XWayland class, name, rect, focused and visible. The workspace comes from the tree, and visible workspaces count as current. A window in the scratchpad counts as minimised.
  - Hyprland: `hyprctl -j clients`, `hyprctl -j monitors` and `hyprctl -j workspaces`. Gives pid, class, title, `at` and `size`, workspace id and name, `hidden` (minimised), and focus.
  - Capture: `grim -g "x,y wxh" -t png -` of the visible rect.
  - A window that is obscured (floating overlap computed from the tree) gets `window_obscured`; there is no faithful window-only path in v1.
  - A missing `swaymsg`, `hyprctl` or `grim` gets `helper_missing`, naming the package.
  - Commands run with fixed argv, no shell, and a timeout of 5 s.

#### Linux Wayland: GNOME (Q12)
- **`controller_client/gnome_capture_helper/`:** a GNOME Shell extension (`metadata.json`, `extension.js`; ESM, shell-version 45–49) with the neutral uuid `test-runner-capture@punk-hazard`. It owns the session-bus name `org.testrunner.Capture1` and exposes:
  - `Version`;
  - `ListWindows()`: per window from `global.get_window_actors()`, the stable id (`meta_window.get_id()`), pid, `wm_class`, title, frame rect, minimised, focus, workspace index and whether it is active, `get_n_workspaces()`, and the workspace names from GSettings;
  - `CaptureArea(x, y, w, h, path)`: `Shell.Screenshot.screenshot_area` after the next frame;
  - `CaptureWindow(id, path)`: the window actor's own content via `Meta.WindowActor.paint_to_content()` or `get_image()`, saved as PNG. It works even when the window is obscured.
  - `ActiveWorkspace()` and `ActivateWorkspace(index)` for desktop switching (`Meta.Workspace.activate`, no focus request).

  `path` must be inside a runner directory under `$XDG_RUNTIME_DIR` (mode 0700). The extension refuses any other path.
- **`gnome_shell_capture.py`:**
  - The runner side, over `dbus_fast`, on a private loop thread like `manual_hotkeys_portal.py`.
  - A missing name or an older `Version` returns `helper_missing`. The detail says: "enable the Test Runner capture extension (setup script installs it) and log out and back in once".
- **Setup:**
  - `controller_client/scripts/setup.sh` installs it with `gnome-extensions install --force` plus `gnome-extensions enable` when `XDG_CURRENT_DESKTOP` contains GNOME.
  - The self-update installer re-runs `install --force`. The running copy changes only after the next login, so the runner accepts the helper `Version` range it knows.
- The existing primary-monitor Mutter ScreenCast in `desktop_gnome` is not used for capture. Its frames lack window identity and cover the primary monitor only.

#### Linux session checks (`capture_session_linux.py`)
- Neither `DISPLAY` nor `WAYLAND_DISPLAY` set, or no session bus on Wayland: `not_interactive` ("the Test Runner is not running inside a logged-in desktop session").
- Locked screen, from logind `Session.LockedHint` (`org.freedesktop.login1`, session from `XDG_SESSION_ID` or `GetSessionByPID`), falling back to `org.freedesktop.ScreenSaver.GetActive`: `not_interactive`.
- KDE Wayland (`XDG_CURRENT_DESKTOP` KDE with `XDG_SESSION_TYPE` wayland) and unknown Wayland compositors: `compositor_unsupported`. The detail says: "log in to a Plasma (X11) session, GNOME, Sway or Hyprland".
- Blank frames: the shared blank check.

### Server: controller action seam (projects)
- **`projects/controller_protocol.py`:**
  - TypedDicts `ListWindowsActionEvent` and `CaptureWindowActionEvent`.
  - Registry entries: `controller.list_windows` → `LIST_WINDOWS` with key `allowed`; `controller.capture_window` → `CAPTURE_WINDOW` with keys `target`, `process`, `title_contains`, `window_id`, `region`, `delay_ms`, `allowed`, `max_bytes`, `max_pixels`.
- **`projects/controller_consumer.py`:**
  - forwarders `controller_list_windows` and `controller_capture_window`;
  - reply types `WINDOW_LIST_RESULT` and `WINDOW_CAPTURE_RESULT` route to `controller_reply_tracker.send_window_result`;
  - a raw result over `WINDOW_CAPTURE_RESULT_MAX_BYTES` (capture) or 256 KiB (list) is answered to the waiter as an error and not forwarded.
- **`projects/controller_reply_tracker.py`:** `send_window_result`.
- **`projects/controller_capture_actions.py`** (new):
  - `controller_list_windows(project_id, *, allowed, timeout) -> WindowList`;
  - `controller_capture_window(project_id, *, request: CaptureWindowRequest, timeout) -> WindowCapture`.
  - Both call `dispatch_controller_action(..., resend_on_reconnect=False, runner_local=True)` and parse with the protocol parsers. An invalid reply raises `ControllerActionError`. They return plain frozen dataclasses, including `desktop`.
- **`projects/runner_capture_readiness.py`** (new leaf): `capture_readiness(project_id) -> CaptureReadiness(connected, supported, phone, device_ready, allowed)`, in one `values_list` query.
- **`projects/manual_screenshot_services.store_manual_screenshot`:** new keyword `capture_source: str = ""`. The runner upload path (`runner_manual_services`) passes `"runner"`.
- **`projects/project_capture_apps_services.py`** (new): `set_capture_allowed_processes(*, project, names) -> list[str]`. It normalizes, removes duplicates, enforces the cap and saves with `update_fields`.

### Server: MCP services and tools (mcp_gateway)
- **`limits.py`:**
  - `MCP_MACHINE_RATE_PER_MINUTE` = 30
  - `MCP_CAPTURE_REPLY_TIMEOUT_SECONDS` = 45 (review round 1; was 50), with a 55 s call deadline for decoding (`MCP_CAPTURE_DEADLINE_SECONDS`) and at most `MCP_MACHINE_ROUND_TRIPS_MAX` = 8 runner round trips per process. It covers a worst case of: hide ≤ 2 s, switch ≤ 6 s, settle 0.5 s, `delay_ms` ≤ 5 s, freshness ≤ 3 s, grab and encode ≤ 6 s, switch back ≤ 6 s, plus the upload. That is under Codex's default 60 s tool timeout.
  - `MCP_LIST_WINDOWS_TIMEOUT_SECONDS` = 10
  - `MCP_CAPTURE_LOCK_SECONDS` = 60
  - `MCP_CAPTURE_PREVIEW_MAX_EDGE` = 1280
  - `MCP_CAPTURE_PREVIEW_MAX_BYTES` = 400 KiB
  - `MCP_CAPTURE_PREVIEW_JPEG_QUALITY` = 70
- **`services/call_guard.py`:** new `CallKind.MACHINE` (`mcp:machine:<project>`, 30/min) for `list_windows`. `capture_screenshot` uses `CallKind.SHOT`, so it shares the budget with `add_screenshot`.
- **`services/stage_shot.py`** (extracted from `add_screenshot.py`): `_DECODE_SLOTS`, `check_image`, and `stage_shot(call, *, pivot_id, claim_token, mime, data, capture_source) -> StagedShot` (lock, caps, insert). `add_screenshot` keeps its contract.
- **`image_preview.py`** (pure Pillow, no database): `make_preview(data, mime) -> bytes`. Long edge 1280, JPEG q70, stepping down until the result is under 400 KiB. It runs under `_DECODE_SLOTS` and is never stored.
- **`services/capture_errors.py`:** one table from `CaptureStatus`, readiness failures and controller exceptions to `McpToolError` texts. Each text starts with a stable code. The texts say "test machine" and never "Test Runner", "agent" or "AI".

  | Code | Text / detail |
  |---|---|
  | `not_set_up` | No allowed applications are configured; use `add_screenshot`. |
  | `machine_offline` | |
  | `machine_outdated` | Ask a project member to update the test machine's software from the Environment page. |
  | `device_offline` | |
  | `machine_busy` | |
  | `machine_unresponsive` | |
  | `not_allowed` | Lists the allowed applications. |
  | `window_not_found` | |
  | `window_minimized` | |
  | `window_on_other_desktop` | Only when switching is disabled: "The window is on desktop 3 (Testing)." |
  | `desktop_switch_failed` | "The test machine could not switch to desktop 3 (Testing): <detail>." Adds "and could not switch back" when `desktop_restored` is false. |
  | `window_not_settled` | "The window kept changing after the desktop switch; call again with a larger delay_ms." |
  | `window_obscured` | "Another window covers it; bring it to the front and call again." |
  | `region_out_of_bounds` | |
  | `screen_not_clean` | Names the visible processes. |
  | `not_interactive`, `permission_missing`, `compositor_unsupported`, `helper_missing` | The runner's fixed detail. |
  | `capture_blank`, `too_large`, `unsupported`, `capture_failed` | |
- **`services/list_windows.py`:**
  1. `capture_readiness` and its refusals;
  2. `controller_list_windows`;
  3. map the status;
  4. return `ListWindowsResponse(windows[] {window_id, process, title (fenced), width, height, minimized, focused, desktop {current, index, name (fenced)}}, current_desktop, desktop_count, others_hidden, allowed_processes)`.

  No claim is needed (Q9).
- **`services/capture_screenshot.py`:**
  1. `load_own_claim(lock=False)` plus `refuse_paused`;
  2. staged-shot cap pre-check;
  3. readiness;
  4. per-project lock `cache.add("mcp:capture:<project>", …, MCP_CAPTURE_LOCK_SECONDS)`, released in `finally`;
  5. `controller_capture_window`, with no transaction held;
  6. map the status;
  7. check the reported process against `readiness.allowed` again (refuse and log the process only);
  8. `stage_shot(capture_source="runner")`, which checks the claim again under the row lock;
  9. `make_preview`;
  10. return `CaptureOutcome(response=CaptureScreenshotResponse(shot_id, width, height, bytes, mime, window_title (fenced), process, target, other_matches, desktop, desktop_switched, desktop_restored), preview_jpeg)`.

  A successful capture with `desktop_restored=false` is still staged. The response flags it, so the client, which drives the machine, can switch back itself. A refused capture whose switch back failed says so in the error text.
- **`services/read_project.py` / `ProjectResponse`:** additive `capture {available, reason_code, allowed_processes}`.
- **`schemas.py`:**
  - argument aliases `CaptureTargetArg`, `WindowMatchArg` (`{process?, title_contains?, window_id?}`), `CaptureRegionArg`, `DelayMsArg`;
  - `CaptureScreenshotRequest` with its cross-field validator: region requires target `region`; `window` and `region` need a `window_match` on desktop runners;
  - `CaptureScreenshotResponse`, `ListWindowsResponse`, `WindowItem`, `WindowDesktopItem`.
- **`mcp_app.py`:**
  - `list_windows` (READ annotations).
  - `capture_screenshot` (WRITE annotations), annotated `Annotated[CallToolResult, CaptureScreenshotResponse]`. It returns `CallToolResult(content=[TextContent(JSON), ImageContent(b64 preview, "image/jpeg")], structured_content=...)`.
  - `_ToolHandler` widens to `Awaitable[BaseModel | CallToolResult]`, and the tool count goes from 12 to 14.
  - The tool descriptions cover: check the preview; call `list_windows` first; use `window_id`; the machine switches to another desktop by itself and back (`desktop_switched` / `desktop_restored`; if `desktop_restored` is false, switch back yourself); use `delay_ms`; error codes; fall back to `add_screenshot`.
- **`workflow.py`:** prefer `capture_screenshot` when `get_project.capture.available` is true; otherwise capture the image yourself and send it with `add_screenshot`.
- **`services/hand_in.py`:** passes `capture_source` through to the stored screenshot.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<id>/settings/` | GET | `@project_membership_required` (read-only members see the list as text) | `ProjectCaptureAppsForm` (initial = the list) | `projects/settings.html` gets a "Screenshot capture" card; context `capture_apps_form`, `capture_apps`, `project_can_write` | n/a |
| `/projects/<id>/settings/capture-apps/` (`projects:settings_capture_apps`) | POST | `@project_membership_required` (writable; read-only gets 403) + `@require_POST` (Q3) | `ProjectCaptureAppsForm.apps`: textarea, one name per line, each checked with `normalize_process_name`, at most 20 | invalid: re-render through `render_project_settings(..., capture_apps_form=form)`; valid: flash "Allowed applications saved." and redirect to `#screenshot-capture` | an error per bad line; too many entries |

- New `projects/project_capture_apps_views.py`, a form in `projects/forms.py`, and a `capture_apps_form` keyword on `render_project_settings`.
- The card wording is neutral: "Screenshot capture. Applications whose windows may be captured on the test machine as screenshots for manual runs, one per line (for example `firefox.exe`; `Firefox` on macOS; `firefox` on Linux). Leave empty to turn capture off. The Test Runner itself, terminals and the system shell are never captured." Autoescaped, no `|safe`.

### Background work and WebSockets
- No Celery task. The capture runs synchronously inside the MCP tool call:
  1. a uvicorn worker thread, through `async_to_sync`, calls `group_send` to the controller group;
  2. Daphne's controller consumer forwards the message to the runner;
  3. the runner replies to the consumer;
  4. the consumer sends the result to the reply channel;
  5. it travels through redis back to the MCP worker thread.
- Both processes share `CHANNEL_LAYERS`, and `controller_screenshot` already moves replies of several MiB this way.
- New controller actions `list_windows` and `capture_window`, with replies `window_list_result` and `window_capture_result`. No browser WebSocket events.

### Settings / infrastructure
- No new env vars and no change to `example.env`.
- No new server dependency, and no new pip dependency in `controller_client/requirements.txt`.
- Linux system packages, checked by `setup.sh` and `diagnose --capture` and named in the error details:
  - `libx11-6` and `libxcomposite1` (X11, usually present);
  - `grim` plus `sway` or `hyprland` tools (wlroots);
  - `gnome-extensions` (GNOME).
- nginx `proxy_read_timeout 120` already covers the call.

### Controller compatibility and rollout
1. **Deploy the server.** The migrations are additive. Runners below 0.20.0 get `[machine_outdated]`, and `get_project.capture.available` is false with that reason.
2. **Update the runner.** A member uses Update Test Runner (self-update) or downloads the new ZIP. On a GNOME Wayland machine, the setup or update installs the helper extension, and the person logs out and back in once.
3. **Fill in the allowlist.** Until then the tools answer `[not_set_up]`.
4. **Older servers** ignore the new capability.

### Data flow (capture)
1. The client calls `get_project` to see `capture.available`, then `list_windows`. The result holds allowlisted windows across all desktops, each with its desktop.
2. The client claims a case, performs a step, and calls `capture_screenshot(..., target="window", window_match={process: "firefox.exe", title_contains: "Kitchen Sink"}, delay_ms=500)`.
3. The server checks the claim, the cap, readiness and the per-project lock, then dispatches `capture_window` with the allowlist and the limits.
4. The runner:
   1. checks the session;
   2. selects the window: allowlist and deny list, with the current desktop preferred;
   3. hides the person window (confirmed);
   4. if the window is on another desktop: switches to it and verifies the switch;
   5. settles and waits `delay_ms`, then selects the window again on the desktop now shown;
   6. grabs it, visible first, then the window-only image or `window_obscured`; after a switch, repeats the grab until two consecutive grabs match;
   7. switches back to the previous desktop and verifies it (`finally`);
   8. runs the blank check, crops, shows the person window again, encodes and replies with `desktop_switched` / `desktop_restored`.
5. The server parses the result, checks the process against the allowlist again, runs `check_manual_image`, and stages the shot with `capture_source="runner"`. It returns JSON, text and the preview image.
6. The client checks the preview and calls `mark_step`. At hand-in, the shot is stored with `capture_source="runner"`.

## Alternatives considered
- **Refuse windows on other desktops and let the client switch** (the earlier draft). Replaced by the user's choice to allow switching (Q11).
- **Switch only when the machine has been idle.** Not chosen by the user. It is also unreliable, because idle timers count the client's own synthetic input.
- **Capture windows on other desktops in place** (`PrintWindow` or Windows.Graphics.Capture, `screencapture -l` or ScreenCaptureKit, the GNOME helper's window image). Rejected: browsers stop painting cloaked or occluded windows, so the image can show an earlier state. Switching, then the freshness check, avoids that.
- **Windows: the undocumented `IVirtualDesktopManagerInternal` COM interface** (direct `SwitchDesktop`, no keys). Its IIDs and vtable layout change between Windows builds. VirtualDesktopAccessor / winvd now support only 24H2 26100.2605 and later; MScholtes/VirtualDesktop keeps a separate interface file per build; MaximizeToVirtualDesktop ships two vtable layouts, pre-24H2 and 24H2+. Each project needs a release after Windows updates, and a wrong vtable call through ctypes can crash the runner. `pyvda` (needs `comtypes`) has the same per-build dependence, and I could not confirm it supports 24H2. Rejected in favour of the system shortcut, which works on every build and is verified with the documented `IVirtualDesktopManager`. The cost is UIPI: an elevated window in front gives a clear `desktop_switch_failed`.
- **macOS Ctrl+Arrow / Ctrl+Number shortcuts.** There is no public way to learn the Space index, so the number of steps is unknown, and Ctrl+Number is off by default. Activating the window's app is the documented behaviour that reaches the right Space.
- **Moving the target window to the current desktop** instead of switching. That changes the app's window layout permanently, and the move is not documented on Windows either. Rejected.
- **`python-xlib~=0.33`.** It is the maintained Xlib binding, but pyautogui and mouseinfo already install `python3-Xlib` on Linux under the same `Xlib` import package. The two would overwrite each other's files depending on install order, which is the kind of on-disk drift `env_check.py` exists to catch. Using the unmaintained `python3-Xlib` directly is no better.
- **`mss~=10.x`.** Maintained, but it grabs root rectangles only (no window list, properties, Composite or pixmaps), so ctypes would still be needed. Rejected. ctypes against libX11 and libXcomposite is already the client's X11 pattern (`manual_hotkeys_x11.py`).
- **xdg-desktop-portal Screenshot or ScreenCast** (reusing the D-Bus approach of `manual_hotkeys_portal.py`). Screenshot is full screen only and has no window list. ScreenCast's persisted `restore_token` replays a source a person picked, and neither carries window identity, so the allowlist and `screen_not_clean` cannot be enforced. Rejected for capture. The D-Bus approach is reused for the GNOME helper and the logind and ScreenSaver lock checks.
- **KDE Wayland via a KWin script plus `ScreenShot2`.** Listing through a KWin script works, but `ScreenShot2` authorization needs a root-installed `.desktop` file whose `Exec` matches the resolved interpreter, which would authorise every Python program of that user. Rejected for v1 (`compositor_unsupported`; Plasma on X11 works).
- **Mutter's private ScreenCast `RecordWindow`** (GNOME without the extension). It needs window ids, and only the restricted Introspect API provides those, so the allowlist cannot be enforced without the extension.
- **Runner-local allowlist, bringing windows to the front, a runner-generated preview, returning the full image, refusing ambiguous matches, duplicating the Win32 enumeration.** All unchanged from the first draft. The Changelog records the earlier reasons.

## Risks and mitigations
- **Wrong content in the evidence.** Mitigations:
  - the allowlist is checked by the runner and again by the server;
  - the deny list (runner, terminals, shell) applies even to listed names;
  - the person window's hide must be confirmed;
  - `screen_not_clean` counts current-desktop windows;
  - off-desktop and obscured-without-faithful-path captures are refused.

  Residual: taskbar or menu bar icons and the clock appear in `screen` shots.
- **Desktop switching changes what a person at the machine sees.** Accepted by the user. Bounded to one switch there and one back per capture (at most about 6 s each, usually under 1 s), with no idle gate. The person window stays hidden for the whole switch.
- **The switch back fails, or the runner is killed mid-switch.** The switch back is in the same worker thread's `finally`, so it survives task cancellation and server timeouts. SIGTERM waits for it. A failure is reported as `desktop_restored=false` in the result, the error text and the runner log, so the client can switch back itself. A hard kill (SIGKILL, power loss) can leave the other desktop active; that cannot be avoided.
- **Allowlist after a switch.** The desktop now shown may hold foreign windows. The runner enumerates again, uses the visible-rect grab only for an unobscured target, otherwise the window-only image, and refuses `screen` with `screen_not_clean`.
- **Focus after a switch.** Windows, X11 and GNOME return focus to whatever the desktop manager restores. macOS activates the previous app and raises its recorded window. The runner never calls `SetForegroundWindow` or raises windows otherwise. A focused text field in the app under test can lose its caret state.
- **Windows shortcut fragility.** UIPI (an elevated window in front) is caught by `input_guard` before pressing. A shortcut remapped or blocked by policy fails verification, giving `desktop_switch_failed`. Stuck modifiers are released by the existing key path. The registry desktop order is undocumented: read-only, and a missing value gives `desktop_switch_failed`.
- **macOS.** "Switch to a Space with open windows" turned off, Accessibility missing, or an original Space with no window: each refused up front with `desktop_switch_failed`, so the desktop is never left switched.
- **Concurrency.** Server messages are handled one at a time by the runner (`_receive_messages` awaits each `_dispatch`), so no agent action runs during a capture. Window-originated captures share `screen_use_lock`. On the server, the per-project capture lock and the one-active-run / one-claim rules apply, Machine setup's `run_setup_task` and readiness runner calls are refused while the claim is not paused, and captures are refused while it is paused.
- **Stale frames.** Off-desktop windows are only captured after a switch and the stability check, never in place. Minimised windows are refused. Obscured windows use only window-only paths that read the window's own current buffer (`PrintWindow` full content, Composite pixmap, GNOME actor content), followed by the blank check. A browser that throttles an obscured but mapped window can still return a slightly old frame. The preview is the client's check.
- **Window-title privacy and prompt injection.** Only allowlisted windows are listed. Titles and desktop names are fenced as data.
- **No-trace rule.** Mitigations:
  - requests carry no claim or client data;
  - the runner logs no titles;
  - `capture_source` "runner" is shared with the window tester's shots;
  - error texts say "test machine";
  - the GNOME extension and its D-Bus name are neutral ("Test Runner capture").

  The verifier greps `capture_source`, `capture_window`, `mcp` and "runner capture" across templates, TestRail, exports, triage and broadcasts.
- **Undocumented Windows desktop index and name** (registry). Read-only, with null when unreadable. The documented `IVirtualDesktopManager` alone decides current versus other desktop.
- **Black frames.** Session checks run before the grab and the blank check after. `PrintWindow` can come back black for some GPU surfaces, which gives `capture_blank`.
- **Mixed-DPI multi-monitor on Windows.** Grab in `GetWindowRect` space (a system-DPI-aware process) and verify on a 100% plus 150% setup.
- **GNOME helper extension.** It is JavaScript against a moving Shell API (supported 45–49, `Version` handshake, `helper_missing` when outdated). It needs one re-login on Wayland. It runs inside gnome-shell, so a bug there can affect the session: keep it minimal, catch every error, write only inside the runner's runtime directory, and expose no input or eval. `get_image()` / `paint_to_content()` can return null, which gives `capture_failed`.
- **Flatpak and Snap apps on Linux.** The process name can be `bwrap`, a wrapper, or a sandbox pid. Verify Firefox Snap on Ubuntu and Flatpak; allowlist the reported name shown by `diagnose --capture`.
- **wlroots CLIs.** These are external tools whose versions vary. Use fixed argv, a timeout, defensive JSON parsing, and `helper_missing` when a tool is absent.
- **Blocking threads in the `mcp` process** (up to 45 s per capture). Bounded by the per-project lock, the SHOT throttle and a process-wide cap of 8 round trips (`[machine_busy]` beyond).
- **Large replies through redis** (about 9.4 MiB of base64). The same as `controller_screenshot`; the consumer caps the size.
- **A claim that ends during the capture.** `stage_shot` checks again under the row lock and drops the image.
- **Controller compatibility.** The capability is optional, `CLIENT_VERSION` 0.20.0, and older runners get `[machine_outdated]`. The `app_closer_windows` refactor keeps its own cloaked-window exclusion; smoke-test `close_app` and `close_tab`.
- **The runner starting on non-GNOME Linux** (X11, Sway, Hyprland). The agent backend stays GNOME-only. Verify that the runner connects and serves Manual and MCP there, with the GNOME backend's startup checks only warning.
- **Migrations.** Additive columns with defaults; metadata-only on Postgres 11+.

## Implementation steps
1. **Shared protocol (orchestrator, first).** Capability, message types, enums including the new statuses, constants, `normalize_process_name`, payloads with the desktop fields, parse and serialize, `CLIENT_VERSION` 0.20.0.
   Files: `controller_client/protocol.py`.
2. **Server models and migrations**, plus stub signatures of `controller_capture_actions`, `runner_capture_readiness` and the `store_manual_screenshot` keyword.
   Files: `projects/models.py`, `projects/migrations/0060_capture_allowlist.py`, `mcp_gateway/models.py`, `mcp_gateway/migrations/0003_staged_capture_source.py`, `projects/controller_capture_actions.py`, `projects/runner_capture_readiness.py`.
3. **Runner capture.** Steps 3a to 3f own disjoint files and run as parallel sub-agents after step 1. Step 3a goes first or together with them, because the others build on its shared modules.
   - **3a. Core** (one sub-agent).
     Files: `controller_client/window_model.py`, `controller_client/capture_platform.py` (platform Protocols, module export convention, `detect_capture_platform`; written by the orchestrator in step 1/2), `controller_client/window_inventory.py`, `controller_client/capture_session.py`, `controller_client/window_capture_policy.py`, `controller_client/window_grab.py`, `controller_client/desktop_switch.py`, `controller_client/capture_freshness.py`, `controller_client/screen_use_lock.py`, `controller_client/window_capture.py`, `controller_client/manual_capture.py`, `controller_client/client.py`, `controller_client/diagnose.py`.
   - **3b. Windows.**
     Files: `controller_client/win32_windows.py`, `controller_client/app_closer_windows.py`, `controller_client/win32_virtual_desktops.py`, `controller_client/win32_desktop_switch.py`, `controller_client/capture_session_windows.py`, `controller_client/window_grab_windows.py`.
   - **3c. macOS.**
     Files: `controller_client/window_inventory_macos.py`, `controller_client/capture_session_macos.py`, `controller_client/window_grab_macos.py`, `controller_client/macos_desktop_switch.py`, and `controller_client/stubs/` if pyobjc symbols need stubs.
   - **3d. Linux X11 and session checks.**
     Files: `controller_client/x11_lib.py`, `controller_client/window_inventory_x11.py`, `controller_client/window_grab_x11.py`, `controller_client/x11_desktop_switch.py`, `controller_client/capture_session_linux.py`.
   - **3e. Linux wlroots.**
     Files: `controller_client/wayland_wlroots.py`, plus the `controller_client/scripts/setup.sh` package hints.
   - **3f. Linux GNOME helper** (Q12 resolved: yes).
     Files: `controller_client/gnome_capture_helper/metadata.json`, `controller_client/gnome_capture_helper/extension.js`, `controller_client/gnome_shell_capture.py`, `controller_client/scripts/setup.sh` (extension install), `controller_client/update_installer.py` (re-install).

   The platform modules implement the inventory, grab, session and `DesktopSwitcher` interfaces 3a defines in `window_model.py` / `desktop_switch.py`. The orchestrator writes that file before the sub-agents start.
4. **Server controller seam** (sub-agent B).
   Files: `projects/controller_protocol.py`, `projects/controller_consumer.py`, `projects/controller_reply_tracker.py`, `projects/controller_capture_actions.py`, `projects/runner_capture_readiness.py`, `projects/manual_screenshot_services.py`, `projects/runner_manual_services.py`.
5. **Settings card** (sub-agent C).
   Files: `projects/project_capture_apps_services.py`, `projects/forms.py`, `projects/project_capture_apps_views.py`, `projects/urls.py`, `projects/testrail_views.py`, `templates/projects/settings.html`.
6. **MCP gateway** (sub-agent D).
   Files: `mcp_gateway/limits.py`, `mcp_gateway/services/call_guard.py`, `mcp_gateway/services/stage_shot.py`, `mcp_gateway/services/add_screenshot.py`, `mcp_gateway/image_preview.py`, `mcp_gateway/services/capture_errors.py`, `mcp_gateway/services/list_windows.py`, `mcp_gateway/services/capture_screenshot.py`, `mcp_gateway/services/read_project.py`, `mcp_gateway/schemas.py`, `mcp_gateway/mcp_app.py`, `mcp_gateway/workflow.py`, `mcp_gateway/services/hand_in.py`.
7. **Integration (orchestrator).** Wire the seams. Run `pre-commit run --all-files` (server and controller mypy), `manage.py check`, `makemigrations --check`, `sqlmigrate` and `scripts/verify_server_image.sh`. Write the change note and the CLAUDE.md paragraphs: MCP gateway 14 tools and its import rule; controller `WINDOW_CAPTURE_V1` 0.20.0; the GNOME helper.

## Verification
- **Static:** `pre-commit run --all-files`, `manage.py check`, `makemigrations --check`, `scripts/verify_server_image.sh`.
- **Windows** (0.20.0, logged in, allowlist `firefox.exe` and `msedge.exe`; three virtual desktops named "Main", "Testing", "Other"):
  - `list_windows`:
    - shows Firefox on desktop 2 "Testing" (`current: false`) and Edge on desktop 1 (`current: true`);
    - `others_hidden` is above 0 while a terminal is open.
  - Desktops:
    - capturing Firefox while desktop 1 is active switches to desktop 2 and back. The shot shows the page's current state: change it on desktop 2 first, then switch to desktop 1 by hand and capture. `desktop_switched` and `desktop_restored` are both true, and focus is back on the desktop 1 window.
    - capture from desktop 3 to a window on desktop 1: two steps left, and back.
    - a playing video on desktop 2 gives `[window_not_settled]`, and desktop 1 is active again.
    - an elevated window (Task Manager) in front gives `[desktop_switch_failed]` with the administrator detail, and the desktop is unchanged.
    - registry order unavailable (simulated) gives `[desktop_switch_failed]`.
    - closing the socket mid-switch (stop the server): the runner still switches back.
    - a terminal visible on desktop 2 and `screen` target: `[screen_not_clean]` after the switch, and desktop 1 restored.
    - an obscured Firefox window on desktop 2: the window-only image, with no foreign window in it.
    - a window-tester hotkey capture during an MCP capture waits on the shared lock.
  - Captures:
    - `window` capture, with the preview checked;
    - `region` in bounds, and out of bounds (refused);
    - `delay_ms=3000`;
    - an obscured Firefox window gives the window-only image with no foreign window in it;
    - a dropdown open on an unobscured window is visible in the shot.
  - Refusals:
    - a terminal as target: `[not_allowed]`;
    - minimised: `[window_minimized]`;
    - no match: `[window_not_found]`;
    - `screen` with a terminal visible on the current desktop: `[screen_not_clean]`. With only the terminal on another desktop, the capture is OK.
  - Session and window states:
    - killing the person window during the hide gives `[hide_failed]`;
    - session 0, a locked screen and a disconnected RDP session each give `[not_interactive]`.
  - Other checks:
    - on mixed DPI, the crop matches;
    - `close_app` and `close_tab` still work.
- **macOS:**
  - without Screen Recording: `[permission_missing]`;
  - window, region and screen captures;
  - a window on another Space: switch there and back, fresh shot, previous app frontmost again; minimised gives `[window_minimized]` (with Accessibility);
  - without Accessibility, auto-switch off, or an empty original Space: `[desktop_switch_failed]`, Space unchanged.
- **Linux X11** (Ubuntu "GNOME on Xorg" and Xfce):
  - list with workspaces and names;
  - window, region and screen capture;
  - a window on another workspace: switch and back through `_NET_CURRENT_DESKTOP`, verified;
  - minimised gives `[window_minimized]`;
  - obscured with a compositor gives the window-only image; obscured on Xfce with the compositor off gives `[window_obscured]`;
  - locked gives `[not_interactive]`;
  - unset `DISPLAY` gives `[not_interactive]`.
- **Linux Sway and Hyprland:**
  - list with workspaces;
  - window and region capture through `grim`;
  - a window on another workspace: `swaymsg workspace` / `hyprctl dispatch workspace` there and back, with each output's or monitor's workspace restored (two monitors);
  - a floating window over the target gives `[window_obscured]`;
  - uninstalling `grim` gives `[helper_missing]`.
- **GNOME Wayland:**
  - without the extension: `[helper_missing]` with instructions;
  - after setup and a re-login: list with workspaces; window capture, including an obscured window, through the helper; `screen`;
  - another workspace: `ActivateWorkspace` there and back, verified; an older helper without the method gives `[helper_missing]`;
  - locked gives `[not_interactive]`;
  - an older helper `Version` gives `[helper_missing]`.
- **KDE Plasma Wayland:** `[compositor_unsupported]`. Plasma X11 is OK.
- **Flatpak or Snap Firefox:** the reported process name matches the name `diagnose --capture` shows.
- **Phone:** a single device entry; `screen` with an allowlisted foreground app is OK; `window` gives `[unsupported]`.
- **Server refusals:** `[machine_offline]` immediately, `[machine_outdated]`, `[not_set_up]`, a concurrent capture gives `[machine_busy]`, the 61st staged shot is refused before dispatch, a claim released during the delay gets nothing staged, and a bad sha256 or an oversized reply gives `[capture_failed]`.
- **MCP clients:** Inspector shows 14 tools and the output schema. The result holds text, an image (at most 400 KiB, long edge at most 1280) and the structured content. Claude and Codex see the preview.
- **Hand-in and no trace:** `capture_source` is "runner" for both kinds of runner capture and "" for `add_screenshot`. TestRail output looks the same as a runner-window hand-in. The logs carry no titles or case data. The Settings card is neutral, and read-only members get 403.
- Run the `reviewer` and `verifier` agents.

## Open questions
None. Q1–Q12 are resolved (see Changelog).

## Changelog
- 2026-11-07: created (Draft).
- 2026-11-07: user answers applied; Status Ready (Q11 and Q12 gate only step 3f and the desktop-switching follow-up).
  - **Resolved as recommended:**
    - Q2: `list_windows` returns allowlisted windows only, plus `others_hidden`.
    - Q3: writable members edit the allowlist on the Settings tab.
    - Q4: an empty allowlist means capture off (`[not_set_up]`).
    - Q5: phone runners capture screen or region only, with the foreground app id allowlisted.
    - Q6: `TestRunScreenshot.capture_source = "runner"` for both kinds of runner capture, never rendered.
    - Q7: several matches pick the topmost window and report `other_matches`.
    - Q8: visible first, window-only fallback, never raise a window.
    - Q9: `list_windows` needs no claim.
  - **Q1 revised for virtual desktops:**
    - windows are listed across all desktops, with the current flag, index and name where the OS exposes them (Windows: `IVirtualDesktopManager` plus read-only registry names; macOS: on screen plus Accessibility, no Space index; X11: EWMH; Sway and Hyprland: IPC; GNOME: helper);
    - captures are limited to the current desktop, otherwise `[window_on_other_desktop]`, and the runner never switches desktops;
    - `screen` is the current desktop's monitor, judged by `screen_not_clean` over current-desktop windows;
    - new statuses `window_on_other_desktop` and `window_obscured`.
  - **Q10 revised, Linux supported:**
    - X11 through ctypes libX11 and libXcomposite (no new pip dependency: `python-xlib` would collide with pyautogui's `python3-Xlib`, and `mss` covers only root grabs);
    - Sway and Hyprland through `swaymsg` / `hyprctl` plus `grim`;
    - GNOME Wayland through the bundled helper extension (Q12);
    - KDE Wayland and other compositors refused;
    - portals rejected for capture (no window identity);
    - Linux session checks (no display, no session bus, logind `LockedHint` or ScreenSaver lock);
    - new statuses `compositor_unsupported` and `helper_missing`. `unsupported` is now phone-only.
- 2026-11-07: Q11 and Q12 answered; Status Ready.
  - **Q12 resolved: yes.** Bundle the GNOME Shell helper extension. It gains `ActiveWorkspace` and `ActivateWorkspace`.
  - **Q11 resolved: "Allow switching"**, chosen over "never" and "only when idle". The runner switches to the window's desktop, verifies the switch, waits `DESKTOP_SWITCH_SETTLE_MS` + `delay_ms`, grabs until two consecutive grabs match (≤ 0.5% changed, within 3 s, else `[window_not_settled]`), captures, and switches back in a `finally` inside the same worker thread. A failed switch back sets `desktop_restored=false`.
  - Mechanisms:
    - Windows: Win+Ctrl+Arrow through the existing key path, guarded by `input_guard` and verified with the documented `IVirtualDesktopManager`. `IVirtualDesktopManagerInternal` rejected because it changes per build.
    - macOS: AX raise plus app activation (auto-switch to the Space); the switch back activates the recorded app.
    - X11: `_NET_CURRENT_DESKTOP` client message.
    - Sway / Hyprland: IPC, with per-output or per-monitor workspaces restored.
    - GNOME: the helper.
    - KDE Wayland: still refused.
  - New statuses `desktop_switch_failed` and `window_not_settled`. `window_on_other_desktop` is kept only for `switch_desktop=false`. New wire fields `switch_desktop`, `desktop_switched` and `desktop_restored`.
  - Shared `screen_use_lock` with `ManualCapturer`. Reply timeout raised to 50 s and lock to 60 s.
- 2026-11-07 review round 1 (reviewer ×2 + verifier), all findings accepted and fixed:
  - **Runner, correctness:** the desktop switcher now gets windows listed after the hide, without the runner's own pids (`switcher_windows`).
  - **Runner, security:**
    - One rectangle-overlap check (`unobscured_by_stacking`) replaces the sample-point grids on every platform.
    - Inventories return every visible window: `DesktopWindow.capturable` / `drawn`; overlays count as foreign.
    - Windows with an unknown pid are never capturable.
    - The GNOME helper requires a runner token, at helper version 2, and writes only through its own stream.
    - No "MCP" wording is shipped in `controller_client/`.
  - **Runner, robustness:**
    - X11 IO error exit handler.
    - A failed macOS session check now proceeds.
    - The `_HiddenWindows` global is gone.
    - Deduplicated into `shell_processes.py`, `session_bus.py` and `process_names.py`.
  - **Server:**
    - The preview shrinks before converting and reuses the integrity-check decode.
    - Reply timeout 45 s, decode deadline 55 s.
    - Process-wide round-trip cap (`services/capture_lock.py`), with an atomic Redis lock release.
    - `desktop_item` moved to `services/capture_mapping.py`.
    - The view maps `CaptureAppsError`.
    - The admin excludes `capture_source`.
  - **Accepted residual risks:** wlroots layer-shell surfaces (bars, notifications) are not seen. On libX11 < 1.7 a lost X connection still exits the runner. Any unsandboxed same-user process can read the GNOME token file.
- 2026-11-07 review round 2:
  - **Server:**
    - The capture lock's Redis client has 2 s socket timeouts.
    - A failed lock release only logs.
    - `stage_shot` re-checks the 55 s deadline before staging.
  - **Runner, security:**
    - GNOME on Xorg uses the GNOME helper (`CapturePlatform.GNOME_X11`; `wayland_gnome.py` renamed `gnome_shell_capture.py`).
    - Cinnamon and other shells that draw notifications in the compositor: `screen` is refused, and window and region captures use the Composite window-only grab only.
    - Every on-screen grab is re-checked against a fresh inventory, with one retry, then a refusal.
  - **Runner, decision:** a covering window of an allowlisted, non-denied process no longer counts as obscuring (`obscuring_windows`), so allowlisting an always-on overlay (NVIDIA, Steam) clears it for window captures as it already did for the screen rule. A blank window-only image names the covering processes.
  - **Runner, small fixes:**
    - `list_windows` holds the screen-use lock.
    - GNOME token `renew(stale)`.
    - The bus is disconnected after a failed connect.
- 2026-11-07 review round 3:
  - **Server:**
    - A Redis error on taking the capture lock, or in `capture_in_progress`, answers `[machine_busy]`.
  - **Runner:**
    - The late re-check retries only a newly covered window; a closed or moved window is refused at once.
    - X11 now lists the window manager's own override-redirect windows (Alt+Tab switchers, on-screen displays) as overlays.
    - The GNOME helper's `shellOverlays` fails closed (every monitor counts as an overlay when the Shell internals are missing).
    - Dock and desktop windows are ignored only when their process is a known shell or panel (`shell_processes.LINUX_SHELL_SURFACE_PROCESSES`, mirrored in `extension.js`); any other "dock" is an overlay.
  - **Accepted residual risks:**
    - A process that renames itself to a listed shell or panel name is trusted.
    - On X11, a client that sets `_NET_WM_PID` to a listed shell's pid is trusted. The X11 allowlist already trusts `_NET_WM_PID`; checking the pid with XRes `XResQueryClientIds` is a possible follow-up. GNOME takes the pid from Mutter and is not affected.
    - KWin compositor effects (Alt+Tab drawn in the scene) are invisible to every inventory.
    - Ubuntu's desktop-icons window (`gjs`) makes `screen` refuse on stock Ubuntu; window captures are unaffected.
    - The macOS Dock display-sized window exclusion must be checked on a Mac against Mission Control and App Exposé.
