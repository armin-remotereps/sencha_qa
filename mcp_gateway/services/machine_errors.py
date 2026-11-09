"""The refusals of the machine tools inside a claimed case.

Like ``capture_errors``: each text starts with a stable ``[code]`` the
client can branch on, and speaks of "the test machine" only, never the
program that runs on it, an agent or an AI (the no trace rule). The codes
the capture tools share keep their values (``CaptureErrorCode``); texts that
fit both reuse the capture table, the others are worded for actions rather
than captures (the capture texts point to add_screenshot).
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Final

from agents.exceptions import AIServiceError
from controller_client.protocol import (
    ErrorCode,
    RunnerTarget,
    WebBrowser,
    WebBrowserSetupStep,
)
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.services.capture_errors import CaptureErrorCode
from mcp_gateway.services.capture_errors import error as capture_error
from projects.call_deadline import CallDeadlineExceededError
from projects.exceptions import (
    BrowserUnavailableError,
    ControllerActionError,
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
    DeviceDisconnectedError,
    KeptSessionLimitError,
    RunnerOutdatedError,
    TestRunnerDisconnectedError,
    TestRunnerUnavailableError,
    TestRunnerUnresponsiveError,
    VisualEngineError,
    WebBrowserAmbiguousError,
    WebBrowserNotOpenError,
    WebBrowserScriptError,
    WebBrowserSetupNeededError,
    WebBrowserUnsupportedError,
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
    # The browse_* tools' own.
    BROWSER_SETUP_NEEDED = "browser_setup_needed"
    BROWSER_ACTION_UNSUPPORTED = "browser_action_unsupported"
    ELEMENT_NOT_FOUND = "element_not_found"
    SCRIPT_FAILED = "script_failed"
    BROWSER_NOT_OPEN = "browser_not_open"
    BROWSER_AMBIGUOUS = "browser_ambiguous"
    BROWSER_ACTION_FAILED = "browser_action_failed"
    BROWSER_OUT_OF_TIME = "browser_out_of_time"


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
        "take_screenshot and click_at and the other *_at tools with "
        "coordinates instead, or try again later."
    ),
    MachineErrorCode.AI_UNAVAILABLE: (
        "The service that reads the screen and the web is not available right "
        "now. Use take_screenshot and click_at and the other *_at tools with "
        "coordinates instead, or try again later."
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
    MachineErrorCode.BROWSER_SETUP_NEEDED: (
        "The browser on the test machine needs a one-time setup first."
    ),
    MachineErrorCode.BROWSER_ACTION_UNSUPPORTED: (
        "This action is not available for this browser on the test machine "
        "(for example hover on a phone, or selectors, scripts, tabs and "
        "waits in accessibility mode). Use browse_list_elements and an index "
        "instead."
    ),
    MachineErrorCode.ELEMENT_NOT_FOUND: (
        "No single element matched. Call browse_list_elements again (indexes "
        "change when the page changes) and use an index, a selector, or a "
        "more precise description."
    ),
    MachineErrorCode.SCRIPT_FAILED: (
        "The script failed or ran out of time in the test machine's browser."
    ),
    MachineErrorCode.BROWSER_NOT_OPEN: (
        "No browser is open on the test machine. Call browse_list_browsers, "
        "then open one with browse_open, or ask the person at the test "
        "machine to open one."
    ),
    MachineErrorCode.BROWSER_AMBIGUOUS: (
        "Several browsers are open on the test machine. Name one with the "
        "browser argument."
    ),
    MachineErrorCode.BROWSER_ACTION_FAILED: (
        "The browser action failed on the test machine."
    ),
    MachineErrorCode.BROWSER_OUT_OF_TIME: (
        "Too little time was left in this call for the browser action, so it "
        "was not sent to the test machine. Try again; an index or a selector "
        "is faster than a description."
    ),
}
# A browse_* request the test machine refused as malformed (INVALID_MESSAGE)
# or the server refused before sending: fixed text, never the runner's.
BROWSE_INVALID_REQUEST: Final = (
    "[invalid] The test machine's browser cannot take this request. Check the "
    "URL (http, https, about or file), the selectors and the key."
)
# Longest runner text kept in the server log for a failed browse_* action.
_LOG_TEXT_MAX_CHARS: Final = 300

logger = logging.getLogger(__name__)

# browser_setup_needed: what the person at the test machine must set up.
_SETUP_STEP_TEXT: Final[dict[WebBrowserSetupStep, str]] = {
    WebBrowserSetupStep.SAFARI_REMOTE_AUTOMATION_OFF: (
        "Safari does not allow remote automation yet. Ask the person at the "
        "test machine to turn on Allow Remote Automation in Safari's "
        "Developer settings (or run safaridriver --enable once), then try "
        "again."
    ),
    WebBrowserSetupStep.IOS_WEB_INSPECTOR_OFF: (
        "Web Inspector is off on the phone. Ask the person at the test "
        "machine to turn it on in Settings > Apps > Safari > Advanced > Web "
        "Inspector, then try again."
    ),
    WebBrowserSetupStep.CHROME_IOS_WEB_INSPECTOR_OFF: (
        "Chrome on the phone does not allow inspection. Ask the person at the "
        "test machine to turn on Web Inspector in Chrome's own settings "
        "(Settings > Content Settings > Web Inspector), then try again."
    ),
    WebBrowserSetupStep.DRIVER_UNAVAILABLE: (
        "The test machine could not get the driver for this browser (no "
        "network, or downloads are off and none is stored). Ask the person at "
        "the test machine to check its network or set up the browser driver, "
        "then try again."
    ),
    WebBrowserSetupStep.ANDROID_DEVTOOLS_UNAVAILABLE: (
        "The browser on the phone cannot be inspected right now. Make sure USB "
        "debugging is on and the browser is open on the phone, then try again."
    ),
    WebBrowserSetupStep.FIREFOX_ANDROID_REMOTE_DEBUGGING_OFF: (
        "Firefox on the phone does not allow remote debugging. Ask the person "
        "at the test machine to turn on Remote debugging via USB in Firefox's "
        "settings, then try again."
    ),
}
_SETUP_STEP_UNKNOWN: Final = (
    "Call browse_list_browsers, or ask the person at the test machine to "
    "check the browser."
)
# browser_not_open, a browser named: how it can be opened (fixed texts).
_NOT_OPEN_NAMED: Final = "No {browser} is open on the test machine."
_OPEN_WITH_SHORTCUT: Final = (
    "Open it with browse_open, or ask the person at the test machine to open "
    "it with the test browser shortcut (a browser opened any other way cannot "
    "be used)."
)
_OPEN_APP: Final = (
    "Open it with browse_open, or ask the person at the test machine to open "
    "the app on the phone."
)
_OPEN_TOOL_ONLY: Final = (
    "Only browse_open can open it; a window the person opened cannot be used."
)

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


def map_browse_exception(
    exc: Exception, *, runner_target: RunnerTarget, os_name: str
) -> McpToolError | None:
    """The coded tool error of an exception a browse_* call raised, or None
    when it is not a controller or infrastructure fault. The runner's text
    is never forwarded: fixed texts, built from enum values only; any other
    ``ControllerActionError`` is ``[browser_action_failed]`` and INVALID_MESSAGE
    ``[invalid]``, with the runner's text cut short in the server log."""
    if isinstance(exc, WebBrowserSetupNeededError):
        step = exc.setup_step
        detail = _SETUP_STEP_UNKNOWN if step is None else _SETUP_STEP_TEXT[step]
        return machine_error(MachineErrorCode.BROWSER_SETUP_NEEDED, detail)
    if isinstance(exc, WebBrowserUnsupportedError):
        return machine_error(MachineErrorCode.BROWSER_ACTION_UNSUPPORTED)
    if isinstance(exc, WebBrowserScriptError):
        return machine_error(
            MachineErrorCode.SCRIPT_FAILED, f"Error class: {exc.error_class}."
        )
    if isinstance(exc, WebBrowserNotOpenError):
        return browser_not_open_error(
            exc.named, runner_target=runner_target, os_name=os_name
        )
    if isinstance(exc, WebBrowserAmbiguousError):
        return browser_ambiguous_error(exc.open_browsers)
    if type(exc) is ControllerActionError and _has_error_code(
        exc, ErrorCode.ELEMENT_NOT_FOUND
    ):
        # ``_raise_for_error_reply`` raises the plain class with the code in
        # its message; the runner's text is dropped.
        return machine_error(MachineErrorCode.ELEMENT_NOT_FOUND)
    if type(exc) is ControllerActionError and _has_error_code(
        exc, ErrorCode.INVALID_MESSAGE
    ):
        _log_browse_failure(exc)
        return McpToolError(BROWSE_INVALID_REQUEST)
    mapped = map_machine_exception(exc)
    if mapped is not None:
        return mapped
    if isinstance(exc, ControllerActionError):
        # EXECUTION_FAILED, an unparsable or unsuccessful reply, any other
        # runner refusal: a fixed text; the runner's stays in the server log.
        _log_browse_failure(exc)
        return machine_error(MachineErrorCode.BROWSER_ACTION_FAILED)
    return None


def _log_browse_failure(exc: Exception) -> None:
    text = " ".join(str(exc).split())
    if len(text) > _LOG_TEXT_MAX_CHARS:
        text = text[:_LOG_TEXT_MAX_CHARS] + "..."
    logger.warning("A browse action failed on the test machine: %s", text)


def browser_not_open_error(
    named: WebBrowser | None, *, runner_target: RunnerTarget, os_name: str
) -> McpToolError:
    """``[browser_not_open]``: the general text without a browser named,
    else how the named browser can be opened on this test machine."""
    if named is None:
        return machine_error(MachineErrorCode.BROWSER_NOT_OPEN)
    head = _NOT_OPEN_NAMED.format(browser=named.value)
    if (runner_target == RunnerTarget.DESKTOP and named == WebBrowser.SAFARI) or (
        runner_target == RunnerTarget.ANDROID and named == WebBrowser.FIREFOX
    ):
        how = _OPEN_TOOL_ONLY
    elif runner_target == RunnerTarget.DESKTOP:
        how = _OPEN_WITH_SHORTCUT
    else:
        how = _OPEN_APP
    return McpToolError(f"[{MachineErrorCode.BROWSER_NOT_OPEN.value}] {head} {how}")


def browser_ambiguous_error(open_browsers: tuple[WebBrowser, ...]) -> McpToolError:
    """``[browser_ambiguous]`` listing the open browsers (enum values only)."""
    names = ", ".join(browser.value for browser in open_browsers)
    if not names:
        return machine_error(MachineErrorCode.BROWSER_AMBIGUOUS)
    return McpToolError(
        f"[{MachineErrorCode.BROWSER_AMBIGUOUS.value}] Several browsers are open "
        f"on the test machine ({names}). Name one with the browser argument."
    )


def _has_error_code(exc: Exception, code: ErrorCode) -> bool:
    """The controller error code ``_raise_for_error_reply`` appended."""
    return f"[{code.value}]" in str(exc)


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
