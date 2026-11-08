"""Use case: may the claimed case's test machine serve a browse_* call?
(docs/plan/feat-mcp-browser-toolset.md, MCP surface › browse_choice).

No query and no server state: the gate (``open_machine_session``) already
loaded the capabilities, target and OS name. It does not pick a browser:
which browser is open is known only on the test machine, which applies the
"one open browser, or the one named" rule itself.
"""

from __future__ import annotations

from controller_client.protocol import RunnerTarget, WebBrowser
from mcp_gateway.services.machine_errors import MachineErrorCode, machine_error
from mcp_gateway.services.machine_gate import MachineSession
from projects.runner_capability_services import (
    WebBrowserCheck,
    web_browser_check,
    web_browsers_supported,
)


def check_web_browser(
    session: MachineSession, requested: WebBrowser | None
) -> RunnerTarget:
    """The test machine's target when it can serve a call for ``requested``
    (None: the one open browser). Raises ``McpToolError``:
    ``[machine_outdated]`` without the browser layer, ``[browser_unavailable]``
    for a named browser outside this test machine's table."""
    target = session.runner_target
    if target is None:
        # A programming error: every browse_* tool needs the test machine.
        raise ValueError("browse_* tools run on a test machine")
    check = web_browser_check(session.capabilities, target, session.os_name, requested)
    if check == WebBrowserCheck.OUTDATED:
        raise machine_error(MachineErrorCode.MACHINE_OUTDATED)
    if check == WebBrowserCheck.UNSUPPORTED:
        names = ", ".join(
            browser.value for browser in web_browsers_supported(target, session.os_name)
        )
        detail = f"Browsers on this test machine: {names}." if names else ""
        raise machine_error(MachineErrorCode.BROWSER_UNAVAILABLE, detail)
    return target
