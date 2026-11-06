from __future__ import annotations

from functools import cache

from agents.types import ToolCategory, ToolDefinition, ToolParameter
from controller_client.protocol import (
    DEFAULT_LONG_PRESS_SECONDS,
    DEFAULT_SCROLL_AMOUNT,
    DEFAULT_SWIPE_DISTANCE,
    DEFAULT_SWIPE_DURATION,
    LONG_PRESS_MAX_SECONDS,
    LONG_PRESS_MIN_SECONDS,
    MAX_SCROLL_AMOUNT,
    MOBILE_TARGETS,
    SWIPE_MAX_DISTANCE,
    SWIPE_MAX_DURATION,
    SWIPE_MIN_DISTANCE,
    SWIPE_MIN_DURATION,
    BrowserName,
    DeviceOrientation,
    MobileButton,
    RunnerTarget,
    ScrollDirection,
)
from knowledge_base.services.agent_tools import knowledge_tool_definitions


def get_controller_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="execute_command",
            description=(
                "Execute a shell command. Handles both interactive and non-interactive commands. "
                "If the command needs input (a password, Y/n confirmation), it returns a "
                "session_id — use send_command_input to respond. "
                "Each command runs in a FRESH shell session — use the cwd parameter "
                "to set the working directory instead of a separate cd command. "
                "The command runs in a HIDDEN background shell: no terminal window opens "
                "and nothing appears on screen. Its output (stdout/stderr, exit code) is "
                "returned in the tool result — read that instead of taking a screenshot. "
                "Only a GUI application the command launches becomes visible on screen."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="command",
                    type="string",
                    description=(
                        "The command line, in the shell named in ENVIRONMENT "
                        "(cmd.exe on Windows, bash elsewhere)."
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="cwd",
                    type="string",
                    description=(
                        "Optional working directory to run the command in: an "
                        "absolute path, or ~/... on macOS and Linux. "
                        "Each command runs in a fresh shell, so a standalone 'cd' has no effect "
                        "on later commands. Use this parameter instead."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="wait_for_command",
            description=(
                "Wait for a long-running interactive command to finish. "
                "Use this after execute_command returns with is_alive=True for "
                "commands that take a long time (e.g. package installs, large downloads). "
                "Blocks until the process exits, polling every 5 seconds, and returns "
                "all accumulated output plus the exit code."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="session_id",
                    type="string",
                    description="The session ID returned by execute_command.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="send_command_input",
            description=(
                "Send input to a running interactive command session. "
                "Returns the output produced after sending the input. "
                "Pass empty input_text to read more output without sending anything."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="session_id",
                    type="string",
                    description="The session ID returned by execute_command.",
                    required=True,
                ),
                ToolParameter(
                    name="input_text",
                    type="string",
                    description="The text to send as input (e.g. password, 'y' for confirmation). Empty string to just read output.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="take_screenshot",
            description=(
                "Take a screenshot of the desktop and answer a question about it using vision AI."
                " Not for web pages opened with browser_navigate — use browser_take_screenshot for those."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="question",
                    type="string",
                    description="Question to answer about the screenshot.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="click",
            description=(
                "Click an element on the desktop found by vision-based natural-language description. "
                "The target element is verified by vision before clicking and the result is checked "
                "afterwards; the tool retries automatically with other candidates (up to 10) and "
                "returns an error if it cannot confirm the element, or an 'element not found' "
                "error once 3 candidates on the current screen have been rejected."
                " Not for web pages opened with browser_navigate — use browser_click for those."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to click.",
                    required=True,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after the click "
                        "(e.g. 'the Settings dialog opens'). The tool verifies the click "
                        "against it and retries other candidates when the click missed; "
                        "when the intended element was clicked but the result differs, it "
                        "stops and reports the action as performed but not confirmed."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="double_click",
            description=(
                "Double-click an element on the desktop found by vision-based natural-language "
                "description (open a file or desktop icon, edit a grid cell, select a word). "
                "The target element is verified by vision before double-clicking and the result "
                "is checked afterwards; the tool retries automatically with other candidates "
                "(up to 10) and returns an error if it cannot confirm the element, or an "
                "'element not found' error once 3 candidates on the current screen have been "
                "rejected. Use it only when the step says double-click or the UI needs one; "
                "never emulate it with two click calls."
                " Not for web pages opened with browser_navigate — use browser_double_click for those."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to double-click.",
                    required=True,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after the double-click "
                        "(e.g. 'the Documents folder opens'). The tool verifies the "
                        "double-click against it and retries other candidates when it "
                        "missed; when the intended element was double-clicked but the "
                        "result differs, it stops and reports the action as performed but "
                        "not confirmed."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="click_at",
            description=(
                "Left-click at pixel coordinates of the desktop screenshot, for targets "
                "click cannot find by description: window chrome, a blank area, the end "
                "of a text line, a scrollbar. Get the coordinates from take_screenshot, "
                "which reports pixel coordinates and the screenshot size. It is a real "
                "mouse click, so it moves keyboard focus. Not verified unless "
                "expected_result is given, and never retried. Prefer click whenever the "
                "target can be described. Never use it for web pages opened with "
                "browser_navigate — use browser_click for those."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="x",
                    type="integer",
                    description="Horizontal pixel coordinate in the latest screenshot.",
                    required=True,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description="Vertical pixel coordinate in the latest screenshot.",
                    required=True,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after the click (e.g. "
                        "'the caret is at the end of the Editor 3 text'). The tool then "
                        "compares the screen before and after once and reports whether "
                        "it is shown; it never clicks again."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="type_text",
            description=(
                "Type text using the keyboard."
                " To fill a field on a web page opened with browser_navigate, use browser_type instead."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="text",
                    type="string",
                    description="Text to type.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="key_press",
            description="Press a key or key combination (e.g., 'Return', 'ctrl+c', 'alt+F4').",
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="keys",
                    type="string",
                    description="Key or key combination to press.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="hover",
            description=(
                "Hover over an element on the desktop found by vision-based natural-language description. "
                "The target element is verified by vision before hovering; the tool retries "
                "automatically with other candidates (up to 10) and returns an error if it "
                "cannot confirm the element."
                " Not for web pages opened with browser_navigate — use browser_hover for those."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to hover over.",
                    required=True,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after hovering "
                        "(e.g. 'a tooltip with the file size appears'). The post-hover check "
                        "runs only when this is given; the tool retries other candidates when "
                        "the action missed, and stops and reports it as performed but not "
                        "confirmed when the intended element was acted on but the result differs."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="drag",
            description=(
                "Drag from one element to another on the desktop, both found by vision-based description. "
                "Both elements are verified by vision before dragging; the tool retries "
                "automatically with other candidates (up to 10 in total) and returns an error "
                "if it cannot confirm them."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="start_description",
                    type="string",
                    description="Natural language description of the element to drag from.",
                    required=True,
                ),
                ToolParameter(
                    name="end_description",
                    type="string",
                    description="Natural language description of the element to drag to.",
                    required=True,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after the drag "
                        "(e.g. 'the file appears inside the Archive folder'). The post-drag check "
                        "runs only when this is given; the tool retries other candidates when "
                        "the action missed, and stops and reports it as performed but not "
                        "confirmed when the intended element was acted on but the result differs."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="scroll",
            description=(
                "Scroll the desktop with the mouse wheel, for content outside the "
                "visible area of a native app or window. Give x and y of a point over "
                "the area to scroll (pixel coordinates from take_screenshot); without "
                "them it scrolls where the pointer is. Take a screenshot afterwards to "
                "see the revealed content. Not for web pages opened with "
                "browser_navigate."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="direction",
                    type="string",
                    description=(
                        "Wheel direction: down reveals content below, up content "
                        "above, left and right content to the sides."
                    ),
                    required=True,
                    enum=tuple(direction.value for direction in ScrollDirection),
                ),
                ToolParameter(
                    name="amount",
                    type="integer",
                    description=(
                        f"Wheel notches, 1-{MAX_SCROLL_AMOUNT}. Optional, default "
                        f"{DEFAULT_SCROLL_AMOUNT}."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="x",
                    type="integer",
                    description=(
                        "Optional. Horizontal pixel coordinate of a point over the "
                        "area to scroll; give it together with y."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description=(
                        "Optional. Vertical pixel coordinate of a point over the area "
                        "to scroll; give it together with x."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="launch_app",
            description=(
                "Launch a GUI application by its human-readable name. "
                "Discovers installed apps on the remote machine using OS-native methods "
                "and fuzzy-matches the provided name. If no confident match is found, "
                "returns a list of similar app names as suggestions."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="app_name",
                    type="string",
                    description="The human-readable name of the application to launch (e.g. 'Calculator', 'Chrome', 'Firefox').",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="close_app",
            description=(
                "Quit a desktop application the test no longer needs so its windows stop "
                "cluttering the screen. Housekeeping: call it only after the step's "
                "expected result is verified; a failed close never fails the step. "
                "Never for the browser opened by browser tools, or for the Test Runner "
                "or the terminal it runs in (use close_tab for a tab you opened there)."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="app_name",
                    type="string",
                    description="The human-readable name of the application to quit (e.g. 'Calculator', 'TextEdit').",
                    required=True,
                ),
                ToolParameter(
                    name="force",
                    type="boolean",
                    description=(
                        "Optional, default false. Force-quit the app. Use only after a plain "
                        "close reported the app is still open and discarding unsaved work "
                        "is acceptable."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="close_tab",
            description=(
                "Close the front tab of a desktop application, for an app that also hosts "
                "the Test Runner (its terminal) or has other tabs the test still needs. "
                "Click the tab you opened first so it is in front; the tool refuses when "
                "another app is in front or when the front tab runs the Test Runner. "
                "Housekeeping: call it only after the step's expected result is verified; "
                "a failed close never fails the step."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="app_name",
                    type="string",
                    description="The human-readable name of the application whose front tab to close (e.g. 'Terminal').",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="check_app_installed",
            description=(
                "Check whether a software application is already installed on the remote machine. "
                "Searches both CLI tools (via PATH lookup) and GUI applications (via OS-native discovery). "
                "Returns 'INSTALLED: ...' or 'NOT INSTALLED: ...'. "
                "ALWAYS call this before attempting to install any software to avoid redundant installations."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="app_name",
                    type="string",
                    description="The name of the application to check (e.g. 'Firefox', 'git', 'Docker').",
                    required=True,
                ),
            ),
        ),
    )


def get_browser_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="browser_navigate",
            description="Navigate the browser to a URL.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="url",
                    type="string",
                    description="The URL to navigate to.",
                    required=True,
                ),
                ToolParameter(
                    name="browser",
                    type="string",
                    description=(
                        "Browser to open the page in. Only set this when the test "
                        "case names a browser; otherwise omit (Chromium). Changing "
                        "it closes the current browser, its page state and any "
                        "download still in progress."
                    ),
                    required=False,
                    enum=tuple(browser.value for browser in BrowserName),
                ),
            ),
        ),
        ToolDefinition(
            name="browser_click",
            description="Click a browser element found by AI-based natural-language description.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to click.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_double_click",
            description=(
                "Double-click a browser element found by AI-based natural-language description. "
                "Use it only when the step says double-click or the page needs one (e.g. edit "
                "a grid cell); never emulate it with two browser_click calls."
            ),
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to double-click.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_type",
            description="Type text into a browser element found by AI-based natural-language description.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to type into.",
                    required=True,
                ),
                ToolParameter(
                    name="text",
                    type="string",
                    description="The text to type into the element.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_hover",
            description="Hover over a browser element found by AI-based natural-language description.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to hover over.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_get_page_content",
            description="Get the text content of the current browser page.",
            category=ToolCategory.BROWSER,
            parameters=(),
        ),
        ToolDefinition(
            name="browser_get_url",
            description="Get the current URL of the browser.",
            category=ToolCategory.BROWSER,
            parameters=(),
        ),
        ToolDefinition(
            name="browser_take_screenshot",
            description="Take a screenshot of the browser and answer a question about it using vision AI.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="question",
                    type="string",
                    description="Question to answer about the browser screenshot.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_download",
            description="Download a file from a URL via the browser. Handles cookie-gated and auth-gated downloads.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="url",
                    type="string",
                    description="The direct download URL.",
                    required=True,
                ),
                ToolParameter(
                    name="save_path",
                    type="string",
                    description="Absolute path to save the file. Defaults to ~/Downloads/<suggested_filename>.",
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_list_downloads",
            description=(
                "List all downloads that occurred during this browser session. "
                "Blocks until every in-progress download finishes (up to 5 minutes). "
                "Use this after clicking a download link to confirm the file was saved."
            ),
            category=ToolCategory.BROWSER,
            parameters=(),
        ),
    )


def get_search_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="web_search",
            description="Search the web and return top results with titles, snippets, and URLs.",
            category=ToolCategory.SEARCH,
            parameters=(
                ToolParameter(
                    name="query",
                    type="string",
                    description="The search query.",
                    required=True,
                ),
            ),
        ),
    )


def get_utility_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="wait",
            description=(
                "Pause agent execution for a fixed number of seconds. Use after a UI "
                "action when an application, page, dialog, or download needs time to "
                "load. After waiting, inspect the UI to verify the expected result. "
                "Do not use this for shell processes; use wait_for_command instead."
            ),
            category=ToolCategory.UTILITY,
            parameters=(
                ToolParameter(
                    name="seconds",
                    type="number",
                    description=(
                        "How long to pause, in seconds. Must be greater than 0 and "
                        "at most 300. Fractional values such as 0.5 are allowed."
                    ),
                    required=True,
                ),
            ),
        ),
    )


def get_desktop_tool_definitions() -> tuple[ToolDefinition, ...]:
    """A desktop runner's tools (every runner before phone targets)."""
    return (
        get_controller_tool_definitions()
        + get_browser_tool_definitions()
        + get_search_tool_definitions()
        + get_utility_tool_definitions()
        + get_knowledge_tool_definitions()
    )


def get_all_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Every tool name of every target, once (desktop wording for the shared
    names). For vocabulary, never as a model's tool list: use
    ``tool_definitions_for``."""
    desktop = get_desktop_tool_definitions()
    desktop_names = {definition.name for definition in desktop}
    return desktop + tuple(
        definition
        for definition in get_mobile_tool_definitions()
        if definition.name not in desktop_names
    )


def tool_definitions_for(target: RunnerTarget) -> tuple[ToolDefinition, ...]:
    """The tools the model gets for a runner driving ``target``."""
    if target in MOBILE_TARGETS:
        return get_mobile_tool_definitions()
    return get_desktop_tool_definitions()


@cache
def tool_names_for(target: RunnerTarget) -> frozenset[str]:
    """The names ``dispatch_tool_call`` accepts for ``target``."""
    return frozenset(definition.name for definition in tool_definitions_for(target))


def get_knowledge_tool_definitions() -> tuple[ToolDefinition, ...]:
    """``search_knowledge`` and ``read_knowledge`` (category ``PROJECT_DATA``):
    read-only project data, here the members' knowledge base, so a tester or
    setup sub-agent can look up how to do a step. No ``list_knowledge``: the
    planner names the items a step needs. Allowlisted sub-agents (person-step
    confirm, teardown) pick tools by name and never get these."""
    return knowledge_tool_definitions(include_list=False)


# The keys key_press accepts on an Android device (one key per call).
MOBILE_KEYS: tuple[str, ...] = (
    "enter",
    "backspace",
    "delete",
    "tab",
    "space",
    "escape",
)

_PHONE_SCREENSHOT_NOTE = (
    " Coordinates are pixels of the phone screenshot, as take_screenshot reports them."
)


def _desktop_definition(name: str) -> ToolDefinition:
    for definition in get_controller_tool_definitions():
        if definition.name == name:
            return definition
    raise KeyError(name)


def _expected_result_parameter(action: str, example: str) -> ToolParameter:
    return ToolParameter(
        name="expected_result",
        type="string",
        description=(
            f"Optional. What the phone screen should show after the {action} "
            f"(e.g. '{example}'). The tool verifies the {action} against it and "
            "retries other candidates when it missed; when the intended element was "
            "acted on but the result differs, it stops and reports the action as "
            "performed but not confirmed."
        ),
        required=False,
    )


def _app_name_parameter(action: str) -> ToolParameter:
    return ToolParameter(
        name="app_name",
        type="string",
        description=(
            f"The app to {action}: its package name (Android, e.g. "
            "'com.android.settings') or bundle id (iOS, e.g. 'com.apple.Preferences'), "
            "or the name shown under its icon (e.g. 'Settings')."
        ),
        required=True,
    )


def get_mobile_tool_definitions() -> tuple[ToolDefinition, ...]:
    """A phone or tablet runner's tools (Android or iOS, device or emulator).

    The shared names keep their names (plans, close clauses, allowlists and
    digests work on both targets) with phone wording; ``hover``, ``close_tab``
    and the ``browser_*`` tools do not exist on a phone.
    """
    return (
        ToolDefinition(
            name="execute_command",
            description=(
                "Execute a shell command on the Test Runner COMPUTER the phone is "
                "connected to, never on the phone itself. Use it for adb (Android) or "
                "xcrun simctl / xcrun devicectl (iOS) with the device id in "
                "$RUNNER_DEVICE_ID, and for files and builds on the computer. "
                "Handles both interactive and non-interactive commands: if the "
                "command needs input it returns a session_id — use "
                "send_command_input to respond. Each command runs in a FRESH, HIDDEN "
                "shell session; use the cwd parameter instead of a separate cd. Its "
                "output (stdout/stderr, exit code) is returned in the tool result — "
                "read that instead of taking a screenshot."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=_desktop_definition("execute_command").parameters,
        ),
        _desktop_definition("wait_for_command"),
        _desktop_definition("send_command_input"),
        ToolDefinition(
            name="take_screenshot",
            description=(
                "Take a screenshot of the phone screen and answer a question about it "
                "using vision AI."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=_desktop_definition("take_screenshot").parameters,
        ),
        ToolDefinition(
            name="click",
            description=(
                "Tap an element on the phone screen found by vision-based "
                "natural-language description. The target element is verified by "
                "vision before tapping and the result is checked afterwards; the tool "
                "retries automatically with other candidates (up to 10) and returns an "
                "error if it cannot confirm the element, or an 'element not found' "
                "error once 3 candidates on the current screen have been rejected."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the element to tap.",
                    required=True,
                ),
                _expected_result_parameter("tap", "the Settings screen opens"),
            ),
        ),
        ToolDefinition(
            name="double_click",
            description=(
                "Double-tap an element on the phone screen found by vision-based "
                "description (zoom into a map or photo, select a word). Verified and "
                "retried like click. Use it only when the step says double-tap or the "
                "app needs one; never emulate it with two click calls."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description=(
                        "Natural language description of the element to double-tap."
                    ),
                    required=True,
                ),
                _expected_result_parameter("double-tap", "the photo is zoomed in"),
            ),
        ),
        ToolDefinition(
            name="click_at",
            description=(
                "Tap at pixel coordinates of the phone screenshot, for targets click "
                "cannot find by description: a blank area, the end of a text line. "
                "Get the coordinates from take_screenshot. Not verified unless "
                "expected_result is given, and never retried. Prefer click whenever "
                "the target can be described."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="x",
                    type="integer",
                    description="Horizontal pixel coordinate in the latest screenshot.",
                    required=True,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description="Vertical pixel coordinate in the latest screenshot.",
                    required=True,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after the tap. The "
                        "tool then compares the screen before and after once and "
                        "reports whether it is shown; it never taps again."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="long_press",
            description=(
                "Touch and hold an element on the phone screen found by vision-based "
                "description (context menus, rearranging icons, selecting text). "
                "Verified before and checked after like click, with automatic retries "
                "on other candidates."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description=(
                        "Natural language description of the element to press and hold."
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="duration",
                    type="number",
                    description=(
                        f"Optional. Seconds to hold, {LONG_PRESS_MIN_SECONDS:g}-"
                        f"{LONG_PRESS_MAX_SECONDS:g}, default "
                        f"{DEFAULT_LONG_PRESS_SECONDS:g}."
                    ),
                    required=False,
                ),
                _expected_result_parameter(
                    "long press", "a context menu with Delete appears"
                ),
            ),
        ),
        ToolDefinition(
            name="type_text",
            description=(
                "Type text into the focused field on the phone with its keyboard. Tap "
                "the field first so it has focus."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=_desktop_definition("type_text").parameters,
        ),
        ToolDefinition(
            name="key_press",
            description=(
                "Press one keyboard key on the phone: "
                f"{', '.join(MOBILE_KEYS)}. No key combinations or shortcuts. Use "
                "press_button for Back, Home and the other device buttons."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="keys",
                    type="string",
                    description="The key to press.",
                    required=True,
                    enum=MOBILE_KEYS,
                ),
            ),
        ),
        ToolDefinition(
            name="drag",
            description=(
                "Drag from one element to another on the phone screen (move an icon, "
                "a list item or a slider), both found by vision-based description and "
                "verified before dragging, with automatic retries. To scroll or "
                "page, use scroll or swipe instead."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="start_description",
                    type="string",
                    description=(
                        "Natural language description of the element to drag from."
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="end_description",
                    type="string",
                    description=(
                        "Natural language description of the element to drag to."
                    ),
                    required=True,
                ),
                _expected_result_parameter(
                    "drag", "the icon is in the second position"
                ),
            ),
        ),
        ToolDefinition(
            name="scroll",
            description=(
                "Scroll content on the phone (done as a swipe), for content outside "
                "the visible area. Give x and y of a point over the area to scroll "
                "(pixel coordinates from take_screenshot); without them it scrolls the "
                "middle of the screen. Take a screenshot afterwards to see the "
                "revealed content."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="direction",
                    type="string",
                    description=(
                        "Content direction: down reveals content below, up content "
                        "above, left and right content to the sides."
                    ),
                    required=True,
                    enum=tuple(direction.value for direction in ScrollDirection),
                ),
                ToolParameter(
                    name="amount",
                    type="integer",
                    description=(
                        f"How far, 1-{MAX_SCROLL_AMOUNT}. Optional, default "
                        f"{DEFAULT_SCROLL_AMOUNT}."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="x",
                    type="integer",
                    description=(
                        "Optional. Horizontal pixel coordinate of a point over the "
                        "area to scroll; give it together with y."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description=(
                        "Optional. Vertical pixel coordinate of a point over the area "
                        "to scroll; give it together with x."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="swipe",
            description=(
                "Swipe a finger across the phone screen: switch pages or tabs, "
                "dismiss a card, pull down to refresh, open the notification shade. "
                "direction is the way the FINGER moves: 'up' moves the finger up and "
                "reveals content below; 'left' shows the next page. Not verified "
                "unless expected_result is given, and never retried."
                + _PHONE_SCREENSHOT_NOTE
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="direction",
                    type="string",
                    description="The direction the finger moves.",
                    required=True,
                    enum=tuple(direction.value for direction in ScrollDirection),
                ),
                ToolParameter(
                    name="x",
                    type="integer",
                    description=(
                        "Optional. Horizontal pixel coordinate where the swipe starts; "
                        "give it together with y. Omitted: the centre of the screen."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description=(
                        "Optional. Vertical pixel coordinate where the swipe starts; "
                        "give it together with x."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="distance",
                    type="number",
                    description=(
                        "Optional. Swipe length as a share of the screen side along "
                        f"the swipe, {SWIPE_MIN_DISTANCE:g}-{SWIPE_MAX_DISTANCE:g}, "
                        f"default {DEFAULT_SWIPE_DISTANCE:g}."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="duration",
                    type="number",
                    description=(
                        f"Optional. Seconds the swipe takes, {SWIPE_MIN_DURATION:g}-"
                        f"{SWIPE_MAX_DURATION:g}, default {DEFAULT_SWIPE_DURATION:g}. "
                        "Short is a fling, long a slow drag."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after the swipe. The "
                        "tool then compares the screen before and after once and "
                        "reports whether it is shown; it never swipes again."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="press_button",
            description=(
                "Press a device button: back (Android only), home, app_switch "
                "(recent apps), volume_up, volume_down or power. Take a screenshot "
                "afterwards to see the result."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="button",
                    type="string",
                    description="The button to press.",
                    required=True,
                    enum=tuple(button.value for button in MobileButton),
                ),
            ),
        ),
        ToolDefinition(
            name="rotate_device",
            description=(
                "Rotate the phone to portrait or landscape. Screenshot sizes and "
                "coordinates change with the orientation: take a new screenshot "
                "before tapping. Not verified unless expected_result is given."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="orientation",
                    type="string",
                    description="The orientation to rotate to.",
                    required=True,
                    enum=tuple(o.value for o in DeviceOrientation),
                ),
                ToolParameter(
                    name="expected_result",
                    type="string",
                    description=(
                        "Optional. What the screen should show after rotating (e.g. "
                        "'the video fills the screen'). Checked once, never retried."
                    ),
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="hide_keyboard",
            description=(
                "Hide the on-screen keyboard when it covers what you need to see or "
                "tap. Harmless when no keyboard is shown."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(),
        ),
        ToolDefinition(
            name="launch_app",
            description=(
                "Open an app on the phone. Prefer its package name (Android) or bundle "
                "id (iOS); a visible name is matched against the installed apps, and "
                "when no confident match is found the tool returns similar names."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(_app_name_parameter("open"),),
        ),
        ToolDefinition(
            name="close_app",
            description=(
                "Close (terminate) an app on the phone the test no longer needs. "
                "Housekeeping: call it only after the step's expected result is "
                "verified; a failed close never fails the step. Never for the app "
                "under test while the step still uses it."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(_app_name_parameter("close"),),
        ),
        ToolDefinition(
            name="check_app_installed",
            description=(
                "Check whether an app is installed on the phone. Returns "
                "'INSTALLED: ...' or 'NOT INSTALLED: ...'. Call it before installing "
                "an app."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(_app_name_parameter("check"),),
        ),
        ToolDefinition(
            name="open_url",
            description=(
                "Open a URL on the phone: an http(s) link opens in the phone's default "
                "browser, an app link or custom scheme (deep link) opens the app that "
                "handles it. Then use take_screenshot and click (taps) on the page."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="url",
                    type="string",
                    description="The URL or deep link to open.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="install_app",
            description=(
                "Install an app build that is a file on the Test Runner computer onto "
                "the phone: an .apk (Android), a simulator .app or a device-signed "
                ".ipa (iOS). Installing can take minutes. Use check_app_installed "
                "first."
            ),
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="path",
                    type="string",
                    description=(
                        "Absolute path of the build on the Test Runner computer."
                    ),
                    required=True,
                ),
            ),
        ),
    ) + (
        get_search_tool_definitions()
        + get_utility_tool_definitions()
        + get_knowledge_tool_definitions()
    )
