# feat: Test Runner on GNOME Wayland (Ubuntu 26.04)

Status: Done
Created: 2026-10-28
Request: Make `controller_client/` (the Test Runner) work on Linux test machines
that run a Wayland session. New Ubuntu releases offer only Wayland: 25.10 dropped
the GNOME-on-Xorg session, and 26.04 runs GNOME 50 on Wayland only. The user wants
Wayland only on Linux. Windows and macOS runners must keep working exactly as they
do today. Observed failure: on an Ubuntu 26.04 Hyper-V VM, `python -m
controller_client.diagnose` crashes at import time. `screen_capture.py` runs
`import pyautogui`, which imports `mouseinfo`, which opens
`Xlib.display.Display(":0")`, which raises `DisplayConnectionError: Authorization
required`. Even with an Xwayland cookie, pyautogui cannot capture or drive native
Wayland windows. The runner needs a screen capture and input path that works on
GNOME Wayland, including unattended (Night mode) runs.

## Context

**Where the runner touches the desktop.** All of this was read in the code.
- `controller_client/executor.py` imports `pyautogui` at the top and sets
  `pyautogui.FAILSAFE = False`. It uses `click`, `moveTo` (hover, before a drag,
  and in the macOS double-click), `drag`, `doubleClick` (Windows and Linux),
  `typewrite`, `press`, `hotkey` and `isValidKey`. The macOS double-click posts
  Quartz events under `if sys.platform == "darwin": import Quartz`. Key names go
  through a per-platform alias table, `_KEY_ALIASES`, and then
  `pyautogui.isValidKey` (`docs/changed/2026-10-22-key-press-modifiers.md`). Unknown
  keys raise `ExecutionError`. Every input action calls `ensure_input_not_blocked()`
  first.
- `controller_client/screen_capture.py` imports `pyautogui` at the top. macOS
  already bypasses it and calls `screencapture -x`. Everything else calls
  `pyautogui.screenshot()`. Callers use `capture_screen()` and
  `describe_screen_capture_failure()`.
- `controller_client/system_info.py` imports `pyautogui` at the top. Screen size
  comes from `screeninfo.get_monitors()[0]`, falling back to `pyautogui.size()`. The
  `screeninfo` fallback only catches `IndexError` and `RuntimeError`.
  `gather_system_info()` runs in the handshake (`client.py::_send_handshake`).
- `controller_client/input_guard.py`: the UIPI check is Windows only and is a no-op
  elsewhere. No change.
- `controller_client/diagnose.py`, `env_check.py` and `diagnostics.py`: one
  "screen capture" step plus a hint that mentions `DISPLAY` for Linux.
  `main.py` runs `verify_screen_capture()` at startup and only warns on failure.
- `controller_client/app_closer.py` already refuses on Linux and tells the agent to
  use `key_press alt+F4` or `ctrl+w`. Platform modules are chosen by top-level
  `if sys.platform == ...: from ..._macos import ...`. Those modules guard their
  platform-only imports with `if sys.platform == ...` too (`app_closer_macos.py`,
  `input_guard.py`). This is the import convention this plan follows.
- `controller_client/app_launcher.py` and `app_discovery.py` (Linux branch) scan
  `.desktop` files in `/usr/share/applications` and `~/.local/share/applications`
  and `Popen` the `Exec` line with the runner's environment. This works under
  Wayland because children inherit `WAYLAND_DISPLAY`. They do not scan snap
  (`/var/lib/snapd/desktop/applications`) or flatpak export directories. That gap
  predates this work and is not Wayland-specific (Q5).
- `controller_client/browser_executor.py` uses Playwright through CDP or Juggler,
  never OS input. A headed Chromium runs either through Xwayland or natively, and
  Firefox runs natively on Wayland. No change is expected; the manual checks cover
  it.
- `controller_client/client.py` handles messages one at a time
  (`_receive_messages` awaits each `_dispatch`). Desktop actions run in
  `asyncio.to_thread`, so only one desktop action is in flight at a time, but it
  may run on any worker thread.
- `controller_client/protocol.py`: `CLIENT_VERSION = "0.6.0"`.
  `REQUIRED_CLIENT_CAPABILITIES` = interactive commands and cleanup. `system_info`
  is a free-form `dict[str, str | int]`. The module is stdlib only, and the server
  imports it (`projects/controller_protocol.py`).
- `controller_client/requirements.txt` pins `PyAutoGUI==0.9.54`,
  `screeninfo~=0.8.1`, `Pillow~=11.2.1` and `types-PyAutoGUI`. `setup.cfg` runs
  strict mypy with local stubs in `stubs/` (Quartz, AppKit only).
  `scripts/setup.sh` apt-installs `gnome-screenshot`, which pyautogui uses on X11.
- Server-side coordinate contract: `projects/services.py::controller_find_elements`
  says that OmniParser elements "are in the screenshot's pixel space, which is the
  space the runner's clicks use". So `click(x, y)` must hit pixel `(x, y)` of the
  image returned by the last `screenshot_request`. A runner may meet this however
  it likes. A known break is that macOS Retina gives physical pixels to
  `screencapture` but points to pyautogui. That is pre-existing, out of scope, and
  noted in `docs/changed/2026-10-24-remote-vision-worker.md`.
- Server prompt: `agents/services/prompt_parts.py` describes every non-Windows,
  non-macOS runner as "Ubuntu 24.04 with XFCE4" and "XFCE4 ... on display :0".
  This text comes from the old Docker test environment
  (`docs/impl/006.Create Testing Environment-impl.md`). It is wrong for GNOME
  Wayland (Q4).
- Packaging: `generate_controller_client_zip` puts every tracked file under
  `controller_client/` into the ZIP, so new modules ship automatically. The server
  imports only `controller_client.protocol`.

**What the platform offers (researched; sources at the end).**
- **Mutter's own D-Bus APIs** are `org.gnome.Mutter.RemoteDesktop` and
  `org.gnome.Mutter.ScreenCast`, on the session bus. The xdg-desktop-portal GNOME
  backend, gnome-remote-desktop and `gnome-ponytail-daemon` (the dogtail helper
  for Wayland) all sit on top of them.
  - In mutter master (checked 2026-10-25), `handle_create_session` in
    `meta-remote-desktop.c` and `meta-screen-cast.c` performs **no caller check**.
    `MetaDbusAccessChecker` is used only by the a11y manager. The only gate is
    *inhibition*.
  - gnome-shell inhibits remote access in every session mode except `user`
    (`js/ui/main.js`; `allowScreencast` is true only in the `user` mode in
    `js/ui/sessionMode.js`). So the **lock screen and the login screen terminate
    every running session and refuse new ones** with "Session creation inhibited".
  - The interface XML calls the API private, with no compatibility promise between
    versions. In practice the methods used here have not changed since GNOME 3.28.
  - Sessions end when the calling D-Bus connection closes.
- **Coordinate space (from mutter source).** A `RecordMonitor` stream produces
  frames of `logical size x scale`, which is physical pixels
  (`meta-stream-source-monitor.c::get_specs`). For a monitor stream,
  `NotifyPointerMotionAbsolute(stream, x, y)` maps `x / scale + monitor origin`
  (`meta-stream-monitor.c::transform_position`). **Coordinates in the frame's
  pixel space go straight to input**, with integer or fractional scaling. This is
  exactly the server's contract. The stream's `Parameters.size` is the *logical*
  size, so it must not be used as the screenshot size.
- **New consumers get a frame.** Enabling a monitor stream source queues a stage
  redraw (`meta-stream-source-monitor.c`), so a consumer that connects to an idle
  screen still receives one frame.
- **Keysym input.** `NotifyKeyboardKeysym` looks up the keycode in the current
  layout and applies the shift level by itself
  (`native/meta-virtual-input-device-native.c::apply_level_modifiers`). A keysym
  missing from the layout is dropped with only a compositor log warning. The D-Bus
  call still succeeds.
- **Portals.** RemoteDesktop v2 supports `persist_mode` (2 = until revoked) and a
  single-use `restore_token` in `SelectDevices`. Persistence for combined
  RemoteDesktop+ScreenCast sessions must go through RemoteDesktop, not
  `ScreenCast.SelectSources`.
  - xdg-desktop-portal-gnome supports it (MR !88, 2023).
  - One report on GNOME Shell 50.5 with xdg-desktop-portal 1.22.1 and
    xdg-desktop-portal-gnome 50.0 says new processes restored "in 0.1 s with no
    dialog". The pointer path and restore after a reboot were not tested there.
  - A token that cannot be restored makes the portal "prompt as usual". On an
    unattended machine, that dialog waits for a click nobody makes.
  - `NotifyPointerMotionAbsolute` and the other Notify methods mirror Mutter's
    methods. They stop working once `ConnectToEIS` is used.
- **Screenshot portal.** With `interactive=false`, the first call from an
  unsandboxed app without an app id shows "Allow Apps to Take Screenshots?" once.
  The answer is stored in the permission store for all such apps and survives
  reboots (`desktop-portal/screenshot.c`). It returns a PNG file of the whole
  stage.
- **`org.gnome.Shell.Screenshot`** only accepts `org.gnome.SettingsDaemon.MediaKeys`
  and the GNOME portal (`js/ui/screenshot.js` `DBusSenderChecker`). It cannot be
  used here.
- **`grim`** needs `wlr-screencopy` and does not run on GNOME.
- **ydotool** needs `/dev/uinput` access (root or a udev rule) and a daemon. It
  moves the pointer relative to its position, subject to libinput acceleration, so
  absolute positioning is unreliable. It types US keycodes, so other layouts break.
  It cannot take screenshots.

## Goals
- On Linux, the Test Runner captures the screen and drives mouse and keyboard in a
  GNOME Wayland session (Ubuntu 26.04 / GNOME 50 first) with no X11, no pyautogui
  import and no consent dialog. Night (unattended) runs work as long as the
  session is logged in and unlocked.
- Screenshot pixels and click coordinates share one space on HiDPI and fractional
  scaling.
- `python -m controller_client.diagnose` on Linux explains each missing
  prerequisite: not a GNOME Wayland session, started outside the desktop session
  (SSH or systemd), Mutter APIs missing, screen locked, GStreamer PipeWire tools
  missing, lock screen or blanking enabled.
- Windows and macOS: same actions, messages, screenshots, handshake fields and
  dependencies as today. The pyautogui code is moved, not rewritten.
- Strict mypy stays clean with no new `type: ignore` and no new local stubs.

## Non-goals
- No X11/Xorg path on Linux (Q1). Other Wayland desktops (KDE, wlroots) are not
  supported. The runner reports that the GNOME APIs are missing.
- No portal-based backend in this plan (Alternatives, Q2).
- No multi-monitor support: the primary monitor is captured and driven. On
  Windows, pyautogui also captures the primary monitor only.
- No macOS Retina coordinate fix.
- No new controller actions. No change to the wire messages. No new required
  capability.
- No snap or flatpak app discovery (Q5). No autostart unit for the runner (Q6).
- Setup does not change lock, idle or autologin settings automatically (Q3).

## Design

### Data model
None (server). The runner's handshake `system_info` gains two optional string keys
on Linux only: `display_server` (`"wayland"`) and `desktop` (from
`XDG_CURRENT_DESKTOP`, for example `"ubuntu:GNOME"`). Windows and macOS send exactly
today's keys. `Project.agent_system_info` is a JSONField rendered with
`json_script`, so extra keys need no migration and are XSS-safe.

### Runner modules (controller_client)

A small backend interface, chosen by a platform dispatch module the same way
`app_closer.py` does it. Linux gets the GNOME backend and every other platform gets
pyautogui. Selection uses `sys.platform`, so mypy narrows it statically and nothing
is chosen at runtime. No runtime session sniffing is needed because Linux is
Wayland-only (Q1).

**Import rule.** Every import is at module top level. `pyautogui` is imported only
by `desktop_pyautogui.py`, and that module is imported only under
`if sys.platform != "linux"` in `desktop.py`. This is the same conditional
top-level import already used for `Quartz` and the `app_closer_*` modules. It is
not a lazy import, so **no exception to the rule is needed**. The GNOME backend
connects to D-Bus on first use, not at import. That is lazy *connection*, not lazy
import, so importing `diagnose` or `client` can never crash on a machine without a
display.

New and changed modules:

| Module | Status | Responsibility |
|---|---|---|
| `desktop_backend.py` | new, **shared, written first** | `DesktopBackend` Protocol (below). Imports only `PIL.Image`, `collections.abc` and `diagnostics.DiagnosticStep`. |
| `desktop.py` | new, **shared, written first** | Platform dispatch: `if sys.platform == "linux": from controller_client.desktop_gnome import BACKEND as _BACKEND`, else the same from `desktop_pyautogui`. Exposes `get_desktop_backend() -> DesktopBackend`. |
| `exceptions.py` | changed, **shared, written first** | Adds `DesktopSessionError(ExecutionError)`: the desktop session refused or lost remote control. Carries the remedy text. |
| `desktop_pyautogui.py` | new | The Windows and macOS backend. Today's pyautogui calls, `FAILSAFE = False`, the macOS Quartz double-click, the per-platform `_KEY_ALIASES` with `isValidKey`, the `screeninfo` then `pyautogui.size()` screen size, and the screenshot path (macOS `screencapture`, otherwise `pyautogui.screenshot()`). Moved verbatim from `executor.py`, `screen_capture.py` and `system_info.py`. |
| `screen_capture_macos.py` | new (moved) | The current macOS `screencapture` helpers and `MACOS_PERMISSION_HINT`, unchanged. Not imported on Linux. |
| `desktop_gnome.py` | new | The GNOME Wayland backend. Implements the Protocol on top of `gnome_remote_session`, `pipewire_frame` and `keysyms`. Does pointer moves, button press and release, double-click (two clicks well inside GNOME's 400 ms default), drag in interpolated steps over `duration`, typing one keysym per character with `interval`, and key combinations (modifiers down in order, released in reverse). Checks coordinates against the last frame size. Builds `session_info()` and `diagnostic_steps()`. |
| `gnome_remote_session.py` | new | A `dbus-fast` client for `org.gnome.Mutter.RemoteDesktop` and `org.gnome.Mutter.ScreenCast`. Owns one private asyncio loop in a daemon thread, and the sync methods submit to it with a timeout. Holds one lock, so a call from any `to_thread` worker is serialized. Session lifecycle is below. |
| `pipewire_frame.py` | new | Grabs one frame from a PipeWire node into a `PIL.Image`. Runs `gst-launch-1.0 -q pipewiresrc <target> num-buffers=1 ! videoconvert ! pngenc ! filesink location=<tmp>` in a `TemporaryDirectory` with a timeout, the same pattern as the macOS `screencapture` path. Maps failures to `ScreenCaptureError`. Also provides a check for missing tools (`gst-launch-1.0`, `gst-inspect-1.0 pipewiresrc`). |
| `keysyms.py` | new | Pure tables, no I/O. Maps pyautogui-style key names (the set the agent already uses: `enter`, `esc`, `tab`, `backspace`, `delete`, arrows, `home`/`end`, `pageup`/`pagedown`, `f1`-`f24`, `ctrl`/`shift`/`alt`/`win` and left/right forms, `space`, `capslock`, `printscreen`, single characters) to X keysyms. Linux aliases follow today's non-macOS table: `cmd`/`command`/`meta`/`super`/`win` map to Super, `opt`/`option` to Alt, `control` to Ctrl. Characters map to keysyms: Latin-1 code points map to themselves, others to `0x01000000 + code point`. |
| `gnome_diagnostics.py` | new | The Linux `DiagnosticStep`s (Verification lists them). Uses read-only probes only: environment variables, D-Bus `NameHasOwner`, a session start, one frame, `gsettings get`. |
| `executor.py` | changed | Desktop functions keep their names and signatures (`client.py` and the `app_closer_*` modules import `execute_key_press`). Their bodies call the backend. `_normalize_key` keeps the generic parsing (split on `+`, the empty-key error, the lowercase rule in combinations) and asks `backend.resolve_key()`; `None` raises the same "Unknown key" error. Drops the `pyautogui` and `Quartz` imports. |
| `screen_capture.py` | changed | `capture_screen()` and `describe_screen_capture_failure()` keep their signatures and delegate to the backend. The `pyautogui` import moves out. |
| `system_info.py` | changed | Screen size comes from `backend.screen_size()`. `backend.session_info()` entries are merged into `to_dict()` (empty for pyautogui, so Windows and macOS are unchanged). The `pyautogui` and `screeninfo` imports move into `desktop_pyautogui.py`. |
| `diagnose.py` | changed | The default run executes `backend.diagnostic_steps()`. The pyautogui backend returns today's single "screen capture" step. Its hint loses the now-wrong Linux `DISPLAY` clause and keeps the Windows and macOS text. |
| `protocol.py` | changed | `CLIENT_VERSION` "0.6.0" becomes "0.6.1". This is informational so logs can tell a Wayland-capable runner apart. The wire format is unchanged, so no capability changes. |

**`DesktopBackend` Protocol** (signatures only; this is the contract every part
builds on):
- `name: str`: for example `"pyautogui"` or `"gnome-wayland"`, used in logs and
  diagnose output.
- `click(x: int, y: int, button: str) -> None`
- `double_click(x: int, y: int) -> None`
- `move_to(x: int, y: int) -> None`
- `drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str, duration: float) -> None`
- `type_text(text: str, interval: float) -> None`
- `resolve_key(name: str) -> str | None`: applies backend aliases and returns the
  backend's key name, or `None` when the key is unknown.
- `press_keys(keys: Sequence[str]) -> None`: takes names that `resolve_key` has
  already returned. One key means press; several mean hotkey.
- `screenshot() -> PIL.Image.Image`: raises `ScreenCaptureError` with the reason
  and the remedy.
- `screen_size() -> tuple[int, int]`: never raises, because the handshake must
  still succeed so browser tools keep working. It returns `(0, 0)` and logs when
  the size is unknown.
- `session_info() -> dict[str, str]`: extra `system_info` keys.
- `diagnostic_steps() -> tuple[DiagnosticStep, ...]`

Input errors may raise any exception. `executor.py` keeps today's wrapping
(`"Click failed: ..."` and so on). `DesktopSessionError` messages are written to be
read by the agent and the person, for example: "The screen is locked or the login
screen is showing; GNOME stops remote control there. Unlock it and disable the lock
screen on test machines."

**GNOME session lifecycle (`gnome_remote_session`).** Exact signatures are below
under "Interfaces between parts".
1. On first use, connect to the session bus. If there is no
   `DBUS_SESSION_BUS_ADDRESS` and no `$XDG_RUNTIME_DIR/bus`, raise
   `DesktopSessionError` with the advice to start the runner from the desktop
   session.
2. Run `RemoteDesktop.CreateSession()` and read `SessionId`. Then run
   `ScreenCast.CreateSession({"remote-desktop-session-id": id})`, followed by
   `RecordMonitor("", {"cursor-mode": 0})`. The empty connector means the primary
   monitor. A hidden cursor matches today's screenshots. `disable-animations` and
   `is-recording` keep their defaults, so nothing changes visually beyond GNOME's
   remote-access indicator.
3. Add match rules for the stream's `PipeWireStreamAdded` and both sessions'
   `Closed`, then run `RemoteDesktop.Session.Start()`. Wait up to 5 s for the node
   id.
4. Input calls `NotifyPointerMotionAbsolute(stream_path, x, y)`,
   `NotifyPointerButton(evdev_code, pressed)` (BTN_LEFT 0x110, BTN_RIGHT 0x111,
   BTN_MIDDLE 0x112) and `NotifyKeyboardKeysym(keysym, pressed)`.
5. A `Closed` signal (lock screen, monitor change, mutter restart) marks the
   session dead. The next call rebuilds it once. "Session creation inhibited" maps
   to the lock-screen message above. A missing bus name maps to "not a GNOME
   session".
6. No explicit teardown: mutter ends the sessions when the runner's D-Bus
   connection closes.

The exact D-Bus signatures are confirmed on the VM with `gdbus introspect --session
--dest org.gnome.Mutter.RemoteDesktop --object-path /org/gnome/Mutter/RemoteDesktop`
(and the ScreenCast equivalent) before step 3 is coded. Mutter's
`NotifyPointerButton` takes `(i, b)`, while the portal takes `(u)` state. Body
values from `dbus-fast` (`list[Any]`) are narrowed with `isinstance` at the
boundary, so no `Any` leaks.

**Screenshot size and coordinates (GNOME).** `screenshot()` returns the frame as
captured, in physical pixels. The backend remembers its size. `screen_size()`
returns that size, and grabs one frame first when none exists yet. Input
coordinates are passed unchanged. Coordinates outside the last frame raise
`ExecutionError` instead of letting mutter clamp or misplace them. This is the
only behaviour that differs from pyautogui, and only on Linux.

### Pages and forms
None. Server pages are unchanged.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| (none) | | | | | |

### Background work and WebSockets
- Wire protocol: unchanged. No new message type and no new `ClientCapability`.
  `REQUIRED_CLIENT_CAPABILITIES` is unchanged.
- Old controllers: an old Linux runner on Wayland keeps failing at import as it
  does today. Downloading the new ZIP fixes it. Old Windows and macOS runners are
  unaffected. The server needs nothing new from runners, so no gate is required.
- Errors reach the server as today: `EXECUTION_FAILED` for input, and
  `ScreenCaptureError` through the screenshot handler. A locked screen during a run
  therefore surfaces as a failed desktop step with an `ENVIRONMENT:`-style
  explanation, and the existing Need help flow takes over.
- Optional server change (Q4): in `agents/services/prompt_parts.py`, the Linux
  branches of `build_agent_persona` and `build_environment_context` describe the
  desktop from `system_info["desktop"]` and `["display_server"]` when present,
  instead of the hard-coded "Ubuntu 24.04 with XFCE4" and "display :0". The
  Windows and macOS branches are unchanged.

### Settings / infrastructure
- `controller_client/requirements.txt`: add `dbus-fast~=5.2.0; sys_platform !=
  "win32"`.
  - dbus-fast ships `py.typed` and cp313 and cp314 wheels for manylinux and macOS.
    On Windows it is sdist only.
  - It is installed on macOS too, so the developer's strict mypy run on the Mac
    type-checks the GNOME modules fully. Windows runners never import it.
  - PyAutoGUI and screeninfo stay installed on every platform (unused on Linux)
    so the requirement files and stubs stay simple. The Linux runner never imports
    them.
- No PyGObject and no GStreamer Python bindings. Frames come from the
  `gst-launch-1.0` CLI, so there are no new stubs.
- `controller_client/scripts/setup.sh` (Linux branch):
  - Replace `gnome-screenshot` with `gstreamer1.0-tools gstreamer1.0-pipewire
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good`, idempotent through
    `dpkg -s`. On non-apt systems, print a warning listing the same packages.
  - Print a warning when `XDG_SESSION_TYPE` is not `wayland` or
    `XDG_CURRENT_DESKTOP` lacks GNOME.
  - Print the commands for unattended machines without running them (Q3):
    `gsettings set org.gnome.desktop.screensaver lock-enabled false`,
    `gsettings set org.gnome.desktop.session idle-delay 0`,
    `gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'`,
    and autologin in `/etc/gdm3/custom.conf` (`AutomaticLoginEnable=true`,
    `AutomaticLogin=<user>`).
  - Finish by pointing at `python -m controller_client.diagnose`.
- No new env vars, so `example.env` is unchanged. Server `settings.py` is
  untouched.
- Server Docker image: imports only `controller_client/protocol.py`, which stays
  stdlib only. `scripts/verify_server_image.sh` needs no change. New files ship in
  the ZIP through the existing `rglob`.

### Data flow
- **Screenshot (GNOME).**
  1. The server sends `screenshot_request`.
  2. `client._handle_screenshot` calls `executor.execute_screenshot`, which calls
     `screen_capture.capture_screen()`, which calls `desktop_gnome.screenshot()`.
  3. `gnome_remote_session.ensure_started()` returns the PipeWire node id. This is
     a no-op once started.
  4. `pipewire_frame.grab_frame(node_id)` runs `gst-launch-1.0` and produces the
     PNG in a temp directory. The image is loaded into a `PIL.Image`.
  5. The size is recorded. Then PNG, then base64, as today.
- **Click (GNOME).**
  1. The server sends `click(x, y, button)` in screenshot pixels.
  2. `executor.execute_click` calls `ensure_input_not_blocked()` (a no-op), then
     `desktop_gnome.click()`.
  3. The bounds are checked against the last frame. Then `NotifyPointerMotionAbsolute`
     runs on the same stream, then button press and release.
  4. `ActionResultPayload` returns with today's message.
- **Startup.** `main.py` calls `verify_screen_capture()`, which captures once. On
  GNOME this starts the session at startup, so a locked screen or missing package
  is logged before the first case. Then the handshake: `gather_system_info()` adds
  `screen_size()` (the frame size) and `display_server`/`desktop`.
- **Windows and macOS.** The same calls land in `desktop_pyautogui`, which runs
  today's code.

### Unattended runs (Night mode) on GNOME
- No consent dialog, at first run or after restarts and reboots, because the Mutter
  API has no caller check.
- Requirements:
  - The user session is logged in, through GDM autologin after a reboot.
  - The screen is unlocked. Any lock, including after suspend, terminates the
    session and new ones are refused until someone unlocks.
  - The runner is started inside the desktop session (terminal or autostart), so
    launched apps inherit `WAYLAND_DISPLAY`. Mutter itself would accept an SSH
    caller on the same user bus, but apps launched from it would have no display,
    so diagnose warns.
  - Idle blanking and suspend are off. Synthetic input counts as activity, but the
    gaps between cases may not.
- Diagnose checks the first three and warns about the last.
- The GNOME top bar shows the remote-access indicator while the runner holds a
  session. It appears in screenshots and is harmless.

## Alternatives considered
- **xdg-desktop-portal RemoteDesktop + ScreenCast with `persist_mode=2`.**
  Documented and stable, works on KDE too, and uses almost the same method set
  (Notify* with stream-relative coordinates). Rejected as the default for these
  reasons:
  - A person must click the consent dialog once per machine.
  - The token is single use: if the runner dies between `Start` and saving the new
    token, the next start prompts again.
  - Restore after a reboot is unconfirmed on GNOME 50.
  - A rejected token falls back to a dialog that hangs an unattended run.
  The Mutter path has none of these. Because the portal mirrors Mutter's methods,
  a later `gnome_portal_session.py` behind the same `desktop_gnome` interface is a
  small, contained follow-up if GNOME ever adds a caller check (Q2).
- **Screenshot portal (`interactive=false`) for capture.** One "Allow Apps to Take
  Screenshots?" grant per machine, stored for all unsandboxed apps. It could be
  pre-seeded through the permission store, and needs no GStreamer. Not chosen:
  - It writes a file per shot.
  - It captures the whole stage rather than the stream that input coordinates are
    relative to. Multi-monitor or fractional-scale layouts could disagree with the
    input space.
  - It still needs a RemoteDesktop session for input.
  It is the fallback if `gst-launch` capture proves too slow or flaky on the VM.
- **In-process GStreamer through PyGObject** (a persistent `pipewiresrc ! appsink`
  pipeline, reading `last-sample`). Faster per shot, but PyGObject has no Linux
  wheels: it builds against `libgirepository-2.0-dev` and cairo, or needs
  `--system-site-packages`. It also needs `pygobject-stubs` coverage for Gst under
  strict mypy. Kept as the performance fallback only.
- **ydotool / uinput.** Root or udev setup and a daemon. Relative-only motion
  under pointer acceleration, so absolute positioning is unreliable. US keycodes,
  so other layouts break. No screenshots. Rejected.
- **libei through `ConnectToEIS`.** No typed Python binding on PyPI. The D-Bus
  Notify methods cover every action the runner has. Rejected.
- **Keep pyautogui on Linux with Xwayland.** Cannot see or drive native Wayland
  windows. Rejected by the request.
- **Runtime session detection (Wayland vs X11) on Linux.** Only needed if the X11
  fallback is kept (Q1). It would replace the static `sys.platform` dispatch with
  an `XDG_SESSION_TYPE`/`WAYLAND_DISPLAY` check in `desktop.py`. Both imports would
  still be top level.
- **No abstraction, just `if sys.platform` in every executor function.** Fewer
  files, but every desktop function would branch, and pyautogui would still need a
  guarded import in three modules. The single Protocol plus one dispatch module is
  the smallest structure that keeps pyautogui out of the Linux import graph.

## Risks and mitigations
- **Private Mutter API.** GNOME could add a caller check (as it did for the a11y
  manager) or change signatures. Mitigations:
  - All D-Bus code lives in one module.
  - Diagnose names the failing call.
  - The portal variant is the documented fallback (Q2).
  - Record the VM's `gnome-shell --version` in the change note.
- **Lock screen or login screen kills the session** (confirmed in gnome-shell and
  mutter source). Mitigation: the setup instructions, diagnose warnings, and a
  specific error message so Need help tells the person exactly what to do.
- **Security of an unlocked, auto-logged-in test machine.** That is a choice for
  the machine owner. The runner already executes arbitrary shell commands as that
  user, and the Mutter API grants nothing that user could not already do. The plan
  prints the settings rather than applying them (Q3).
- **Screenshot latency** from one `gst-launch-1.0` process per shot. Verified
  clicks take before and after shots. Measure on the VM; the target is under 1 s
  per shot at 1920x1080. If it is slower, switch `pipewire_frame` to the PyGObject
  persistent pipeline behind the same function.
- **`pipewiresrc` targeting.** `path=<node id>` is the legacy property, and
  `target-object` prefers an object serial. Mutter emits a node id. Confirm with
  `gst-inspect-1.0 pipewiresrc` on 26.04 which property accepts the node id, and
  fix the choice in `pipewire_frame`.
- **Silent keystroke loss.** A character missing from the active layout is dropped
  by mutter while the D-Bus call succeeds. pyautogui also skips unmappable
  characters, so this matches today. Document it in the change note. Clipboard
  paste for such characters is out of scope.
- **Windows and macOS regressions from moving code.** The move is mechanical, and
  the manual checks cover both platforms.
- **The Quartz stub** must still be found by mypy for `desktop_pyautogui.py`. It
  is, through `mypy_path`.
- **Python 3.14 on Ubuntu 26.04.** `Pillow~=11.2.1` has no cp314 wheel, so pip
  builds it from source. The user's VM evidently got through setup. Record
  `python3 --version`, and raise a separate bump if setup fails there.
- **Hyper-V enhanced session** (xrdp, or GNOME Remote Login) may not be a normal
  GNOME Wayland user session. Use the VM's basic console session, and confirm
  `XDG_SESSION_TYPE=wayland` in diagnose.
- **Thread affinity.** Desktop calls come from arbitrary `to_thread` workers. The
  private loop thread and lock in `gnome_remote_session` make this safe, and the
  per-call timeouts (5 s) keep a hung compositor from blocking the handler forever.

## Implementation steps

Parts A-F touch disjoint files and can run as parallel sub-agents once step 0 is in.

0. **Shared interfaces (orchestrator, first).** Files: `exceptions.py`
   (`DesktopSessionError`), `desktop_backend.py` (the Protocol above), `desktop.py`
   (dispatch, which imports `BACKEND` from the two backend modules), and
   `requirements.txt` (`dbus-fast`). Before parts B and D start, run `gdbus
   introspect` on the VM and paste the Mutter signatures into the sub-agent briefs.
1. **Part A: pyautogui backend (moved code).** Files: `desktop_pyautogui.py`,
   `screen_capture_macos.py`. Exposes `BACKEND: Final[DesktopBackend]`. Moves
   today's code: the executor pyautogui calls, the Quartz double-click,
   `_KEY_ALIASES` with `isValidKey`, the screenshot paths and the screeninfo size.
   `diagnostic_steps()` returns the one existing "screen capture" step.
2. **Part B: Mutter session client.** File: `gnome_remote_session.py`. Exposes the
   `GnomeRemoteSession` API under "Interfaces between parts".
3. **Part C: frames and keys.** Files: `pipewire_frame.py`, `keysyms.py`.
4. **Part D: GNOME backend and diagnostics.** Files: `desktop_gnome.py`,
   `gnome_diagnostics.py`. Builds against the B and C interfaces. Exposes
   `BACKEND: Final[DesktopBackend]`.
5. **Part E: callers.** Files: `executor.py`, `screen_capture.py`,
   `system_info.py`, `diagnose.py`, `protocol.py` (`CLIENT_VERSION`).
   `env_check.py` keeps working through `describe_screen_capture_failure`; only its
   docstring wording changes, if any.
6. **Part F: setup and prompt.** Files: `controller_client/scripts/setup.sh`, and
   `agents/services/prompt_parts.py` if Q4 is yes.
7. **Integration (orchestrator).**
   - Check that `desktop.py` resolves both backends.
   - Check that no module except `desktop_pyautogui.py` imports `pyautogui`,
     `screeninfo` or `Quartz` (`grep`).
   - Run `pre-commit run --all-files`, then on the Mac
     `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg
     --platform linux controller_client/` so the Linux branch of `desktop.py` is
     also checked.
   - Run the VM checklist.
   - Write `docs/changed/2026-10-28-wayland-controller.md`.

**Interfaces between parts** (signatures only):
- `gnome_remote_session`:
  - `StreamInfo` (frozen dataclass: `node_id: int`, `stream_path: str`)
  - `class GnomeRemoteSession` with:
    - `ensure_started() -> StreamInfo`
    - `pointer_move(x: float, y: float, *, stream_path: str | None = None) -> None`
    - `pointer_button(button_code: int, pressed: bool, *, stream_path: str | None = None) -> None`
    - `key(keysym: int, pressed: bool, *, stream_path: str | None = None) -> None`
    - `active_stream_path() -> str | None` (no start or rebuild)
    - `probe() -> str`: a diagnose summary (bus names present, session started).
      Raises `DesktopSessionError`.
  - Module constants `BTN_LEFT`, `BTN_RIGHT`, `BTN_MIDDLE`.
- `pipewire_frame`:
  - `grab_frame(node_id: int, timeout_s: float) -> Image.Image`: raises
    `ScreenCaptureError`.
  - `missing_capture_tools() -> str | None`
- `keysyms`:
  - `resolve_key_name(name: str) -> str | None`: applies aliases and validates.
  - `key_name_to_keysym(name: str) -> int`: for names `resolve_key_name` accepted.
  - `char_to_keysym(char: str) -> int`

## Verification

Type checks:
- `pre-commit run --all-files` is clean on the Mac.
- `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg
  controller_client/` is clean, with and without `--platform linux`, and also on
  the Ubuntu VM.
- `grep -rn "import pyautogui\|from screeninfo\|import Quartz" controller_client/
  --include='*.py'` matches only `desktop_pyautogui.py` and `screen_capture_macos.py`
  (Quartz and AppKit also stay in `app_closer_macos.py`).

Ubuntu 26.04 VM (basic console session, GNOME Wayland):
1. Record `echo $XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP`, `gnome-shell --version`,
   `python3 --version`, `gst-launch-1.0 --version` and `pipewire --version`.
2. Run `scripts/setup.sh`. The GStreamer packages install, the gnome-screenshot
   step is gone, and the unattended-machine advice is printed.
3. `python -m controller_client.diagnose`: every step is `[ok]`. Steps, in order:
   - Wayland GNOME session.
   - Started inside the desktop session (`WAYLAND_DISPLAY` set).
   - Mutter RemoteDesktop/ScreenCast present.
   - GStreamer PipeWire tools.
   - Session start, reporting the primary monitor connector.
   - One frame, reporting its size and time in ms.
   - Lock screen and idle settings (shown as a warning text if enabled).
4. Failure messages:
   - Lock the screen (Super+L) and run diagnose from SSH (`ssh vm 'cd ...;
     .venv/bin/python -m controller_client.diagnose'`). It reports the locked
     screen and outside-session messages, and none crash.
   - Remove `gstreamer1.0-pipewire`: the tools step names the package.
     Reinstall it.
5. Start the runner and connect it to a project. The project overview shows the
   system info with the screen size equal to the screenshot size, plus
   `display_server: wayland`.
6. Through a test case or Machine setup chat:
   - A screenshot shows native Wayland windows (Files, Settings, Text Editor).
   - Click the "Activities" corner and an app's button.
   - Double-click a file in Files.
   - Hover shows a tooltip.
   - Drag a file into a folder.
   - `type_text` "Hello World 123!".
   - `key_press` `ctrl+a`, `ctrl+c`, `super`, `alt+F4`, `ctrl+shift+t`, and a lone
     `A`.
   - An unknown key returns "Unknown key".
   - `launch_app` "Text Editor".
   - Browser tools in chromium and firefox (`diagnose --browsers`).
7. HiDPI: set scaling to 200%, then to 125% (fractional, after enabling the
   experimental setting). Repeat a click on a small button. It lands on the
   element.
8. Lock mid-run: the step fails with the lock-screen message, and Need help opens.
   After unlocking, Continue works because the session rebuilds without a runner
   restart.
9. Reboot with autologin and lock disabled. Start the runner. A run proceeds with
   no dialog ever shown.
10. Latency: log the median screenshot time over 20 shots, and record it in the
    change note.

Regression checks:
- Windows: diagnose output is unchanged. Run a case with click, double-click,
  drag, type, key combos, and an elevated window (UIPI message unchanged).
- macOS: diagnose, then a case with double-click in Finder and `cmd+n`. The
  Screen Recording permission message is unchanged.
- Server: run `scripts/verify_server_image.sh`.

Then run the `reviewer` and `verifier` agents in parallel on the diff (verifier
gets this plan and the request).

## Open questions
- **Q1. Keep an X11/pyautogui fallback on Linux, or make Linux GNOME-Wayland-only?**
  The fallback would cover Xorg sessions, older Ubuntu or other desktops, XFCE, VNC
  and Docker machines. *Assumption used in this draft: Wayland-only, as the user
  said. Linux always uses the GNOME backend, and Xorg or non-GNOME desktops get a
  clear "needs a GNOME Wayland session" error. Keeping the fallback would add
  runtime session detection to `desktop.py` and keep `gnome-screenshot` in
  setup.sh.*
- **Q2. Is the private Mutter D-Bus API acceptable, or do you prefer the
  documented portal?** Mutter needs no consent, ever. It is unofficial and could
  change in a future GNOME. The portal needs one consent click per machine, and a
  lost token re-prompts and would hang an unattended run. *Assumption: Mutter API
  now; the portal only as a later fallback.*
- **Q3. Should `setup.sh` apply the unattended settings** (disable lock screen,
  blanking and suspend, enable GDM autologin with sudo), or only print them?
  *Assumption: print only. They lower the machine's security and are the owner's
  call.*
- **Q4. Update the agent's Linux environment prompt**
  (`agents/services/prompt_parts.py` still says "Ubuntu 24.04 with XFCE4 on display
  :0") to describe the reported desktop? *Assumption: yes, Linux branch only, using
  the new `system_info` keys with a generic fallback.*
- **Q5. Add snap and flatpak `.desktop` directories to Linux app discovery?**
  Ubuntu's Firefox, Thunderbird and others are snaps, so `launch_app` cannot find
  them today. *Assumption: out of scope; separate small fix plan.*
- **Q6. Should the runner start automatically after login** (a `~/.config/autostart`
  entry or a `systemd --user` unit tied to `graphical-session.target`) so Night runs
  survive a reboot? *Assumption: out of scope; the runner is started manually as on
  Windows and macOS.*
- **Q7. Is primary-monitor-only acceptable on Linux?** *Assumption: yes, matching
  Windows.*

## References
- RemoteDesktop portal (persist_mode, restore_token, Notify*, ConnectToEIS): https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html
- ScreenCast portal: https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html
- Portal persistence PR (frontend): https://github.com/flatpak/xdg-desktop-portal/pull/1004
- GNOME 50 persistence field report: https://github.com/agent-sh/computer-use-linux/issues/185
- Mutter API is private: https://github.com/GNOME/gnome-remote-desktop/blob/master/src/org.gnome.Mutter.RemoteDesktop.xml
- gnome-ponytail-daemon (dogtail on Wayland through Mutter APIs): https://tracker.debian.org/media/packages/g/gnome-ponytail-daemon/control-0.0.11-2
- Direct Mutter backend tested on GNOME Shell 50.4: https://github.com/rustdesk/rustdesk/pull/15988
- Source read for this plan (cloned 2026-10-28): mutter master `src/backends/meta-remote-desktop.c`, `meta-screen-cast.c`, `meta-dbus-session-manager.c`, `meta-stream-monitor.c`, `meta-stream-source-monitor.c`, `meta-screen-cast-stream.c`, `native/meta-virtual-input-device-native.c`, `meta-dbus-access-checker.c`; gnome-shell `js/ui/main.js`, `js/ui/sessionMode.js`, `js/ui/screenshot.js`, `js/ui/shellDBus.js`; xdg-desktop-portal `desktop-portal/screenshot.c`.
- pipewiresrc `target-object` vs `path`: https://gitlab.freedesktop.org/pipewire/pipewire/-/issues/1497, https://invent.kde.org/plasma/krdp/-/merge_requests/222
- Ubuntu 26.04 Wayland-only, GNOME 50: https://www.omgubuntu.co.uk/2026/04/ubuntu-26-04-lts-released
- dbus-fast 5.2.0 (py.typed, cp313/cp314 wheels): https://pypi.org/project/dbus-fast/

## Changelog
- 2026-10-28: created (Draft).
- 2026-10-28: user accepted the default answers to Q1-Q7 (Wayland-only Linux, Mutter API, setup prints unattended settings, prompt updated, no snap/flatpak, no autostart, primary monitor only). Status In progress. Mutter signatures confirmed against mutter master source (`meta-remote-desktop-session.c`, `meta-screen-cast-session.c`); still to confirm on the VM with `gdbus introspect`.
- 2026-10-28: review round 1 fixes. `gnome_remote_session`: sessions it gives up on are stopped (`RemoteDesktop.Session.Stop`) unless mutter sent `Closed`; a failed start stops the created session and removes its match rules (shielded from cancellation); every D-Bus call has its own 5 s timeout and the outer budgets are larger; a call timeout no longer marks the session closed; the bus address is no longer put in error text. `desktop_gnome`: releases in `finally` are skipped when the session is dead; the cached frame size is tied to the stream path, so a rebuilt session re-measures; `screen_size()` never captures (handshake must not wait on a locked screen; the startup capture check fills it). `keysyms`: `\r` types Return like pyautogui; stale doc references fixed. `main.py` logs the backend name. Rejected: the diagnose session step reports the PipeWire node id rather than the monitor connector (the empty connector means primary, and reading it needs another call for no diagnostic gain); the `--help` description "and drive input" is kept, as diagnose now checks input on Linux; `screen_size()` not raising on Windows/macOS is the Protocol's intent (the handshake must succeed so browser tools work); the 40-character cap on runner-reported desktop strings is accepted (an authenticated runner already runs the agent's commands); `get_os_name` not sanitising `os` predates this change.
- 2026-10-28: review round 2 fixes. Every multi-event action is bound to the session it started on (`stream_path=` on `pointer_move`/`pointer_button`/`key`, `GnomeRemoteSession._session_for`): if mutter closed it meanwhile the action fails with a clear message instead of finishing on a rebuilt session without the held button or keys. Releases are queued before each press and sent in `finally` (click, double-click, drag, typing, hotkeys) because a press whose reply timed out may still have reached mutter; they are skipped when the session is gone (`active_stream_path()`). Budgets: a rebuild adds Stop + 3 RemoveMatch, the old session's drop is shielded, the service lookup covers connect + 2 calls, and the bus connect has its own timeout. A start that finishes just as the caller gives up is stopped (`_stop_orphan`) instead of leaking.
- 2026-10-28: review round 3 fixes. Releases: every key is released even when one release fails, and a release failure after a failed press is logged instead of replacing the press error (`_then_release`, `quiet`). A start that finishes as the caller gives up is settled through `_StartHandoff` under a lock shared with the loop thread: handed to the caller if already delivered, otherwise stopped on the loop side; `_stop_orphan` removed. Session drops run as referenced tasks behind `asyncio.shield` (`_shielded_drop`). Start budget counts the bus connect and the failed-start cleanup; a failed bus connect is disconnected. `drag` checks both points against the same stream; the frame size and its stream are recorded together (`_capture`). Behaviour simulated with a fake session on the Mac (click, hotkey, timed-out press and release, bounds, session closed mid-drag, typing `\r`).
- 2026-10-28: review round 4 clean (reviewer approve, verifier verified). Declined optional notes: a malformed `DBUS_SESSION_BUS_ADDRESS` surfaces dbus-fast's own address error rather than the "start from the desktop session" hint (still reported, never crashes; a hand-broken environment); a `CreateSession` that answers after its timeout leaves an unstarted mutter session until the bus closes (no device or stream exists). Status Done; VM checklist still to run by the user.
