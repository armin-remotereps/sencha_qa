"""``list_windows`` and ``capture_window``: allowlisted window capture.

``WindowCaptureService`` answers both server messages; every refusal is a result with a status other than ``ok``, never an
exception.

``capture`` on a desktop:

1. session check (``capture_session``);
2. enumerate (``window_inventory``) and select (``window_capture_policy``:
   allowlist, deny list, current desktop first, topmost);
3. a minimised window is refused;
4. the person window is hidden with ``hidden_for_capture`` (confirmed, on the
   original desktop; unconfirmed: ``hide_failed``);
5. one worker-thread job (``asyncio.to_thread``): when the window is on
   another desktop, enumerate again (the person window is hidden now) and
   switch to that desktop (``desktop_switch``; the switcher sees only app
   windows, never the runner's own), settle and ``delay_ms``,
   enumerate again and find the same window, apply the screen rule, grab
   (visible rect first, window-only image when obscured), after a switch
   grab until two consecutive grabs match (``capture_freshness``), enumerate
   once more and, when a window now covers a window grabbed from the screen
   (or it moved or closed) or a foreign window now shows on the ``screen``
   target's monitor, grab once more and check again (still worse: refused),
   blank check, and in ``finally`` switch back. Cancelling the awaiting task never
   interrupts the thread: the service waits for the job (and so the switch
   back) before it lets go of the person window and the screen-use lock;
6. crop the region, show the person window again (leaving the hold), encode
   (``manual_capture.encode_shot`` with the request's limits) and reply with
   ``desktop_switched`` / ``desktop_restored``.

Phone target: one "device" entry (the foreground app, the device name, the
screen); ``screen`` / ``region`` only, the foreground app must be
allowlisted (checked before and after the grab), no hide.

The screen-use lock (``screen_use_lock``) is held around the hide and the
job, and around ``list_windows``'s inventory (never a temporary desktop
mid-switch). Logs carry status, process, size, desktop index and duration only:
never a title.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypeVar, runtime_checkable

from PIL import Image

from controller_client.capture_freshness import wait_for_stable_frame
from controller_client.capture_platform import PlatformCapture
from controller_client.capture_session import interactive_session_problem
from controller_client.desktop_switch import (
    DESKTOP_SWITCH_SETTLE_MS,
    DesktopSwitcher,
    DesktopSwitchError,
)
from controller_client.exceptions import ExecutionError
from controller_client.manual_capture import (
    EXTRA_SETTLE_SECONDS,
    EncodedShot,
    ShotTooLargeError,
    encode_shot,
)
from controller_client.person_window import PersonWindow
from controller_client.protocol import (
    CAPTURE_DESKTOP_NAME_MAX_CHARS,
    CAPTURE_TITLE_MAX_CHARS,
    PHONE_WINDOW_ID,
    CaptureRegion,
    CaptureStatus,
    CaptureTarget,
    CaptureWindowPayload,
    ListWindowsPayload,
    WindowCaptureResultPayload,
    WindowDesktopPayload,
    WindowInfoPayload,
    WindowListResultPayload,
    normalize_process_name,
)
from controller_client.screen_use_lock import screen_use_lock
from controller_client.target import get_mobile_device, get_target_backend
from controller_client.window_capture_policy import (
    DenyRules,
    SelectionRefusedError,
    WindowSelection,
    build_deny_rules,
    capturable_count,
    check_region,
    foreign_processes_on_monitor,
    is_allowed,
    listable_windows,
    select_window,
    switcher_windows,
)
from controller_client.window_grab import (
    Grab,
    ensure_not_blank,
    plan_window_grab,
    rect_grabber,
    window_is_unobscured,
)
from controller_client.window_inventory import platform_capture
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopWindow,
    Rect,
    WindowDesktop,
)

logger = logging.getLogger(__name__)

# How long shutdown (SIGTERM, Ctrl+C) waits for a running capture job, so its
# switch back runs: the switch, settle, delay, freshness and switch-back
# budgets plus grabs.
WINDOW_CAPTURE_SHUTDOWN_WAIT_SECONDS: Final[float] = 30.0

_FAILED_DETAIL: Final[str] = "the window could not be captured"
_HIDE_FAILED_DETAIL: Final[str] = (
    "the Test Runner window could not be hidden; nothing was captured"
)
_GONE_DETAIL: Final[str] = "the window closed before it was captured"
_MOVED_DETAIL: Final[str] = "the window moved to another desktop"
_NOT_SHOWN_DETAIL: Final[str] = "the window was not shown after the desktop switch"
_LATE_GONE_DETAIL: Final[str] = "the window closed during the capture"
_LATE_COVERED_DETAIL: Final[str] = (
    "another window covered the window or it moved during the capture; try again"
)
_NOT_SETTLED_DETAIL: Final[str] = (
    "the window kept changing after the desktop switch; try a larger delay_ms"
)
_PHONE_WINDOW_DETAIL: Final[str] = (
    "a phone or tablet has no windows; capture the screen or a region of it"
)
_PHONE_APP_UNKNOWN_DETAIL: Final[str] = (
    "the app in front on the phone or tablet cannot be read"
)
_PHONE_APP_CHANGED_DETAIL: Final[str] = (
    "the app in front changed while the screen was captured"
)
_NOT_ALLOWED_DETAIL: Final[str] = "the app in front is not an allowed application"
_PHONE_GRAB_DETAIL: Final[str] = "the phone or tablet screen could not be captured"

_T = TypeVar("_T")


@runtime_checkable
class ForegroundAppSource(Protocol):
    """A phone backend that can read the app in front (its id)."""

    def foreground_app(self) -> str | None: ...


@dataclass(frozen=True)
class _JobOutcome:
    """What the worker-thread job hands back (it never raises)."""

    status: CaptureStatus
    detail: str
    image: Image.Image | None
    window: DesktopWindow | None
    desktop_switched: bool
    desktop_restored: bool | None


def _desktop_payload(desktop: WindowDesktop) -> WindowDesktopPayload:
    name = desktop.name[:CAPTURE_DESKTOP_NAME_MAX_CHARS] if desktop.name else None
    return WindowDesktopPayload(
        current=desktop.current, index=desktop.index, name=name or None
    )


def _window_info(window: DesktopWindow) -> WindowInfoPayload:
    return WindowInfoPayload(
        window_id=window.window_id,
        process=window.process,
        title=window.title[:CAPTURE_TITLE_MAX_CHARS],
        width=max(window.rect.width, 0),
        height=max(window.rect.height, 0),
        minimized=window.minimized,
        focused=window.focused,
        desktop=_desktop_payload(window.desktop),
    )


def list_refusal(status: CaptureStatus, detail: str) -> WindowListResultPayload:
    """A ``window_list_result`` that lists nothing (``status`` is not OK)."""
    return WindowListResultPayload(
        status=status,
        detail=detail,
        windows=(),
        others_hidden=0,
        current_desktop=None,
        desktop_count=None,
    )


def capture_refusal(
    status: CaptureStatus,
    detail: str,
    *,
    window: DesktopWindow | None = None,
    other_matches: int = 0,
    desktop_switched: bool = False,
    desktop_restored: bool | None = None,
) -> WindowCaptureResultPayload:
    """A ``window_capture_result`` without an image (``status`` is not OK).

    ``window``: the refused, allowlisted window the reply may name, or None.
    """
    return WindowCaptureResultPayload(
        status=status,
        detail=detail,
        mime=None,
        data=b"",
        sha256="",
        width=0,
        height=0,
        window_title="" if window is None else window.title,
        process="" if window is None else window.process,
        other_matches=other_matches,
        desktop=None if window is None else _desktop_payload(window.desktop),
        desktop_switched=desktop_switched,
        desktop_restored=desktop_restored,
    )


def _crop(image: Image.Image, region: CaptureRegion | None) -> Image.Image:
    """The region of the shot (``region_out_of_bounds`` when it does not fit)."""
    if region is None:
        return image
    check_region(region, image.width, image.height)
    return image.crop((region.x, region.y, region.x + region.w, region.y + region.h))


def _find_again(
    windows: Sequence[DesktopWindow],
    selection: WindowSelection,
    *,
    allowed: frozenset[str],
    deny: DenyRules,
    switched: bool,
) -> DesktopWindow:
    """The selected window in a fresh inventory, still capturable."""
    window_id = selection.window.window_id
    found = next((w for w in windows if w.window_id == window_id), None)
    if found is None:
        raise CaptureUnavailableError(CaptureStatus.WINDOW_NOT_FOUND, _GONE_DETAIL)
    if not is_allowed(found, allowed, deny):
        raise CaptureUnavailableError(CaptureStatus.NOT_ALLOWED, "")
    if found.minimized:
        raise CaptureUnavailableError(CaptureStatus.WINDOW_MINIMIZED, "")
    if not found.desktop.current:
        if switched:
            raise CaptureUnavailableError(
                CaptureStatus.DESKTOP_SWITCH_FAILED, _NOT_SHOWN_DETAIL
            )
        raise CaptureUnavailableError(
            CaptureStatus.WINDOW_ON_OTHER_DESKTOP, _MOVED_DETAIL
        )
    return found


def _grab_on_shown_desktop(
    parts: PlatformCapture,
    selection: WindowSelection,
    payload: CaptureWindowPayload,
    *,
    allowed: frozenset[str],
    deny: DenyRules,
    switched: bool,
) -> tuple[Image.Image, DesktopWindow]:
    """Steps after the switch (or without one): settle, delay, enumerate
    again, screen rule, grab, freshness after a switch, blank check."""
    settle = EXTRA_SETTLE_SECONDS
    if switched:
        settle = max(settle, DESKTOP_SWITCH_SETTLE_MS / 1000)
    time.sleep(settle + payload.delay_ms / 1000)
    _, windows = parts.inventory.list_windows()
    window = _find_again(
        windows, selection, allowed=allowed, deny=deny, switched=switched
    )
    monitor: Rect | None = None
    covered_by: tuple[str, ...] = ()
    if payload.target is CaptureTarget.SCREEN:
        monitor = parts.grab.monitor_rect(window)
        problem = _screen_problem(monitor, windows, allowed, deny)
        if problem is not None:
            raise problem
        grab: Grab = rect_grabber(parts.grab, monitor)
        on_screen = True
    else:
        plan = plan_window_grab(parts.grab, window, windows, allowed, deny)
        grab, on_screen, covered_by = plan.grab, plan.on_screen, plan.covered_by
    if switched:
        stable = wait_for_stable_frame(grab)
        if stable is None:
            raise CaptureUnavailableError(
                CaptureStatus.WINDOW_NOT_SETTLED, _NOT_SETTLED_DETAIL
            )
        image = stable
    else:
        image = grab()
    if on_screen:
        # The decision above came from one inventory, but the grab (and the
        # freshness loop) took time: a window that appeared since would be
        # in the shot. Check once more; retry the grab once if it got worse.
        late = _late_problem(parts, window, monitor, allowed, deny)
        if late is not None:
            problem, retryable = late
            if not retryable:
                # Closed or moved: a grab of the old rect cannot be right.
                raise problem
            image = grab()
            late = _late_problem(parts, window, monitor, allowed, deny)
            if late is not None:
                raise late[0]
    ensure_not_blank(image, covered_by)
    return image, window


def _screen_problem(
    monitor: Rect,
    windows: Sequence[DesktopWindow],
    allowed: frozenset[str],
    deny: DenyRules,
) -> CaptureUnavailableError | None:
    """``screen_not_clean`` naming the foreign processes on ``monitor``."""
    foreign = foreign_processes_on_monitor(monitor, windows, allowed, deny)
    if not foreign:
        return None
    return CaptureUnavailableError(CaptureStatus.SCREEN_NOT_CLEAN, ", ".join(foreign))


def _late_problem(
    parts: PlatformCapture,
    window: DesktopWindow,
    monitor: Rect | None,
    allowed: frozenset[str],
    deny: DenyRules,
) -> tuple[CaptureUnavailableError, bool] | None:
    """A fresh inventory's verdict on a grab from the screen that passed,
    with whether one more grab could fix it: ``monitor`` set (the ``screen``
    target): a foreign window now shows on it (retryable); else ``window``
    (grabbed from its visible rect) closed or moved (not retryable) or is
    now covered (retryable). None when nothing got worse."""
    _, windows = parts.inventory.list_windows()
    if monitor is not None:
        screen = _screen_problem(monitor, windows, allowed, deny)
        return None if screen is None else (screen, True)
    found = next((w for w in windows if w.window_id == window.window_id), None)
    if found is None:
        return (
            CaptureUnavailableError(CaptureStatus.WINDOW_NOT_FOUND, _LATE_GONE_DETAIL),
            False,
        )
    if found.rect != window.rect:
        return (
            CaptureUnavailableError(
                CaptureStatus.WINDOW_OBSCURED, _LATE_COVERED_DETAIL
            ),
            False,
        )
    if not window_is_unobscured(parts.grab, found, windows, allowed, deny):
        return (
            CaptureUnavailableError(
                CaptureStatus.WINDOW_OBSCURED, _LATE_COVERED_DETAIL
            ),
            True,
        )
    return None


def _restore(switcher: DesktopSwitcher) -> bool:
    try:
        restored = switcher.restore()
    except Exception:  # restore() never raises by contract; be safe anyway
        logger.warning("Desktop switch back failed", exc_info=True)
        return False
    if not restored:
        logger.warning("Desktop switch back could not be verified")
    return restored


def _switch_target(
    parts: PlatformCapture, selection: WindowSelection, deny: DenyRules
) -> tuple[DesktopSwitcher | None, DesktopWindow]:
    """Enumerate again (the person window is hidden by now), find the
    selected window and, when it is still on another desktop, switch there.

    The switcher is built from app windows only, without the runner's own
    (``switcher_windows``), so it never anchors on or counts the hidden
    person window. Returns the switcher (None when no switch was needed) and
    the window as the fresh inventory reports it. Nothing is switched here:
    the caller calls ``switch_to`` and restores in ``finally``."""
    info, windows = parts.inventory.list_windows()
    window_id = selection.window.window_id
    target = next((w for w in windows if w.window_id == window_id), None)
    if target is None:
        raise CaptureUnavailableError(CaptureStatus.WINDOW_NOT_FOUND, _GONE_DETAIL)
    if target.desktop.current:
        return None, target
    return parts.create_switcher(info, switcher_windows(windows, deny)), target


def _screen_job(
    parts: PlatformCapture,
    selection: WindowSelection,
    payload: CaptureWindowPayload,
    allowed: frozenset[str],
    deny: DenyRules,
) -> _JobOutcome:
    """Switch (if needed), grab, switch back: one blocking function, so the
    switch back in ``finally`` always runs on this thread."""
    switcher: DesktopSwitcher | None = None
    status = CaptureStatus.OK
    detail = ""
    image: Image.Image | None = None
    window: DesktopWindow | None = selection.window
    restored: bool | None = None
    try:
        if selection.needs_switch:
            switcher, target = _switch_target(parts, selection, deny)
            if switcher is not None:
                try:
                    # May also raise CaptureUnavailableError (helper_missing,
                    # not_interactive) before anything changes.
                    switcher.switch_to(target)
                except DesktopSwitchError as e:
                    raise CaptureUnavailableError(
                        CaptureStatus.DESKTOP_SWITCH_FAILED, e.detail
                    ) from e
        image, window = _grab_on_shown_desktop(
            parts,
            selection,
            payload,
            allowed=allowed,
            deny=deny,
            switched=switcher is not None,
        )
    except CaptureUnavailableError as e:
        status, detail, image = e.status, e.detail, None
    except Exception:
        logger.exception("Window capture failed")
        status, detail, image = CaptureStatus.CAPTURE_FAILED, _FAILED_DETAIL, None
    finally:
        switched = switcher is not None and switcher.changed
        if switcher is not None and switched:
            restored = _restore(switcher)
    return _JobOutcome(
        status=status,
        detail=detail,
        image=image,
        window=window,
        desktop_switched=switched,
        desktop_restored=restored,
    )


class WindowCaptureService:
    """Answers ``list_windows`` and ``capture_window`` (module docstring).

    ``window`` is the person window (None when it is off): hidden, confirmed,
    around every desktop capture.
    """

    def __init__(self, window: PersonWindow | None) -> None:
        self._window = window
        # Running worker-thread jobs; shutdown waits for them (switch back).
        self._running_jobs = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def wait_idle(
        self, timeout: float = WINDOW_CAPTURE_SHUTDOWN_WAIT_SECONDS
    ) -> None:
        """Wait (bounded) until no capture job runs, so a desktop switched for
        a capture is switched back before the runner exits."""
        if self._idle.is_set():
            return
        logger.info("Waiting for a running window capture to finish")
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("A window capture was still running at shutdown")

    async def _in_thread(self, fn: Callable[[], _T]) -> _T:
        """Run ``fn`` on a worker thread; when the awaiting task is cancelled
        (socket closed, shutdown) wait for the thread to finish before the
        cancellation goes on, so nothing (person window, screen-use lock) is
        let go while it may still switch desktops."""
        job: asyncio.Future[_T] = asyncio.ensure_future(asyncio.to_thread(fn))
        self._running_jobs += 1
        self._idle.clear()
        job.add_done_callback(self._job_done)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            while not job.done():
                try:
                    await asyncio.wait({job})
                except asyncio.CancelledError:
                    continue
            raise

    def _job_done(self, job: asyncio.Future[_T]) -> None:
        self._running_jobs -= 1
        if self._running_jobs == 0:
            self._idle.set()
        if not job.cancelled() and job.exception() is not None:
            logger.warning("Window capture job failed", exc_info=job.exception())

    # --- list_windows -----------------------------------------------------

    async def list_windows(
        self, payload: ListWindowsPayload
    ) -> WindowListResultPayload:
        start = time.monotonic()
        allowed = frozenset(payload.allowed)
        if get_mobile_device() is not None:
            result = await self._list_phone(allowed)
        else:
            result = await self._list_desktop(allowed)
        logger.info(
            "Window list: status=%s windows=%d hidden=%d desktop=%s in %d ms",
            result.status.value,
            len(result.windows),
            result.others_hidden,
            result.current_desktop,
            (time.monotonic() - start) * 1000,
        )
        return result

    async def _list_desktop(self, allowed: frozenset[str]) -> WindowListResultPayload:
        problem = await asyncio.to_thread(interactive_session_problem)
        if problem is not None:
            return list_refusal(*problem)
        try:
            parts = platform_capture()
            # Never mid-switch: a capture switches desktops under this lock.
            async with screen_use_lock():
                info, windows = await asyncio.to_thread(parts.inventory.list_windows)
            deny = await asyncio.to_thread(build_deny_rules, windows)
        except CaptureUnavailableError as e:
            return list_refusal(e.status, e.detail)
        listed = listable_windows(windows, allowed, deny)
        return WindowListResultPayload(
            status=CaptureStatus.OK,
            detail="",
            windows=tuple(_window_info(window) for window in listed),
            others_hidden=capturable_count(windows) - len(listed),
            current_desktop=info.current,
            desktop_count=info.count,
        )

    async def _list_phone(self, allowed: frozenset[str]) -> WindowListResultPayload:
        try:
            process = await asyncio.to_thread(_phone_foreground_process)
            width, height = await asyncio.to_thread(get_target_backend().capture_size)
            name = await asyncio.to_thread(_phone_name)
        except CaptureUnavailableError as e:
            return list_refusal(e.status, e.detail)
        except ExecutionError:
            logger.warning("Phone window list failed", exc_info=True)
            return list_refusal(CaptureStatus.CAPTURE_FAILED, _PHONE_GRAB_DETAIL)
        if process is None or process not in allowed:
            windows: tuple[WindowInfoPayload, ...] = ()
        else:
            windows = (
                WindowInfoPayload(
                    window_id=PHONE_WINDOW_ID,
                    process=process,
                    title=name,
                    width=width,
                    height=height,
                    minimized=False,
                    focused=True,
                    desktop=WindowDesktopPayload(current=True, index=None, name=None),
                ),
            )
        return WindowListResultPayload(
            status=CaptureStatus.OK,
            detail="",
            windows=windows,
            others_hidden=1 - len(windows),
            current_desktop=None,
            desktop_count=None,
        )

    # --- capture_window ---------------------------------------------------

    async def capture(
        self, payload: CaptureWindowPayload
    ) -> WindowCaptureResultPayload:
        start = time.monotonic()
        if get_mobile_device() is not None:
            result = await self._capture_phone(payload)
        else:
            result = await self._capture_desktop(payload)
        logger.info(
            "Window capture: status=%s process=%s size=%dx%d desktop=%s "
            "switched=%s restored=%s in %d ms",
            result.status.value,
            result.process or "-",
            result.width,
            result.height,
            None if result.desktop is None else result.desktop.index,
            result.desktop_switched,
            result.desktop_restored,
            (time.monotonic() - start) * 1000,
        )
        return result

    async def _capture_desktop(
        self, payload: CaptureWindowPayload
    ) -> WindowCaptureResultPayload:
        problem = await asyncio.to_thread(interactive_session_problem)
        if problem is not None:
            return capture_refusal(*problem)
        allowed = frozenset(payload.allowed)
        async with screen_use_lock():
            try:
                parts = platform_capture()
                _, windows = await asyncio.to_thread(parts.inventory.list_windows)
                deny = await asyncio.to_thread(build_deny_rules, windows)
                selection = select_window(
                    windows,
                    window_id=payload.window_id,
                    process=payload.process,
                    title_contains=payload.title_contains,
                    allowed=allowed,
                    deny=deny,
                    switch_desktop=payload.switch_desktop,
                )
            except SelectionRefusedError as e:
                return capture_refusal(
                    e.status, e.detail, window=e.window, other_matches=e.other_matches
                )
            except CaptureUnavailableError as e:
                return capture_refusal(e.status, e.detail)
            outcome = await self._hidden_job(
                lambda: _screen_job(parts, selection, payload, allowed, deny)
            )
        return await self._finish(outcome, selection.other_matches, payload)

    async def _hidden_job(self, job: Callable[[], _JobOutcome]) -> _JobOutcome:
        """Run the screen job with the person window hidden (confirmed)."""
        window = self._window
        if window is None:
            return await self._in_thread(job)
        async with window.hidden_for_capture() as confirmed:
            if not confirmed:
                logger.warning(
                    "Window capture aborted: the window did not confirm it was hidden"
                )
                return _JobOutcome(
                    status=CaptureStatus.HIDE_FAILED,
                    detail=_HIDE_FAILED_DETAIL,
                    image=None,
                    window=None,
                    desktop_switched=False,
                    desktop_restored=None,
                )
            return await self._in_thread(job)

    async def _finish(
        self,
        outcome: _JobOutcome,
        other_matches: int,
        payload: CaptureWindowPayload,
    ) -> WindowCaptureResultPayload:
        """Crop, encode and build the reply of a finished job."""
        window = outcome.window
        if outcome.status is not CaptureStatus.OK or outcome.image is None:
            return capture_refusal(
                outcome.status,
                outcome.detail,
                window=window,
                other_matches=other_matches,
                desktop_switched=outcome.desktop_switched,
                desktop_restored=outcome.desktop_restored,
            )
        try:
            shot = await asyncio.to_thread(_crop_and_encode, outcome.image, payload)
        except CaptureUnavailableError as e:
            return capture_refusal(
                e.status,
                e.detail,
                window=window,
                other_matches=other_matches,
                desktop_switched=outcome.desktop_switched,
                desktop_restored=outcome.desktop_restored,
            )
        return WindowCaptureResultPayload(
            status=CaptureStatus.OK,
            detail="",
            mime=shot.mime,
            data=shot.data,
            sha256=shot.sha256,
            width=shot.width,
            height=shot.height,
            window_title="" if window is None else window.title,
            process="" if window is None else window.process,
            other_matches=other_matches,
            desktop=None if window is None else _desktop_payload(window.desktop),
            desktop_switched=outcome.desktop_switched,
            desktop_restored=outcome.desktop_restored,
        )

    async def _capture_phone(
        self, payload: CaptureWindowPayload
    ) -> WindowCaptureResultPayload:
        if payload.target is CaptureTarget.WINDOW:
            return capture_refusal(CaptureStatus.UNSUPPORTED, _PHONE_WINDOW_DETAIL)
        allowed = frozenset(payload.allowed)
        async with screen_use_lock():
            try:
                image, process, name = await self._in_thread(
                    lambda: _phone_grab(allowed)
                )
                shot = await asyncio.to_thread(_crop_and_encode, image, payload)
            except CaptureUnavailableError as e:
                return capture_refusal(e.status, e.detail)
        return WindowCaptureResultPayload(
            status=CaptureStatus.OK,
            detail="",
            mime=shot.mime,
            data=shot.data,
            sha256=shot.sha256,
            width=shot.width,
            height=shot.height,
            window_title=name,
            process=process,
            other_matches=0,
            desktop=WindowDesktopPayload(current=True, index=None, name=None),
            desktop_switched=False,
            desktop_restored=None,
        )


def _crop_and_encode(image: Image.Image, payload: CaptureWindowPayload) -> EncodedShot:
    """Region crop, then the shot within the request's limits (blocking)."""
    cropped = _crop(image, payload.region)
    try:
        return encode_shot(
            cropped, max_bytes=payload.max_bytes, max_pixels=payload.max_pixels
        )
    except ShotTooLargeError as e:
        raise CaptureUnavailableError(CaptureStatus.TOO_LARGE, "") from e


def _phone_foreground_process() -> str | None:
    """The normalized id of the app in front on the phone (blocking)."""
    device = get_mobile_device()
    if not isinstance(device, ForegroundAppSource):
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, _PHONE_APP_UNKNOWN_DETAIL
        )
    app = device.foreground_app()
    return None if app is None else normalize_process_name(app)


def _phone_name() -> str:
    device = get_mobile_device()
    return "" if device is None else device.device_info().name


def _phone_grab(allowed: frozenset[str]) -> tuple[Image.Image, str, str]:
    """The phone's screen with an allowlisted app in front, checked before
    and after the grab; with the app id and the device name (blocking)."""
    try:
        before = _phone_foreground_process()
        if before is None or before not in allowed:
            raise CaptureUnavailableError(
                CaptureStatus.NOT_ALLOWED, _NOT_ALLOWED_DETAIL
            )
        image = get_target_backend().screenshot().convert("RGB")
        after = _phone_foreground_process()
        name = _phone_name()
    except ExecutionError:
        logger.warning("Phone screen capture failed", exc_info=True)
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, _PHONE_GRAB_DETAIL
        ) from None
    if after != before:
        raise CaptureUnavailableError(
            CaptureStatus.NOT_ALLOWED, _PHONE_APP_CHANGED_DETAIL
        )
    ensure_not_blank(image)
    return image, before, name
