"""Window capture on wlroots compositors: Sway and Hyprland.

Two platform paths in one module (``capture_platform.py``):

* Sway: ``swaymsg -t get_tree -r`` (windows), ``-t get_workspaces -r``
  (visible and focused workspaces) and ``-t get_outputs -r`` (monitors);
  switched with ``swaymsg 'workspace --no-auto-back-and-forth "<name>"'``.
* Hyprland: ``hyprctl -j clients|monitors|workspaces``; switched with
  ``hyprctl dispatch workspace <id>`` and restored per monitor, then
  ``hyprctl dispatch focusmonitor <name>``.
* Both capture with ``grim -g "x,y wxh" -t png -`` (wlr-screencopy) of the
  visible rect, in layout coordinates. There is no faithful window-only
  capture, so a covered window is ``window_obscured``.

Only a window with a compositor-reported pid is ``capturable``; its process
is the pid's name. A window without a pid (its app id or class is chosen by
the client itself) is listed as not capturable, so it is never selected but
still counts for the obscured check and the screen rule. A sway view on a
shown workspace that is not drawn (a background tab) is listed with
``drawn=False``. Layer-shell surfaces (bars, notifications) are not in the
compositors' window lists.

Every tool runs with a fixed argv, no shell and a timeout; a missing tool is
``helper_missing`` naming its package. JSON is parsed defensively (anything
unexpected is skipped, never trusted). Importing does no I/O.
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from PIL import Image

from controller_client.capture_platform import (
    WindowGrabBackend,
    WindowInventoryBackend,
)
from controller_client.desktop_switch import (
    DesktopSwitcher,
    DesktopSwitchError,
    poll_until,
)
from controller_client.process_names import ProcessNames
from controller_client.protocol import (
    CAPTURE_WINDOW_ID_MAX_CHARS,
    CaptureStatus,
)
from controller_client.window_capture_policy import unobscured_by_stacking
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
    Rect,
    WindowDesktop,
)

logger = logging.getLogger(__name__)

# Longest run of one compositor tool.
_TOOL_TIMEOUT_SECONDS: Final[float] = 5.0
# Deepest container nesting followed in the sway tree.
_SWAY_TREE_MAX_DEPTH: Final[int] = 64
# Sway's scratchpad output / workspace (windows there are "minimised").
_SWAY_SCRATCH_OUTPUT: Final[str] = "__i3"
_SWAY_SCRATCH_WORKSPACE: Final[str] = "__i3_scratch"
# A monitor name passed to ``hyprctl dispatch focusmonitor``.
_MONITOR_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:-]{1,64}")
# Characters refused in a sway workspace name sent back to sway.
_CONTROL_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

JsonObject = dict[str, object]


@dataclass(frozen=True)
class _Tool:
    """One external tool and the package that provides it."""

    executable: str
    package: str


_SWAYMSG: Final[_Tool] = _Tool("swaymsg", "sway")
_HYPRCTL: Final[_Tool] = _Tool("hyprctl", "hyprland")
_GRIM: Final[_Tool] = _Tool("grim", "grim")


class _ToolFailedError(Exception):
    """A tool ran but failed, timed out or answered unreadable output;
    ``detail`` is user-safe."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --- running tools ---------------------------------------------------------


def _run(tool: _Tool, args: Sequence[str]) -> bytes:
    """Run ``tool`` with ``args`` (no shell) and return its stdout.

    Raises ``CaptureUnavailableError(helper_missing)`` when the tool is not
    installed and ``_ToolFailedError`` when it fails or times out.
    """
    argv = [tool.executable, *args]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=_TOOL_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise CaptureUnavailableError(
            CaptureStatus.HELPER_MISSING,
            f"{tool.executable} is not installed; install the {tool.package} "
            "package and restart the Test Runner",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _ToolFailedError(f"{tool.executable} did not answer in time") from exc
    except OSError as exc:
        raise _ToolFailedError(f"{tool.executable} could not be started") from exc
    if completed.returncode != 0:
        raise _ToolFailedError(
            f"{tool.executable} failed (exit code {completed.returncode})"
        )
    return completed.stdout


def _run_json(tool: _Tool, args: Sequence[str]) -> object:
    raw = _run(tool, args)
    try:
        parsed: object = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _ToolFailedError(f"{tool.executable} returned unreadable output") from exc
    return parsed


def _inventory_error(exc: _ToolFailedError) -> CaptureUnavailableError:
    return CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, exc.detail)


# --- defensive JSON access -------------------------------------------------


def _as_object(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _as_objects(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [obj for obj in (_as_object(item) for item in value) if obj is not None]


def _get_object(obj: JsonObject, key: str) -> JsonObject | None:
    return _as_object(obj.get(key))


def _get_int(obj: JsonObject, key: str) -> int | None:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _get_number(obj: JsonObject, key: str) -> float | None:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _get_str(obj: JsonObject, key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _get_bool(obj: JsonObject, key: str) -> bool:
    return obj.get(key) is True


def _get_pair(obj: JsonObject, key: str) -> tuple[int, int] | None:
    value = obj.get(key)
    if not isinstance(value, list) or len(value) != 2:
        return None
    first: object = value[0]
    second: object = value[1]
    if (
        isinstance(first, bool)
        or isinstance(second, bool)
        or not isinstance(first, int)
        or not isinstance(second, int)
    ):
        return None
    return first, second


def _rect_of(obj: JsonObject | None) -> Rect | None:
    if obj is None:
        return None
    x = _get_int(obj, "x")
    y = _get_int(obj, "y")
    width = _get_int(obj, "width")
    height = _get_int(obj, "height")
    if x is None or y is None or width is None or height is None:
        return None
    return Rect(x, y, width, height)


# --- shared helpers ---------------------------------------------------------


def _valid_window_id(value: str) -> bool:
    return 0 < len(value) <= CAPTURE_WINDOW_ID_MAX_CHARS


def _grim_grab(rect: Rect) -> Image.Image:
    if rect.is_empty:
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, "the capture area is empty"
        )
    geometry = f"{rect.x},{rect.y} {rect.width}x{rect.height}"
    try:
        raw = _run(_GRIM, ["-g", geometry, "-t", "png", "-"])
    except _ToolFailedError as exc:
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, "grim could not capture the area"
        ) from exc
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            rgb: Image.Image = image.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, "grim returned an unreadable image"
        ) from exc
    return rgb


def _largest_overlap(window: DesktopWindow, monitors: Sequence[Rect]) -> Rect | None:
    best: Rect | None = None
    best_area = 0
    for monitor in monitors:
        area = window.rect.intersection(monitor).area
        if area > best_area:
            best, best_area = monitor, area
    return best


class _GrimGrab:
    """``grim`` grabs of the visible rect; no window-only path."""

    def __init__(
        self,
        monitors: Callable[[], tuple[list[Rect], Rect | None]],
        compositor: str,
    ) -> None:
        self._monitors = monitors
        self._compositor = compositor

    def grab_rect(self, rect: Rect) -> Image.Image:
        return _grim_grab(rect)

    def grab_window_only(self, window: DesktopWindow) -> Image.Image:
        raise CaptureUnavailableError(
            CaptureStatus.WINDOW_OBSCURED,
            "the window is covered and this compositor has no window-only "
            "capture; move the covering window away and try again",
        )

    def is_unobscured(
        self, window: DesktopWindow, windows: Sequence[DesktopWindow]
    ) -> bool:
        return unobscured_by_stacking(window, windows)

    def monitor_rect(self, window: DesktopWindow) -> Rect:
        try:
            monitors, focused = self._monitors()
        except _ToolFailedError as exc:
            raise _inventory_error(exc) from exc
        best = _largest_overlap(window, monitors)
        if best is not None:
            return best
        if focused is not None:
            return focused
        if monitors:
            return monitors[0]
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, f"{self._compositor} reports no monitor"
        )


def _restore_failed(platform: str, reason: str) -> bool:
    logger.warning("%s workspace restore failed: %s", platform, reason)
    return False


# --- Sway ------------------------------------------------------------------


@dataclass(frozen=True)
class _SwayWorkspace:
    name: str
    output: str
    visible: bool
    focused: bool


def _sway_workspaces() -> list[_SwayWorkspace]:
    result: list[_SwayWorkspace] = []
    for obj in _as_objects(_run_json(_SWAYMSG, ["-t", "get_workspaces", "-r"])):
        name = _get_str(obj, "name")
        if not name or name == _SWAY_SCRATCH_WORKSPACE:
            continue
        result.append(
            _SwayWorkspace(
                name=name,
                output=_get_str(obj, "output") or "",
                visible=_get_bool(obj, "visible"),
                focused=_get_bool(obj, "focused"),
            )
        )
    return result


@dataclass(frozen=True)
class _SwayView:
    con_id: int
    pid: int | None
    title: str
    rect: Rect
    focused: bool
    visible: bool
    fullscreen: bool
    floating: bool
    # 0 for the topmost floating window of its workspace.
    floating_rank: int
    workspace: str | None
    scratch: bool
    order: int


class _SwayTreeWalker:
    def __init__(self) -> None:
        self.views: list[_SwayView] = []

    def walk(
        self,
        node: JsonObject,
        *,
        workspace: str | None,
        scratch: bool,
        floating: bool,
        floating_rank: int,
        depth: int,
    ) -> None:
        if depth > _SWAY_TREE_MAX_DEPTH:
            return
        node_type = _get_str(node, "type")
        if node_type == "output":
            scratch = _get_str(node, "name") == _SWAY_SCRATCH_OUTPUT
        elif node_type == "workspace":
            workspace = _get_str(node, "name")
            scratch = scratch or workspace == _SWAY_SCRATCH_WORKSPACE
        children = _as_objects(node.get("nodes"))
        floating_children = _as_objects(node.get("floating_nodes"))
        if (
            node_type in ("con", "floating_con")
            and not children
            and not floating_children
        ):
            self._add_view(node, workspace, scratch, floating, floating_rank)
            return
        for child in children:
            self.walk(
                child,
                workspace=workspace,
                scratch=scratch,
                floating=floating,
                floating_rank=floating_rank,
                depth=depth + 1,
            )
        count = len(floating_children)
        for position, child in enumerate(floating_children):
            # Sway draws floating containers in list order: the last is on top.
            self.walk(
                child,
                workspace=workspace,
                scratch=scratch,
                floating=True,
                floating_rank=count - 1 - position,
                depth=depth + 1,
            )

    def _add_view(
        self,
        node: JsonObject,
        workspace: str | None,
        scratch: bool,
        floating: bool,
        floating_rank: int,
    ) -> None:
        con_id = _get_int(node, "id")
        rect = _rect_of(_get_object(node, "rect"))
        pid = _get_int(node, "pid")
        app_id = _get_str(node, "app_id")
        x_window = _get_int(node, "window")
        if con_id is None or rect is None or rect.is_empty:
            return
        if pid is None and not app_id and x_window is None:
            # An empty split container, not a view.
            return
        fullscreen_mode = _get_int(node, "fullscreen_mode") or 0
        self.views.append(
            _SwayView(
                con_id=con_id,
                pid=pid,
                title=_get_str(node, "name") or "",
                rect=rect,
                focused=_get_bool(node, "focused"),
                visible=_get_bool(node, "visible"),
                fullscreen=fullscreen_mode > 0,
                floating=floating or _get_str(node, "type") == "floating_con",
                floating_rank=floating_rank,
                workspace=workspace,
                scratch=scratch,
                order=len(self.views),
            )
        )


class _SwayInventory:
    def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
        try:
            workspaces = _sway_workspaces()
            tree = _as_object(_run_json(_SWAYMSG, ["-t", "get_tree", "-r"]))
        except _ToolFailedError as exc:
            raise _inventory_error(exc) from exc
        if tree is None:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, "swaymsg returned an unexpected tree"
            )
        walker = _SwayTreeWalker()
        walker.walk(
            tree,
            workspace=None,
            scratch=False,
            floating=False,
            floating_rank=0,
            depth=0,
        )
        names = tuple(workspace.name for workspace in workspaces)
        index_of = {name: position + 1 for position, name in enumerate(names)}
        visible = {workspace.name for workspace in workspaces if workspace.visible}
        focused_ws = next((ws.name for ws in workspaces if ws.focused), None)
        info = DesktopInfo(
            current=index_of.get(focused_ws) if focused_ws is not None else None,
            count=len(names),
            names=names,
        )

        def is_current(view: _SwayView) -> bool:
            return (
                not view.scratch
                and view.workspace is not None
                and view.workspace in visible
            )

        def sort_key(view: _SwayView) -> tuple[int, int, int, int, int, int, int]:
            if not is_current(view):
                return (1, 0, 0, 0, 0, 0, view.order)
            return (
                0,
                0 if view.fullscreen else 1,
                0 if view.floating else 1,
                view.floating_rank if view.floating else 0,
                0 if view.visible else 1,
                0 if view.focused else 1,
                view.order,
            )

        windows: list[DesktopWindow] = []
        processes = ProcessNames()
        for z, view in enumerate(sorted(walker.views, key=sort_key)):
            window_id = str(view.con_id)
            if not _valid_window_id(window_id):
                continue
            current = is_current(view)
            pid = view.pid if view.pid is not None and view.pid > 0 else None
            workspace_name = None if view.scratch else view.workspace
            windows.append(
                DesktopWindow(
                    window_id=window_id,
                    pid=pid,
                    process=processes.name(pid),
                    title=view.title,
                    rect=view.rect,
                    minimized=view.scratch,
                    focused=view.focused,
                    z=z,
                    desktop=WindowDesktop(
                        current=current,
                        index=(
                            index_of.get(workspace_name)
                            if workspace_name is not None
                            else None
                        ),
                        name=workspace_name,
                    ),
                    capturable=pid is not None,
                    # A background tab of a shown workspace: listed, not drawn.
                    drawn=not current or view.visible,
                )
            )
        return info, windows


def _sway_monitors() -> tuple[list[Rect], Rect | None]:
    """The active outputs' rects (layout coordinates) and the focused one."""
    monitors: list[Rect] = []
    focused: Rect | None = None
    for output in _as_objects(_run_json(_SWAYMSG, ["-t", "get_outputs", "-r"])):
        if output.get("active") is False:
            continue
        rect = _rect_of(_get_object(output, "rect"))
        if rect is None or rect.is_empty:
            continue
        monitors.append(rect)
        if focused is None and _get_bool(output, "focused"):
            focused = rect
    return monitors, focused


def _sway_quote(value: str) -> str:
    """A sway command argument in double quotes, escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sway_command(command: str) -> None:
    """Run one sway command line; raises ``_ToolFailedError`` unless every
    command in it reports success."""
    results = _as_objects(_run_json(_SWAYMSG, [command]))
    if not results or not all(_get_bool(result, "success") for result in results):
        raise _ToolFailedError("sway refused the workspace change")


class _SwaySwitcher:
    def __init__(self) -> None:
        self._changed = False
        self._recorded: list[_SwayWorkspace] = []

    @property
    def changed(self) -> bool:
        return self._changed

    def switch_to(self, window: DesktopWindow) -> None:
        if window.desktop.current:
            return
        target = window.desktop.name
        if not target or _CONTROL_CHARS_RE.search(target):
            raise DesktopSwitchError("the window's workspace is unknown")
        try:
            self._recorded = [ws for ws in _sway_workspaces() if ws.visible]
        except (_ToolFailedError, CaptureUnavailableError) as exc:
            raise DesktopSwitchError(_switch_detail(exc)) from exc
        if not self._recorded:
            raise DesktopSwitchError("sway reports no visible workspace")
        self._changed = True
        try:
            _sway_command(f"workspace --no-auto-back-and-forth {_sway_quote(target)}")
        except (_ToolFailedError, CaptureUnavailableError) as exc:
            raise DesktopSwitchError(_switch_detail(exc)) from exc

        def target_visible() -> bool:
            try:
                return any(
                    ws.name == target and ws.visible for ws in _sway_workspaces()
                )
            except (_ToolFailedError, CaptureUnavailableError):
                return False

        if not poll_until(target_visible):
            raise DesktopSwitchError("sway did not show the window's workspace")

    def restore(self) -> bool:
        if not self._changed:
            return True
        try:
            recorded = self._recorded
            focused = next((ws for ws in recorded if ws.focused), None)
            ordered = [ws for ws in recorded if ws is not focused]
            if focused is not None:
                ordered.append(focused)
            for workspace in ordered:
                if _CONTROL_CHARS_RE.search(workspace.name) or _CONTROL_CHARS_RE.search(
                    workspace.output
                ):
                    continue
                # Focus the output first so a workspace sway destroyed while
                # empty is created again on its own output.
                command = (
                    f"workspace --no-auto-back-and-forth {_sway_quote(workspace.name)}"
                )
                if workspace.output:
                    command = f"focus output {_sway_quote(workspace.output)}; {command}"
                _sway_command(command)

            def restored() -> bool:
                try:
                    now = _sway_workspaces()
                except (_ToolFailedError, CaptureUnavailableError):
                    return False
                shown = {(ws.output, ws.name) for ws in now if ws.visible}
                if any((ws.output, ws.name) not in shown for ws in recorded):
                    return False
                if focused is None:
                    return True
                return any(ws.name == focused.name and ws.focused for ws in now)

            if poll_until(restored):
                return True
            return _restore_failed("sway", "not verified in time")
        except (_ToolFailedError, CaptureUnavailableError) as exc:
            return _restore_failed("sway", _switch_detail(exc))
        except Exception:  # never raise from restore (desktop_switch contract)
            logger.warning("sway workspace restore failed", exc_info=True)
            return False


def _switch_detail(exc: _ToolFailedError | CaptureUnavailableError) -> str:
    return exc.detail


def create_sway_switcher(
    info: DesktopInfo, windows: Sequence[DesktopWindow]
) -> DesktopSwitcher:
    """A switcher for one capture on Sway (``desktop_switch.SwitcherFactory``);
    it records the visible workspaces itself when switching."""
    return _SwaySwitcher()


# --- Hyprland --------------------------------------------------------------


@dataclass(frozen=True)
class _HyprMonitor:
    name: str
    focused: bool
    workspace_id: int | None
    special_id: int | None
    rect: Rect | None


def _hypr_monitor_rect(obj: JsonObject) -> Rect | None:
    """The monitor in layout coordinates: pixel size / scale, swapped for a
    90 or 270 degree transform."""
    x = _get_int(obj, "x")
    y = _get_int(obj, "y")
    width = _get_int(obj, "width")
    height = _get_int(obj, "height")
    scale = _get_number(obj, "scale") or 1.0
    if x is None or y is None or width is None or height is None or scale <= 0:
        return None
    logical_width = round(width / scale)
    logical_height = round(height / scale)
    if (_get_int(obj, "transform") or 0) % 2 == 1:
        logical_width, logical_height = logical_height, logical_width
    return Rect(x, y, logical_width, logical_height)


def _hypr_monitors() -> list[_HyprMonitor]:
    result: list[_HyprMonitor] = []
    for obj in _as_objects(_run_json(_HYPRCTL, ["-j", "monitors"])):
        name = _get_str(obj, "name")
        if not name or obj.get("disabled") is True:
            continue
        active = _get_object(obj, "activeWorkspace")
        special = _get_object(obj, "specialWorkspace")
        special_id = _get_int(special, "id") if special is not None else None
        result.append(
            _HyprMonitor(
                name=name,
                focused=_get_bool(obj, "focused"),
                workspace_id=_get_int(active, "id") if active is not None else None,
                special_id=special_id if special_id else None,
                rect=_hypr_monitor_rect(obj),
            )
        )
    return result


@dataclass(frozen=True)
class _HyprClient:
    address: str
    pid: int | None
    title: str
    rect: Rect
    workspace_id: int | None
    workspace_name: str | None
    hidden: bool
    floating: bool
    pinned: bool
    fullscreen: bool
    focus_history: int
    order: int


def _hypr_clients() -> list[_HyprClient]:
    result: list[_HyprClient] = []
    for obj in _as_objects(_run_json(_HYPRCTL, ["-j", "clients"])):
        address = _get_str(obj, "address")
        at = _get_pair(obj, "at")
        size = _get_pair(obj, "size")
        if not address or at is None or size is None:
            continue
        if obj.get("mapped") is False:
            continue
        rect = Rect(at[0], at[1], size[0], size[1])
        if rect.is_empty:
            continue
        workspace = _get_object(obj, "workspace")
        fullscreen_value = obj.get("fullscreen")
        fullscreen = fullscreen_value is True or (
            isinstance(fullscreen_value, int)
            and not isinstance(fullscreen_value, bool)
            and fullscreen_value > 0
        )
        history = _get_int(obj, "focusHistoryID")
        result.append(
            _HyprClient(
                address=address,
                pid=_get_int(obj, "pid"),
                title=_get_str(obj, "title") or "",
                rect=rect,
                workspace_id=(
                    _get_int(workspace, "id") if workspace is not None else None
                ),
                workspace_name=(
                    _get_str(workspace, "name") if workspace is not None else None
                ),
                hidden=_get_bool(obj, "hidden"),
                floating=_get_bool(obj, "floating"),
                pinned=_get_bool(obj, "pinned"),
                fullscreen=fullscreen,
                focus_history=(
                    history if history is not None and history >= 0 else 1_000_000
                ),
                order=len(result),
            )
        )
    return result


class _HyprlandInventory:
    def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
        try:
            monitors = _hypr_monitors()
            clients = _hypr_clients()
            workspace_objs = _as_objects(_run_json(_HYPRCTL, ["-j", "workspaces"]))
        except _ToolFailedError as exc:
            raise _inventory_error(exc) from exc
        workspaces: list[tuple[int, str]] = []
        for obj in workspace_objs:
            ws_id = _get_int(obj, "id")
            if ws_id is None or ws_id <= 0:
                # Special workspaces (scratchpads) have negative ids.
                continue
            workspaces.append((ws_id, _get_str(obj, "name") or str(ws_id)))
        workspaces.sort()
        index_of = {
            ws_id: position + 1 for position, (ws_id, _) in enumerate(workspaces)
        }
        names = tuple(name for _, name in workspaces)
        active_ids = {m.workspace_id for m in monitors if m.workspace_id is not None}
        shown_special = {m.special_id for m in monitors if m.special_id is not None}
        focused_monitor = next((m for m in monitors if m.focused), None)
        focused_ws = focused_monitor.workspace_id if focused_monitor else None
        info = DesktopInfo(
            current=index_of.get(focused_ws) if focused_ws is not None else None,
            count=len(workspaces),
            names=names,
        )

        def is_special(client: _HyprClient) -> bool:
            return client.workspace_id is not None and client.workspace_id < 0

        def is_current(client: _HyprClient) -> bool:
            if client.hidden:
                return False
            if client.pinned:
                return True
            if is_special(client):
                return client.workspace_id in shown_special
            return client.workspace_id in active_ids

        def minimized(client: _HyprClient) -> bool:
            return client.hidden or (
                is_special(client) and client.workspace_id not in shown_special
            )

        def sort_key(client: _HyprClient) -> tuple[int, int, int, int, int]:
            if not is_current(client):
                return (1, 0, 0, client.focus_history, client.order)
            return (
                0,
                # A shown special workspace and pinned windows are drawn on top.
                0 if is_special(client) or client.pinned else 1,
                0 if client.fullscreen else (1 if client.floating else 2),
                client.focus_history,
                client.order,
            )

        windows: list[DesktopWindow] = []
        processes = ProcessNames()
        for z, client in enumerate(sorted(clients, key=sort_key)):
            if not _valid_window_id(client.address):
                continue
            special = is_special(client)
            current = is_current(client)
            pid = client.pid if client.pid is not None and client.pid > 0 else None
            windows.append(
                DesktopWindow(
                    window_id=client.address,
                    pid=pid,
                    process=processes.name(pid),
                    title=client.title,
                    rect=client.rect,
                    minimized=minimized(client),
                    focused=(
                        client.focus_history == 0
                        and current
                        and (client.workspace_id == focused_ws or client.pinned)
                    ),
                    z=z,
                    desktop=WindowDesktop(
                        current=current,
                        index=(
                            None
                            if special or client.workspace_id is None
                            else index_of.get(client.workspace_id)
                        ),
                        name=client.workspace_name,
                    ),
                    capturable=pid is not None,
                )
            )
        return info, windows


def _hyprland_monitors() -> tuple[list[Rect], Rect | None]:
    """The monitors' rects (layout coordinates) and the focused one."""
    monitors = _hypr_monitors()
    rects = [m.rect for m in monitors if m.rect is not None and not m.rect.is_empty]
    focused = next((m.rect for m in monitors if m.focused and m.rect is not None), None)
    return rects, focused


def _hypr_dispatch(*args: str) -> None:
    _run(_HYPRCTL, ["dispatch", *args])


class _HyprlandSwitcher:
    def __init__(self) -> None:
        self._changed = False
        self._recorded: list[_HyprMonitor] = []

    @property
    def changed(self) -> bool:
        return self._changed

    def switch_to(self, window: DesktopWindow) -> None:
        if window.desktop.current:
            return
        try:
            client = next(
                (c for c in _hypr_clients() if c.address == window.window_id), None
            )
            self._recorded = [
                m
                for m in _hypr_monitors()
                if m.workspace_id is not None and _MONITOR_NAME_RE.fullmatch(m.name)
            ]
        except (_ToolFailedError, CaptureUnavailableError) as exc:
            raise DesktopSwitchError(_switch_detail(exc)) from exc
        if client is None:
            raise DesktopSwitchError("the window is gone")
        target = client.workspace_id
        if target is None or target <= 0:
            raise DesktopSwitchError("the window's workspace cannot be shown")
        if not self._recorded:
            raise DesktopSwitchError("Hyprland reports no monitor")
        self._changed = True
        try:
            _hypr_dispatch("workspace", str(target))
        except (_ToolFailedError, CaptureUnavailableError) as exc:
            raise DesktopSwitchError(_switch_detail(exc)) from exc

        def target_shown() -> bool:
            try:
                return any(m.workspace_id == target for m in _hypr_monitors())
            except (_ToolFailedError, CaptureUnavailableError):
                return False

        if not poll_until(target_shown):
            raise DesktopSwitchError("Hyprland did not show the window's workspace")

    def restore(self) -> bool:
        if not self._changed:
            return True
        try:
            recorded = self._recorded
            focused = next((m for m in recorded if m.focused), None)
            ordered = [m for m in recorded if m is not focused]
            if focused is not None:
                ordered.append(focused)
            current = {m.name: m.workspace_id for m in _hypr_monitors()}
            for monitor in ordered:
                if monitor.workspace_id is None:
                    continue
                if current.get(monitor.name) == monitor.workspace_id:
                    continue
                # Focus the monitor first so a workspace Hyprland destroyed
                # while empty is created again on its own monitor.
                _hypr_dispatch("focusmonitor", monitor.name)
                _hypr_dispatch("workspace", str(monitor.workspace_id))
            if focused is not None:
                _hypr_dispatch("focusmonitor", focused.name)

            def restored() -> bool:
                try:
                    now = _hypr_monitors()
                except (_ToolFailedError, CaptureUnavailableError):
                    return False
                by_name = {m.name: m for m in now}
                for monitor in recorded:
                    shown = by_name.get(monitor.name)
                    if shown is None or shown.workspace_id != monitor.workspace_id:
                        return False
                if focused is None:
                    return True
                shown_focused = by_name.get(focused.name)
                return shown_focused is not None and shown_focused.focused

            if poll_until(restored):
                return True
            return _restore_failed("Hyprland", "not verified in time")
        except (_ToolFailedError, CaptureUnavailableError) as exc:
            return _restore_failed("Hyprland", _switch_detail(exc))
        except Exception:  # never raise from restore (desktop_switch contract)
            logger.warning("Hyprland workspace restore failed", exc_info=True)
            return False


def create_hyprland_switcher(
    info: DesktopInfo, windows: Sequence[DesktopWindow]
) -> DesktopSwitcher:
    """A switcher for one capture on Hyprland
    (``desktop_switch.SwitcherFactory``); it records each monitor's
    workspace and the focused monitor itself when switching."""
    return _HyprlandSwitcher()


# --- exports (capture_platform.py) ----------------------------------------

SWAY_INVENTORY: WindowInventoryBackend = _SwayInventory()
SWAY_GRAB: WindowGrabBackend = _GrimGrab(_sway_monitors, "sway")
HYPRLAND_INVENTORY: WindowInventoryBackend = _HyprlandInventory()
HYPRLAND_GRAB: WindowGrabBackend = _GrimGrab(_hyprland_monitors, "Hyprland")
