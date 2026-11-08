"""The machine tools an MCP client may use inside its own claimed Manual case
(docs/plan/feat-mcp-runner-tools.md): the one table of their names, the
agent tool each one dispatches to, its throttle class and where it runs.

The tool list is static (one connection can span desktop and phone
projects); ``targets`` decides, per call, whether the project's test machine
can run the tool (``[wrong_target]`` otherwise, before any round trip).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from controller_client.protocol import MOBILE_TARGETS, RunnerTarget
from mcp_gateway.services.call_guard import CallKind

_ALL: Final[frozenset[RunnerTarget]] = frozenset(RunnerTarget)
_DESKTOP: Final[frozenset[RunnerTarget]] = frozenset({RunnerTarget.DESKTOP})
_PHONE: Final[frozenset[RunnerTarget]] = MOBILE_TARGETS


@dataclass(frozen=True)
class MachineTool:
    # The MCP tool name.
    name: str
    # The agent tool registry name it dispatches to; None for the command
    # tools (command services), take_screenshot (machine_screenshot),
    # web_search (its snippets-only search) and wait (the handler sleeps).
    agent_tool: str | None
    # The throttle class (``click_at`` / ``swipe`` / ``rotate_device`` count
    # as AI instead when ``ai_with_expected_result`` and an expected_result
    # is given).
    kind: CallKind
    # Where the tool runs.
    targets: frozenset[RunnerTarget]
    # False for web_search and wait: claim check only, no test machine.
    needs_runner: bool = True
    # Finds elements by description on a screen parse: needs a ready worker
    # of the visual engine.
    uses_visual_engine: bool = False
    # Counts as AI when called with an expected_result.
    ai_with_expected_result: bool = False


def _tool(
    name: str,
    kind: CallKind,
    targets: frozenset[RunnerTarget],
    *,
    dispatches: bool = True,
    needs_runner: bool = True,
    uses_visual_engine: bool = False,
    ai_with_expected_result: bool = False,
) -> MachineTool:
    """A table row; a dispatching tool has the agent tool of its own name."""
    return MachineTool(
        name=name,
        agent_tool=name if dispatches else None,
        kind=kind,
        targets=targets,
        needs_runner=needs_runner,
        uses_visual_engine=uses_visual_engine,
        ai_with_expected_result=ai_with_expected_result,
    )


_TOOLS: Final[tuple[MachineTool, ...]] = (
    # Commands (desktop and phone; a phone runs them on its host computer).
    _tool("execute_command", CallKind.COMMAND, _ALL, dispatches=False),
    _tool("read_command_output", CallKind.COMMAND, _ALL, dispatches=False),
    _tool("send_command_input", CallKind.COMMAND, _ALL, dispatches=False),
    _tool("stop_command", CallKind.COMMAND, _ALL, dispatches=False),
    _tool("list_commands", CallKind.COMMAND, _ALL, dispatches=False),
    # Screen and input.
    _tool("take_screenshot", CallKind.SCREEN, _ALL, dispatches=False),
    _tool("click", CallKind.AI, _ALL, uses_visual_engine=True),
    _tool("double_click", CallKind.AI, _ALL, uses_visual_engine=True),
    _tool("click_at", CallKind.CONTROL, _ALL, ai_with_expected_result=True),
    _tool("type_text", CallKind.CONTROL, _ALL),
    _tool("key_press", CallKind.CONTROL, _ALL),
    _tool("hover", CallKind.AI, _DESKTOP, uses_visual_engine=True),
    _tool("drag", CallKind.AI, _ALL, uses_visual_engine=True),
    _tool("scroll", CallKind.CONTROL, _ALL),
    # Apps.
    _tool("launch_app", CallKind.CONTROL, _ALL),
    _tool("close_app", CallKind.CONTROL, _ALL),
    _tool("check_app_installed", CallKind.CONTROL, _ALL),
    _tool("close_tab", CallKind.CONTROL, _DESKTOP),
    # Browser (desktop).
    _tool("browser_navigate", CallKind.CONTROL, _DESKTOP),
    _tool("browser_click", CallKind.AI, _DESKTOP),
    _tool("browser_double_click", CallKind.AI, _DESKTOP),
    _tool("browser_type", CallKind.AI, _DESKTOP),
    _tool("browser_hover", CallKind.AI, _DESKTOP),
    _tool("browser_get_page_content", CallKind.CONTROL, _DESKTOP),
    _tool("browser_get_url", CallKind.CONTROL, _DESKTOP),
    _tool("browser_download", CallKind.CONTROL, _DESKTOP),
    _tool("browser_list_downloads", CallKind.CONTROL, _DESKTOP),
    # Phone.
    _tool("long_press", CallKind.AI, _PHONE, uses_visual_engine=True),
    _tool("swipe", CallKind.CONTROL, _PHONE, ai_with_expected_result=True),
    _tool("press_button", CallKind.CONTROL, _PHONE),
    _tool("rotate_device", CallKind.CONTROL, _PHONE, ai_with_expected_result=True),
    _tool("hide_keyboard", CallKind.CONTROL, _PHONE),
    _tool("open_url", CallKind.CONTROL, _PHONE),
    _tool("install_app", CallKind.CONTROL, _PHONE),
    # No test machine.
    # web_search runs the snippets-only search (run_web_search), not the
    # agent's dispatch, which also fetches pages from the server.
    _tool("web_search", CallKind.AI, _ALL, dispatches=False, needs_runner=False),
    # wait sleeps on the event loop after the gate (machine_handlers.wait).
    _tool("wait", CallKind.CONTROL, _ALL, dispatches=False, needs_runner=False),
)


def effective_kind(tool: MachineTool, expected_result: object) -> CallKind:
    """``tool``'s throttle and budget class: AI for a coordinate action
    called with a non-empty expected_result (a vision check), else its own.
    The one rule for the handler's throttle and ``run_agent_tool``'s
    budget."""
    if tool.ai_with_expected_result and expected_result:
        return CallKind.AI
    return tool.kind


MACHINE_TOOLS: Final[Mapping[str, MachineTool]] = MappingProxyType(
    {tool.name: tool for tool in _TOOLS}
)
assert len(MACHINE_TOOLS) == len(_TOOLS) == 36, "machine tool table is not 36 tools"

# What ``allowlisted_dispatcher`` lets the machine tools reach: every agent
# tool of the table (never wait_for_command or browser_take_screenshot).
MCP_AGENT_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    tool.agent_tool for tool in _TOOLS if tool.agent_tool is not None
)
