from __future__ import annotations

from typing import Any, Final, TypedDict

from controller_client.protocol import MessageType, serialize_message


class BaseActionEvent(TypedDict):
    type: str
    request_id: str
    reply_channel: str


class ClickActionEvent(BaseActionEvent):
    x: int
    y: int
    button: str


class HoverActionEvent(BaseActionEvent):
    x: int
    y: int


class DragActionEvent(BaseActionEvent):
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    button: str
    duration: float


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


class BrowserClickActionEvent(BaseActionEvent):
    element_index: int


class BrowserTypeActionEvent(BaseActionEvent):
    element_index: int
    text: str


class BrowserHoverActionEvent(BaseActionEvent):
    element_index: int


class BrowserGetElementsActionEvent(BaseActionEvent):
    pass


class BrowserGetPageContentActionEvent(BaseActionEvent):
    pass


class BrowserGetUrlActionEvent(BaseActionEvent):
    pass


class BrowserTakeScreenshotActionEvent(BaseActionEvent):
    pass


class BrowserDownloadActionEvent(BaseActionEvent):
    url: str
    save_path: str


class BrowserListDownloadsActionEvent(BaseActionEvent):
    pass


class StartInteractiveCmdActionEvent(BaseActionEvent):
    command: str


class SendInputActionEvent(BaseActionEvent):
    session_id: str
    input_text: str


class TerminateInteractiveCmdActionEvent(BaseActionEvent):
    session_id: str


class LaunchAppActionEvent(BaseActionEvent):
    app_name: str


class CheckAppInstalledActionEvent(BaseActionEvent):
    app_name: str


class ActionTypeRegistry:
    _ACTION_TYPE_MAP: Final[dict[str, MessageType]] = {
        "controller.click": MessageType.CLICK,
        "controller.hover": MessageType.HOVER,
        "controller.drag": MessageType.DRAG,
        "controller.type_text": MessageType.TYPE_TEXT,
        "controller.key_press": MessageType.KEY_PRESS,
        "controller.screenshot": MessageType.SCREENSHOT_REQUEST,
        "controller.run_command": MessageType.RUN_COMMAND,
        "controller.browser_navigate": MessageType.BROWSER_NAVIGATE,
        "controller.browser_click": MessageType.BROWSER_CLICK,
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
        "controller.terminate_interactive_cmd": MessageType.TERMINATE_INTERACTIVE_CMD,
        "controller.launch_app": MessageType.LAUNCH_APP,
        "controller.check_app_installed": MessageType.CHECK_APP_INSTALLED,
    }

    _PAYLOAD_KEYS: Final[dict[str, tuple[str, ...]]] = {
        "controller.click": ("x", "y", "button"),
        "controller.hover": ("x", "y"),
        "controller.drag": (
            "start_x",
            "start_y",
            "end_x",
            "end_y",
            "button",
            "duration",
        ),
        "controller.type_text": ("text", "interval"),
        "controller.key_press": ("keys",),
        "controller.screenshot": (),
        "controller.run_command": ("command",),
        "controller.browser_navigate": ("url",),
        "controller.browser_click": ("element_index",),
        "controller.browser_type": ("element_index", "text"),
        "controller.browser_hover": ("element_index",),
        "controller.browser_get_elements": (),
        "controller.browser_get_page_content": (),
        "controller.browser_get_url": (),
        "controller.browser_take_screenshot": (),
        "controller.browser_download": ("url", "save_path"),
        "controller.browser_list_downloads": (),
        "controller.start_interactive_cmd": ("command",),
        "controller.send_input": ("session_id", "input_text"),
        "controller.terminate_interactive_cmd": ("session_id",),
        "controller.launch_app": ("app_name",),
        "controller.check_app_installed": ("app_name",),
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
        event_type: str = event["type"]
        message_type = self._registry.get_message_type(event_type)
        payload_keys = self._registry.get_payload_keys(event_type)

        raw: dict[str, Any] = dict(event)
        payload_kwargs: dict[str, Any] = {
            key: raw[key] for key in payload_keys if key in raw
        }
        return serialize_message(message_type, request_id, **payload_kwargs)
