"""The refusals of the machine tools inside a claimed case.

Like ``capture_errors``: each text starts with a stable ``[code]`` the
client can branch on, and speaks of "the test machine" only, never the
program that runs on it, an agent or an AI (the no trace rule). The codes
the capture tools share keep their values (``CaptureErrorCode``); texts that
fit both reuse the capture table, the others are worded for actions rather
than captures (the capture texts point to add_screenshot).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from agents.exceptions import AIServiceError
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.services.capture_errors import CaptureErrorCode
from mcp_gateway.services.capture_errors import error as capture_error
from projects.call_deadline import CallDeadlineExceededError
from projects.exceptions import (
    BrowserUnavailableError,
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
    DeviceDisconnectedError,
    KeptSessionLimitError,
    RunnerOutdatedError,
    TestRunnerDisconnectedError,
    TestRunnerUnavailableError,
    TestRunnerUnresponsiveError,
    VisualEngineError,
)


class MachineErrorCode(StrEnum):
    # Shared with the capture tools (same values as ``CaptureErrorCode``).
    MACHINE_OFFLINE = CaptureErrorCode.MACHINE_OFFLINE.value
    MACHINE_OUTDATED = CaptureErrorCode.MACHINE_OUTDATED.value
    DEVICE_OFFLINE = CaptureErrorCode.DEVICE_OFFLINE.value
    MACHINE_BUSY = CaptureErrorCode.MACHINE_BUSY.value
    MACHINE_UNRESPONSIVE = CaptureErrorCode.MACHINE_UNRESPONSIVE.value
    # The machine tools' own.
    WRONG_TARGET = "wrong_target"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    VISUAL_ENGINE_OFFLINE = "visual_engine_offline"
    AI_UNAVAILABLE = "ai_unavailable"
    COMMAND_NOT_FOUND = "command_not_found"
    TOO_MANY_COMMANDS = "too_many_commands"
    SEARCH_BUSY = "search_busy"
    SEARCH_FAILED = "search_failed"


# Codes whose capture text fits an action as well.
_CAPTURE_TEXT: Final[frozenset[MachineErrorCode]] = frozenset(
    {MachineErrorCode.DEVICE_OFFLINE}
)
_FIXED: Final[dict[MachineErrorCode, str]] = {
    MachineErrorCode.MACHINE_OFFLINE: (
        "The test machine is not connected right now. Try again later."
    ),
    MachineErrorCode.MACHINE_OUTDATED: (
        "The test machine's software is too old for this action. Ask a project "
        "member to update the test machine's software from the Environment page."
    ),
    MachineErrorCode.MACHINE_BUSY: (
        "The test machine is busy with another action or screenshot. Try again "
        "in a few seconds."
    ),
    MachineErrorCode.MACHINE_UNRESPONSIVE: (
        "The test machine did not answer in time. Take a screenshot to see "
        "whether the action happened before you try again."
    ),
    MachineErrorCode.WRONG_TARGET: (
        "This tool does not exist for this project's test machine (a desktop "
        "computer, or a phone or tablet)."
    ),
    MachineErrorCode.BROWSER_UNAVAILABLE: (
        "That browser cannot be used on the test machine."
    ),
    MachineErrorCode.VISUAL_ENGINE_OFFLINE: (
        "Finding elements by description is not available right now. Use "
        "take_screenshot and click_at with coordinates instead, or try again "
        "later."
    ),
    MachineErrorCode.AI_UNAVAILABLE: (
        "The service that reads the screen and the web is not available right "
        "now. Use take_screenshot and click_at with coordinates instead, or try "
        "again later."
    ),
    MachineErrorCode.COMMAND_NOT_FOUND: (
        "No command with this session_id belongs to your claimed case (it ended, "
        "was stopped, or was started under another claim). Call list_commands "
        "for your commands."
    ),
    MachineErrorCode.TOO_MANY_COMMANDS: (
        "Too many commands are still running for this case. Stop one with "
        "stop_command (list_commands shows them) and try again."
    ),
    MachineErrorCode.SEARCH_BUSY: (
        "Too many web searches are running right now. Try again in a few " "seconds."
    ),
    MachineErrorCode.SEARCH_FAILED: (
        "The web search did not work this time. Try again later or rephrase "
        "the query."
    ),
}

# "Test Runner(s)" (also test_runner, test-runner) and "controller(s)"
# with an optional "client(s)", with an optional "the", as whole words (not
# inside a hyphenated or underscored word), in any case.
# The rest of a hyphen/word token reaching an extension or a path
# separator makes it a file name or path (agent-v2.exe, ai-tools/run.sh).
_NOT_A_PATH: Final = r"(?![-\w]*(?:[/\\]|\.\w))"

_RUNNER_NAME: Final = re.compile(
    r"(?<![-\w/\\.])(?:(?P<the>the)\s+)?"
    # Atomic: "controller client.py" must not fall back to "controller".
    r"(?P<name>(?>test[\s_-]*runners?|controllers?(?:[\s_-]*clients?)?))"
    + _NOT_A_PATH
    + r"(?![-\w])",
    re.IGNORECASE,
)


# The server's helpers (screen parser, AI models, the agent), with an
# optional leading article, as whole words, in any case.
_HELPER_NAME: Final = re.compile(
    # Not inside a path or file name (/home/agent/, agent.py, C:\ai\).
    r"(?<![-\w/\\.])(?P<art>(?:the|an?)\s+)?"
    r"(?>omniparser"
    r"|vision(?:\s+(?:models?|checks?))?"
    r"|llms?"
    r"|ais?"
    # "user agent" is the browser's, not the server's.
    r"|(?<!user\s)(?<!user\s\s)(?<!user-)agents?)" + _NOT_A_PATH
    # A hyphenated word goes on (AI-generated), a plain word may not.
    + r"(?![\w])",
    re.IGNORECASE,
)


def machine_error(code: MachineErrorCode, detail: str = "") -> McpToolError:
    """The tool error ``[code] text``; ``detail`` (server or neutralised
    machine text) follows the table's text."""
    if code in _CAPTURE_TEXT:
        base = capture_error(CaptureErrorCode(code.value)).message
    else:
        base = f"[{code.value}] {_FIXED[code]}"
    return McpToolError(f"{base} {detail}" if detail else base)


def map_machine_exception(exc: Exception) -> McpToolError | None:
    """The coded tool error of an exception a machine action raised, or None
    when it is not one of the infrastructure or controller faults below."""
    if isinstance(exc, KeptSessionLimitError):
        return machine_error(MachineErrorCode.TOO_MANY_COMMANDS)
    if isinstance(exc, DeviceDisconnectedError):
        return machine_error(MachineErrorCode.DEVICE_OFFLINE)
    if isinstance(
        exc,
        (
            ControllerNotConnectedError,
            ControllerReconnectTimeoutError,
            TestRunnerDisconnectedError,
        ),
    ):
        return machine_error(MachineErrorCode.MACHINE_OFFLINE)
    if isinstance(
        exc,
        (
            TestRunnerUnresponsiveError,
            TestRunnerUnavailableError,
            CallDeadlineExceededError,
        ),
    ):
        return machine_error(MachineErrorCode.MACHINE_UNRESPONSIVE)
    if isinstance(exc, RunnerOutdatedError):
        return machine_error(MachineErrorCode.MACHINE_OUTDATED)
    if isinstance(exc, BrowserUnavailableError):
        # ``safe_message`` is server text; ``detail`` (runner text) stays in
        # the server log.
        return machine_error(
            MachineErrorCode.BROWSER_UNAVAILABLE,
            neutral_machine_text(exc.safe_message),
        )
    if isinstance(exc, VisualEngineError):
        return machine_error(MachineErrorCode.VISUAL_ENGINE_OFFLINE)
    if isinstance(exc, AIServiceError):
        return machine_error(MachineErrorCode.AI_UNAVAILABLE)
    return None


def neutral_machine_text(text: str) -> str:
    """``text`` with the program on the test machine named "the test
    machine" ("Test Runner", "controller") and the server's helpers in
    neutral words: OmniParser "the screen reader", a vision model, LLM or
    AI "the screen check", an agent "the tester". Whole words only, in any
    case ("said" or "maintain" stay as they are)."""

    def _runner(match: re.Match[str]) -> str:
        plural = match.group("name").lower().endswith("s")
        machine = "test machines" if plural else "test machine"
        the = match.group("the")
        if the is not None:
            return ("The" if the[0] == "T" else "the") + " " + machine
        return _sentence_case(match, machine)

    def _helper(match: re.Match[str]) -> str:
        word = match.group(0)
        lowered = word.lower()
        if "omniparser" in lowered:
            replacement = "the screen reader"
        elif "agent" in lowered:
            replacement = "the testers" if lowered.endswith("s") else "the tester"
        elif lowered.endswith("s") and not lowered.endswith("ai"):
            replacement = "the screen checks"
        else:
            replacement = "the screen check"
        if match.group("art") is None:
            # No article in the source: drop ours where it reads as a name.
            replacement = replacement.removeprefix("the ")
        return _sentence_case(match, replacement)

    return _HELPER_NAME.sub(_helper, _RUNNER_NAME.sub(_runner, text))


def _sentence_case(match: re.Match[str], replacement: str) -> str:
    """``replacement`` capitalised when ``match`` starts a sentence."""
    before = match.string[: match.start()].rstrip()
    if not before or before[-1] in ".!?:\n":
        return replacement[0].upper() + replacement[1:]
    return replacement
