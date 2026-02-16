from __future__ import annotations

from agents.types import ToolCategory, ToolDefinition, ToolParameter


def get_controller_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="execute_command",
            description="Execute a shell command in the container. Returns stdout, stderr, and exit code.",
            category=ToolCategory.CONTROLLER,
            parameters=(
                ToolParameter(
                    name="command",
                    type="string",
                    description="The shell command to execute.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="take_screenshot",
            description="Take a screenshot of the desktop and answer a question about it using vision AI.",
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
            description="Click an element on the desktop found by vision-based natural-language description.",
            category=ToolCategory.CONTROLLER,
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
            name="type_text",
            description="Type text using the keyboard.",
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
            description="Hover over an element on the desktop found by vision-based natural-language description.",
            category=ToolCategory.CONTROLLER,
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
            name="drag",
            description="Drag from one element to another on the desktop, both found by vision-based description.",
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


def get_all_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        get_controller_tool_definitions()
        + get_browser_tool_definitions()
        + get_search_tool_definitions()
    )
