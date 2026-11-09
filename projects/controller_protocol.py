from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, NotRequired, TypedDict

from controller_client.protocol import MessageType, serialize_message

# Largest raw ``interactive_sessions`` reply the consumer forwards
# (KEPT_SESSIONS_V1). INTERACTIVE_SESSIONS_MAX_ENTRIES entries of about 250
# bytes each fit many times over; anything bigger is refused.
INTERACTIVE_SESSIONS_RESULT_MAX_BYTES: Final[int] = 65_536

# Largest raw ``web_browser_result`` reply the consumer forwards
# (WEB_BROWSER_V1). ``content`` is at most WEB_RESULT_CONTENT_MAX_CHARS
# characters, and the runner's ``json.dumps`` escapes non-ASCII text (up to
# 12 bytes per character for astral ones), plus at most WEB_BROWSER_LIST_MAX
# small list entries; anything bigger is refused.
WEB_BROWSER_RESULT_MAX_BYTES: Final[int] = 1024 * 1024


class BaseActionEvent(TypedDict):
    type: str
    request_id: str
    reply_channel: str


class ClickActionEvent(BaseActionEvent):
    x: int
    y: int
    button: str
    modifiers: NotRequired[list[str]]


class DoubleClickActionEvent(BaseActionEvent):
    x: int
    y: int
    modifiers: NotRequired[list[str]]


class ScrollActionEvent(BaseActionEvent):
    direction: str
    amount: int
    x: NotRequired[int]
    y: NotRequired[int]
    modifiers: NotRequired[list[str]]


class HoverActionEvent(BaseActionEvent):
    x: int
    y: int
    modifiers: NotRequired[list[str]]


class DragActionEvent(BaseActionEvent):
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    button: str
    duration: float
    modifiers: NotRequired[list[str]]


class TypeTextActionEvent(BaseActionEvent):
    text: str
    interval: float


class KeyPressActionEvent(BaseActionEvent):
    keys: str


class ScreenshotActionEvent(BaseActionEvent):
    pass


class RunCommandActionEvent(BaseActionEvent):
    command: str


class BrowserNavigateActionEvent(BaseActionEvent):
    url: str
    browser: str


class BrowserClickActionEvent(BaseActionEvent):
    element_index: int
    browser: str


class BrowserDoubleClickActionEvent(BaseActionEvent):
    element_index: int
    browser: str


class BrowserTypeActionEvent(BaseActionEvent):
    element_index: int
    text: str
    browser: str


class BrowserHoverActionEvent(BaseActionEvent):
    element_index: int
    browser: str


class BrowserGetElementsActionEvent(BaseActionEvent):
    browser: str


class BrowserGetPageContentActionEvent(BaseActionEvent):
    browser: str


class BrowserGetUrlActionEvent(BaseActionEvent):
    browser: str


class BrowserTakeScreenshotActionEvent(BaseActionEvent):
    browser: str


class BrowserDownloadActionEvent(BaseActionEvent):
    url: str
    save_path: str
    browser: str


class BrowserListDownloadsActionEvent(BaseActionEvent):
    pass


class StartInteractiveCmdActionEvent(BaseActionEvent):
    command: str
    # KEPT_SESSIONS_V1 only (``controller_command_sessions``); agent starts
    # never carry them, so their message is unchanged.
    keep: NotRequired[bool]
    max_seconds: NotRequired[int]
    initial_read_ms: NotRequired[int]
    max_kept: NotRequired[int]


class SendInputActionEvent(BaseActionEvent):
    session_id: str
    input_text: str


class WaitForCommandActionEvent(BaseActionEvent):
    session_id: str


class TerminateInteractiveCmdActionEvent(BaseActionEvent):
    session_id: str


class ListInteractiveSessionsActionEvent(BaseActionEvent):
    """KEPT_SESSIONS_V1: no fields besides the request id."""


class LaunchAppActionEvent(BaseActionEvent):
    app_name: str


class CloseAppActionEvent(BaseActionEvent):
    app_name: str
    force: bool


class CloseTabActionEvent(BaseActionEvent):
    app_name: str


class CheckAppInstalledActionEvent(BaseActionEvent):
    app_name: str


class CleanupEnvironmentActionEvent(BaseActionEvent):
    pass


class ScanDirectoryActionEvent(BaseActionEvent):
    path: str
    max_depth: int
    max_entries: int
    allow_network: bool


class InspectPathsActionEvent(BaseActionEvent):
    paths: list[str]
    allow_network: bool


# Window listing and capture (WINDOW_CAPTURE_V1). The fields are exactly what
# ``serialize_list_windows_payload`` / ``serialize_capture_window_payload``
# produce; coordinates are screenshot pixels.


class ListWindowsActionEvent(BaseActionEvent):
    allowed: list[str]


class CaptureWindowActionEvent(BaseActionEvent):
    target: str
    process: str | None
    title_contains: str | None
    window_id: str | None
    # {"x", "y", "w", "h"} for target ``region``; None otherwise.
    region: dict[str, int] | None
    delay_ms: int
    allowed: list[str]
    max_bytes: int
    max_pixels: int
    switch_desktop: bool


# WebDriver browser layer (WEB_BROWSER_V1). The fields are exactly what
# ``projects.controller_web_browser_actions`` sends after checking them with
# the runner's own ``parse_web_browser_*_payload``; optional fields are left
# out when unset. ``browser`` is a ``WebBrowser`` value; ``target`` is
# ``serialize_web_element_target`` output.


class WebBrowserOpenActionEvent(BaseActionEvent):
    browser: str
    url: NotRequired[str]


class WebBrowserListActionEvent(BaseActionEvent):
    """No fields besides the request id."""


class WebBrowserNavigateActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    url: str


class WebBrowserHistoryActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    action: str


class WebBrowserTabsActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    action: str
    index: NotRequired[int]


class WebBrowserElementsActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    scope_css: NotRequired[str]
    max: NotRequired[int]


class WebBrowserActActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    action: str
    target: NotRequired[dict[str, object]]
    text: NotRequired[str]
    clear: NotRequired[bool]
    value: NotRequired[str]
    label: NotRequired[str]
    key: NotRequired[str]


class WebBrowserReadActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    what: str
    target: NotRequired[dict[str, object]]
    name: NotRequired[str]
    max_chars: NotRequired[int]


class WebBrowserWaitActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    target: dict[str, object]
    state: str
    timeout_ms: NotRequired[int]


class WebBrowserEvaluateActionEvent(BaseActionEvent):
    browser: NotRequired[str]
    script: str
    args_json: NotRequired[str]
    timeout_ms: NotRequired[int]


class WebBrowserCloseActionEvent(BaseActionEvent):
    browser: NotRequired[str]


# Phone or tablet target (MOBILE_DEVICE_V1). Coordinates are screenshot pixels.


class SwipeActionEvent(BaseActionEvent):
    direction: str
    distance: float
    duration: float
    x: NotRequired[int]
    y: NotRequired[int]


class LongPressActionEvent(BaseActionEvent):
    x: int
    y: int
    duration: float


class PressButtonActionEvent(BaseActionEvent):
    button: str


class RotateDeviceActionEvent(BaseActionEvent):
    orientation: str


class HideKeyboardActionEvent(BaseActionEvent):
    pass


class OpenUrlActionEvent(BaseActionEvent):
    url: str


class InstallAppActionEvent(BaseActionEvent):
    path: str


class ListDeviceAppsActionEvent(BaseActionEvent):
    """The installed apps of the phone (DEVICE_APPS_V1); no fields besides the
    request id (``serialize_list_device_apps_payload``)."""


class UpdateRunnerEvent(TypedDict):
    """Channel-layer event ``controller.update_runner``.

    Fire and forget: the runner answers with ``update_status`` messages
    keyed by ``update_id``, not with a reply on a ``reply_channel``.
    """

    type: str
    update_id: str
    target_version: str
    archive_base64: str
    archive_sha256: str
    archive_size: int


class ActionTypeRegistry:
    _ACTION_TYPE_MAP: Final[dict[str, MessageType]] = {
        "controller.click": MessageType.CLICK,
        "controller.double_click": MessageType.DOUBLE_CLICK,
        "controller.hover": MessageType.HOVER,
        "controller.scroll": MessageType.SCROLL,
        "controller.drag": MessageType.DRAG,
        "controller.type_text": MessageType.TYPE_TEXT,
        "controller.key_press": MessageType.KEY_PRESS,
        "controller.screenshot": MessageType.SCREENSHOT_REQUEST,
        "controller.run_command": MessageType.RUN_COMMAND,
        "controller.browser_navigate": MessageType.BROWSER_NAVIGATE,
        "controller.browser_click": MessageType.BROWSER_CLICK,
        "controller.browser_double_click": MessageType.BROWSER_DOUBLE_CLICK,
        "controller.browser_type": MessageType.BROWSER_TYPE,
        "controller.browser_hover": MessageType.BROWSER_HOVER,
        "controller.browser_get_elements": MessageType.BROWSER_GET_ELEMENTS,
        "controller.browser_get_page_content": MessageType.BROWSER_GET_PAGE_CONTENT,
        "controller.browser_get_url": MessageType.BROWSER_GET_URL,
        "controller.browser_take_screenshot": MessageType.BROWSER_TAKE_SCREENSHOT,
        "controller.browser_download": MessageType.BROWSER_DOWNLOAD,
        "controller.browser_list_downloads": MessageType.BROWSER_LIST_DOWNLOADS,
        "controller.start_interactive_cmd": MessageType.START_INTERACTIVE_CMD,
        "controller.send_input": MessageType.SEND_INPUT,
        "controller.wait_for_command": MessageType.WAIT_FOR_COMMAND,
        "controller.terminate_interactive_cmd": MessageType.TERMINATE_INTERACTIVE_CMD,
        "controller.list_interactive_sessions": (MessageType.LIST_INTERACTIVE_SESSIONS),
        "controller.launch_app": MessageType.LAUNCH_APP,
        "controller.close_app": MessageType.CLOSE_APP,
        "controller.close_tab": MessageType.CLOSE_TAB,
        "controller.check_app_installed": MessageType.CHECK_APP_INSTALLED,
        "controller.cleanup_environment": MessageType.CLEANUP_ENVIRONMENT,
        "controller.scan_directory": MessageType.SCAN_DIRECTORY,
        "controller.inspect_paths": MessageType.INSPECT_PATHS,
        "controller.list_windows": MessageType.LIST_WINDOWS,
        "controller.capture_window": MessageType.CAPTURE_WINDOW,
        "controller.web_browser_open": MessageType.WEB_BROWSER_OPEN,
        "controller.web_browser_list": MessageType.WEB_BROWSER_LIST,
        "controller.web_browser_navigate": MessageType.WEB_BROWSER_NAVIGATE,
        "controller.web_browser_history": MessageType.WEB_BROWSER_HISTORY,
        "controller.web_browser_tabs": MessageType.WEB_BROWSER_TABS,
        "controller.web_browser_elements": MessageType.WEB_BROWSER_ELEMENTS,
        "controller.web_browser_act": MessageType.WEB_BROWSER_ACT,
        "controller.web_browser_read": MessageType.WEB_BROWSER_READ,
        "controller.web_browser_wait": MessageType.WEB_BROWSER_WAIT,
        "controller.web_browser_evaluate": MessageType.WEB_BROWSER_EVALUATE,
        "controller.web_browser_close": MessageType.WEB_BROWSER_CLOSE,
        "controller.update_runner": MessageType.UPDATE_RUNNER,
        "controller.swipe": MessageType.SWIPE,
        "controller.long_press": MessageType.LONG_PRESS,
        "controller.press_button": MessageType.PRESS_BUTTON,
        "controller.rotate_device": MessageType.ROTATE_DEVICE,
        "controller.hide_keyboard": MessageType.HIDE_KEYBOARD,
        "controller.open_url": MessageType.OPEN_URL,
        "controller.install_app": MessageType.INSTALL_APP,
        "controller.list_device_apps": MessageType.LIST_DEVICE_APPS,
    }

    _PAYLOAD_KEYS: Final[dict[str, tuple[str, ...]]] = {
        "controller.click": ("x", "y", "button", "modifiers"),
        "controller.double_click": ("x", "y", "modifiers"),
        "controller.hover": ("x", "y", "modifiers"),
        "controller.scroll": ("direction", "amount", "x", "y", "modifiers"),
        "controller.drag": (
            "start_x",
            "start_y",
            "end_x",
            "end_y",
            "button",
            "duration",
            "modifiers",
        ),
        "controller.type_text": ("text", "interval"),
        "controller.key_press": ("keys",),
        "controller.screenshot": (),
        "controller.run_command": ("command",),
        "controller.browser_navigate": ("url", "browser"),
        "controller.browser_click": ("element_index", "browser"),
        "controller.browser_double_click": ("element_index", "browser"),
        "controller.browser_type": ("element_index", "text", "browser"),
        "controller.browser_hover": ("element_index", "browser"),
        "controller.browser_get_elements": ("browser",),
        "controller.browser_get_page_content": ("browser",),
        "controller.browser_get_url": ("browser",),
        "controller.browser_take_screenshot": ("browser",),
        "controller.browser_download": ("url", "save_path", "browser"),
        "controller.browser_list_downloads": (),
        # The kept-session fields go out only when the event has them.
        "controller.start_interactive_cmd": (
            "command",
            "keep",
            "max_seconds",
            "initial_read_ms",
            "max_kept",
        ),
        "controller.send_input": ("session_id", "input_text"),
        "controller.wait_for_command": ("session_id",),
        "controller.terminate_interactive_cmd": ("session_id",),
        "controller.list_interactive_sessions": (),
        "controller.launch_app": ("app_name",),
        "controller.close_app": ("app_name", "force"),
        "controller.close_tab": ("app_name",),
        "controller.check_app_installed": ("app_name",),
        "controller.cleanup_environment": (),
        "controller.scan_directory": (
            "path",
            "max_depth",
            "max_entries",
            "allow_network",
        ),
        "controller.inspect_paths": ("paths", "allow_network"),
        "controller.list_windows": ("allowed",),
        "controller.capture_window": (
            "target",
            "process",
            "title_contains",
            "window_id",
            "region",
            "delay_ms",
            "allowed",
            "max_bytes",
            "max_pixels",
            "switch_desktop",
        ),
        # WEB_BROWSER_V1: optional fields go out only when the event has them.
        "controller.web_browser_open": ("browser", "url"),
        "controller.web_browser_list": (),
        "controller.web_browser_navigate": ("browser", "url"),
        "controller.web_browser_history": ("browser", "action"),
        "controller.web_browser_tabs": ("browser", "action", "index"),
        "controller.web_browser_elements": ("browser", "scope_css", "max"),
        "controller.web_browser_act": (
            "browser",
            "action",
            "target",
            "text",
            "clear",
            "value",
            "label",
            "key",
        ),
        "controller.web_browser_read": (
            "browser",
            "what",
            "target",
            "name",
            "max_chars",
        ),
        "controller.web_browser_wait": ("browser", "target", "state", "timeout_ms"),
        "controller.web_browser_evaluate": (
            "browser",
            "script",
            "args_json",
            "timeout_ms",
        ),
        "controller.web_browser_close": ("browser",),
        "controller.update_runner": (
            "update_id",
            "target_version",
            "archive_base64",
            "archive_sha256",
            "archive_size",
        ),
        "controller.swipe": ("direction", "distance", "duration", "x", "y"),
        "controller.long_press": ("x", "y", "duration"),
        "controller.press_button": ("button",),
        "controller.rotate_device": ("orientation",),
        "controller.hide_keyboard": (),
        "controller.open_url": ("url",),
        "controller.install_app": ("path",),
        "controller.list_device_apps": (),
    }

    @classmethod
    def get_message_type(cls, event_type: str) -> MessageType:
        return cls._ACTION_TYPE_MAP[event_type]

    @classmethod
    def get_payload_keys(cls, event_type: str) -> tuple[str, ...]:
        return cls._PAYLOAD_KEYS[event_type]

    @classmethod
    def is_valid_action_type(cls, event_type: str) -> bool:
        return event_type in cls._ACTION_TYPE_MAP


class ControllerMessageBuilder:
    def __init__(self, registry: ActionTypeRegistry) -> None:
        self._registry = registry

    def build_action_message(self, event: BaseActionEvent, request_id: str) -> str:
        return self._build_message(event["type"], event, request_id)

    def build_update_runner_message(self, event: UpdateRunnerEvent) -> str:
        # The update id doubles as the request id so runner logs correlate.
        return self._build_message(event["type"], event, event["update_id"])

    def _build_message(
        self, event_type: str, event: Mapping[str, object], request_id: str
    ) -> str:
        message_type = self._registry.get_message_type(event_type)
        payload_keys = self._registry.get_payload_keys(event_type)
        payload_kwargs: dict[str, Any] = {
            key: event[key] for key in payload_keys if key in event
        }
        return serialize_message(message_type, request_id, **payload_kwargs)
