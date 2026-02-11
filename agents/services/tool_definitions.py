from __future__ import annotations

from agents.types import ToolCategory, ToolDefinition, ToolParameter


def get_shell_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="execute_command",
            description="Execute a shell command in the container via SSH. Returns stdout, stderr, and exit code.",
            category=ToolCategory.SHELL,
            parameters=(
                ToolParameter(
                    name="command",
                    type="string",
                    description="The shell command to execute.",
                    required=True,
                ),
            ),
        ),
    )


def get_screen_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="take_screenshot",
            description="Take a screenshot of the desktop and answer a question about it.",
            category=ToolCategory.SCREEN,
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
            name="screen_click",
            description="Click at a specific position on the desktop.",
            category=ToolCategory.SCREEN,
            parameters=(
                ToolParameter(
                    name="x",
                    type="integer",
                    description="X coordinate to click.",
                    required=True,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description="Y coordinate to click.",
                    required=True,
                ),
                ToolParameter(
                    name="button",
                    type="integer",
                    description="Mouse button (1=left, 2=middle, 3=right).",
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="screen_type_text",
            description="Type text on the desktop using the keyboard.",
            category=ToolCategory.SCREEN,
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
            name="screen_key_press",
            description="Press a key or key combination (e.g., 'Return', 'ctrl+c', 'alt+F4').",
            category=ToolCategory.SCREEN,
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
            name="screen_list_windows",
            description="List all open windows on the desktop.",
            category=ToolCategory.SCREEN,
            parameters=(),
        ),
        ToolDefinition(
            name="screen_get_active_window",
            description="Get the name of the currently active window.",
            category=ToolCategory.SCREEN,
            parameters=(),
        ),
    )


def get_browser_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="browser_navigate",
            description="Navigate to a URL in the browser. Returns the page title and a visual description of the page.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="url",
                    type="string",
                    description="URL to navigate to.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_click",
            description="Click an element in the browser by natural-language description.",
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
            name="browser_type",
            description="Type text into a form element found by natural-language description.",
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
                    description="Text to type.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_hover",
            description="Hover over an element found by natural-language description.",
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
            parameters=(
                ToolParameter(
                    name="max_length",
                    type="integer",
                    description="Maximum length of content to return.",
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_get_url",
            description="Get the current URL of the browser.",
            category=ToolCategory.BROWSER,
            parameters=(),
        ),
        ToolDefinition(
            name="browser_take_screenshot",
            description="Take a screenshot of the browser and answer a question about it.",
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
    )


def get_vnc_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="vnc_take_screenshot",
            description="Capture the VNC desktop framebuffer and answer a question about it using vision AI.",
            category=ToolCategory.VNC,
            parameters=(
                ToolParameter(
                    name="question",
                    type="string",
                    description="Question to answer about the VNC screenshot.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="vnc_click",
            description="Click an element on the VNC desktop found by vision-based natural-language description.",
            category=ToolCategory.VNC,
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
            name="vnc_type",
            description="Find an input element on the VNC desktop by description, click to focus, then type text.",
            category=ToolCategory.VNC,
            parameters=(
                ToolParameter(
                    name="description",
                    type="string",
                    description="Natural language description of the input element.",
                    required=True,
                ),
                ToolParameter(
                    name="text",
                    type="string",
                    description="Text to type into the element.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="vnc_hover",
            description="Hover over an element on the VNC desktop found by vision-based natural-language description.",
            category=ToolCategory.VNC,
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
            name="vnc_key_press",
            description="Press a key or key combination via VNC (e.g., 'Return', 'ctrl-a', 'alt-F4').",
            category=ToolCategory.VNC,
            parameters=(
                ToolParameter(
                    name="keys",
                    type="string",
                    description="Key or key combination to press (X11 keysym names).",
                    required=True,
                ),
            ),
        ),
    )


def get_all_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        *get_shell_tool_definitions(),
        *get_screen_tool_definitions(),
        *get_browser_tool_definitions(),
        *get_vnc_tool_definitions(),
    )
