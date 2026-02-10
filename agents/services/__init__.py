from agents.services.agent_loop import (
    build_agent_config,
    build_system_prompt,
    run_agent,
)
from agents.services.dmr_client import (
    build_dmr_config,
    build_vision_dmr_config,
    ensure_model_available,
    is_model_available,
    list_models,
    pull_model,
    send_chat_completion,
)
from agents.services.tool_registry import dispatch_tool_call, get_all_tool_definitions
from agents.services.tools_browser import (
    browser_click,
    browser_get_page_content,
    browser_get_url,
    browser_navigate,
    browser_take_screenshot,
    browser_type,
)
from agents.services.tools_screen import (
    screen_click,
    screen_get_active_window,
    screen_key_press,
    screen_list_windows,
    screen_type_text,
    take_screenshot,
)
from agents.services.tools_shell import execute_command

__all__ = [
    "browser_click",
    "browser_get_page_content",
    "browser_get_url",
    "browser_navigate",
    "browser_take_screenshot",
    "browser_type",
    "build_agent_config",
    "build_dmr_config",
    "build_system_prompt",
    "build_vision_dmr_config",
    "ensure_model_available",
    "dispatch_tool_call",
    "execute_command",
    "get_all_tool_definitions",
    "is_model_available",
    "list_models",
    "pull_model",
    "run_agent",
    "screen_click",
    "screen_get_active_window",
    "screen_key_press",
    "screen_list_windows",
    "screen_type_text",
    "send_chat_completion",
    "take_screenshot",
]
