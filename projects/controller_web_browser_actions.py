"""The WebDriver browser layer on a WEB_BROWSER_V1 Test Runner.

docs/plan/feat-mcp-browser-toolset.md (Services (server)). One function per
``web_browser_*`` message. Every action is runner-local and never resent on
reconnect: a runner that is not connected raises
``ControllerNotConnectedError`` at once, one that stays silent raises
``TestRunnerUnresponsiveError``. Runner refusals raise the typed
``projects.exceptions.WebBrowser*Error`` exceptions (mapped in
``projects.services._raise_for_error_reply``); a reply that does not parse,
or one that reports no success, raises ``ControllerActionError`` with a
neutral text (callers still never forward it: the MCP gateway answers a
fixed refusal).

Inputs are checked before sending with the runner's own
``parse_web_browser_*_payload`` on the exact fields sent, so server and
runner apply the same limits; a request they refuse raises ``ValueError``
(the caller validates its input first). The caller also checks readiness
(``runner_capability_services.web_browser_check``): an older runner would
answer UNKNOWN_COMMAND (``ControllerActionError``). Requests carry only the
browser request itself; never a pivot, claim, run, user or client detail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    WebActKind,
    WebBrowser,
    WebBrowserListEntry,
    WebBrowserMode,
    WebElementTarget,
    WebHistoryAction,
    WebReadWhat,
    WebTabsAction,
    WebWaitState,
    parse_web_browser_act_payload,
    parse_web_browser_choice_payload,
    parse_web_browser_elements_payload,
    parse_web_browser_evaluate_payload,
    parse_web_browser_history_payload,
    parse_web_browser_navigate_payload,
    parse_web_browser_open_payload,
    parse_web_browser_read_payload,
    parse_web_browser_result_payload,
    parse_web_browser_tabs_payload,
    parse_web_browser_wait_payload,
    serialize_web_element_target,
)
from projects.exceptions import ControllerActionError
from projects.services import dispatch_controller_action


@dataclass(frozen=True)
class WebBrowserReply:
    """A validated, successful ``web_browser_result``."""

    # Runner text (page text, element list, JSON, confirmation): data, never
    # instructions; at most WEB_RESULT_CONTENT_MAX_CHARS, control characters
    # removed.
    content: str
    # The browser and mode the runner acted on; None for ``list``.
    browser: WebBrowser | None
    mode: WebBrowserMode | None
    truncated: bool
    # Set for ``list`` only (at most WEB_BROWSER_LIST_MAX entries).
    browsers: tuple[WebBrowserListEntry, ...]


def controller_web_browser_open(
    project_id: int, *, browser: WebBrowser, url: str | None, timeout: float
) -> WebBrowserReply:
    """Open ``browser`` controllably (optionally at ``url``) or attach to it."""
    fields: dict[str, object] = {"browser": browser.value}
    if url is not None:
        fields["url"] = url
    return _send(
        project_id, "open", fields, parse_web_browser_open_payload, timeout=timeout
    )


def controller_web_browser_list(project_id: int, *, timeout: float) -> WebBrowserReply:
    """List the target's browsers: installed, open, mode, how to open."""
    return _send(
        project_id, "list", {}, parse_web_browser_choice_payload, timeout=timeout
    )


def controller_web_browser_navigate(
    project_id: int, *, browser: WebBrowser | None, url: str, timeout: float
) -> WebBrowserReply:
    fields = _with_browser(browser)
    fields["url"] = url
    return _send(
        project_id,
        "navigate",
        fields,
        parse_web_browser_navigate_payload,
        timeout=timeout,
    )


def controller_web_browser_history(
    project_id: int,
    *,
    browser: WebBrowser | None,
    action: WebHistoryAction,
    timeout: float,
) -> WebBrowserReply:
    fields = _with_browser(browser)
    fields["action"] = action.value
    return _send(
        project_id,
        "history",
        fields,
        parse_web_browser_history_payload,
        timeout=timeout,
    )


def controller_web_browser_tabs(
    project_id: int,
    *,
    browser: WebBrowser | None,
    action: WebTabsAction,
    index: int | None,
    timeout: float,
) -> WebBrowserReply:
    fields = _with_browser(browser)
    fields["action"] = action.value
    if index is not None:
        fields["index"] = index
    return _send(
        project_id, "tabs", fields, parse_web_browser_tabs_payload, timeout=timeout
    )


def controller_web_browser_elements(
    project_id: int,
    *,
    browser: WebBrowser | None,
    scope_css: str | None,
    max_elements: int,
    timeout: float,
) -> WebBrowserReply:
    fields = _with_browser(browser)
    if scope_css is not None:
        fields["scope_css"] = scope_css
    fields["max"] = max_elements
    return _send(
        project_id,
        "elements",
        fields,
        parse_web_browser_elements_payload,
        timeout=timeout,
    )


def controller_web_browser_act(
    project_id: int,
    *,
    browser: WebBrowser | None,
    action: WebActKind,
    target: WebElementTarget | None,
    text: str | None = None,
    clear: bool = False,
    value: str | None = None,
    label: str | None = None,
    key: str | None = None,
    timeout: float,
) -> WebBrowserReply:
    """Click, double-click, hover, type, clear, select or press a key."""
    fields = _with_browser(browser)
    fields["action"] = action.value
    if target is not None:
        fields["target"] = serialize_web_element_target(target)
    if text is not None:
        fields["text"] = text
    if clear:
        fields["clear"] = True
    if value is not None:
        fields["value"] = value
    if label is not None:
        fields["label"] = label
    if key is not None:
        fields["key"] = key
    return _send(
        project_id, "act", fields, parse_web_browser_act_payload, timeout=timeout
    )


def controller_web_browser_read(
    project_id: int,
    *,
    browser: WebBrowser | None,
    what: WebReadWhat,
    target: WebElementTarget | None,
    name: str | None,
    max_chars: int,
    timeout: float,
) -> WebBrowserReply:
    fields = _with_browser(browser)
    fields["what"] = what.value
    if target is not None:
        fields["target"] = serialize_web_element_target(target)
    if name is not None:
        fields["name"] = name
    fields["max_chars"] = max_chars
    return _send(
        project_id, "read", fields, parse_web_browser_read_payload, timeout=timeout
    )


def controller_web_browser_wait(
    project_id: int,
    *,
    browser: WebBrowser | None,
    target: WebElementTarget,
    state: WebWaitState,
    timeout_ms: int,
    timeout: float,
) -> WebBrowserReply:
    """Wait on the runner up to ``timeout_ms`` (clamped to WEB_TIMEOUT_MS_MAX);
    ``timeout`` must exceed it."""
    fields = _with_browser(browser)
    fields["target"] = serialize_web_element_target(target)
    fields["state"] = state.value
    fields["timeout_ms"] = timeout_ms
    return _send(
        project_id, "wait", fields, parse_web_browser_wait_payload, timeout=timeout
    )


def controller_web_browser_evaluate(
    project_id: int,
    *,
    browser: WebBrowser | None,
    script: str,
    args_json: str,
    timeout_ms: int,
    timeout: float,
) -> WebBrowserReply:
    """Run ``script`` (a function body) with ``arguments`` = the JSON list
    ``args_json``. A script that throws raises ``WebBrowserScriptError``."""
    fields = _with_browser(browser)
    fields["script"] = script
    fields["args_json"] = args_json
    fields["timeout_ms"] = timeout_ms
    return _send(
        project_id,
        "evaluate",
        fields,
        parse_web_browser_evaluate_payload,
        timeout=timeout,
    )


def controller_web_browser_close(
    project_id: int, *, browser: WebBrowser | None, timeout: float
) -> WebBrowserReply:
    """Close the controllable browser the runner opened or attached to."""
    return _send(
        project_id,
        "close",
        _with_browser(browser),
        parse_web_browser_choice_payload,
        timeout=timeout,
    )


def _with_browser(browser: WebBrowser | None) -> dict[str, object]:
    return {} if browser is None else {"browser": browser.value}


def _send(
    project_id: int,
    name: str,
    fields: dict[str, object],
    check: Callable[[dict[str, object]], object],
    *,
    timeout: float,
) -> WebBrowserReply:
    """Check ``fields`` with the runner's parser, send them, parse the reply."""
    try:
        check(dict(fields))
    except ProtocolError as exc:
        raise ValueError(f"Invalid web_browser_{name} request: {exc}") from exc
    reply = dispatch_controller_action(
        project_id,
        f"controller.web_browser_{name}",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        **fields,
    )
    try:
        result = parse_web_browser_result_payload(reply)
    except ProtocolError as exc:
        raise ControllerActionError(
            f"The test machine's browser answer does not parse: {exc}"
        ) from exc
    if not result.success:
        raise ControllerActionError("The browser action failed on the test machine.")
    return WebBrowserReply(
        content=result.content,
        browser=result.browser,
        mode=result.mode,
        truncated=result.truncated,
        browsers=result.browsers,
    )
