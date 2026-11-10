"""Every MCP tool call: resolve the project the call names, throttle, run, log.

``guarded_call`` is what the project-scoped MCP handlers run (in a worker
thread). The caller (``McpCaller``: an OAuth grant or a Connection key) was
verified by ``authenticate_bearer`` on this very HTTP request; every call
names its project (``project_id``, required by the tool schema), resolved
here in one query:

- Connection key: ``project_id`` must be the key's project (non-archived);
- OAuth: the grant's live link to ``project_id``
  (``oauth_access.live_links`` plus the Connection key fingerprint compare).

An unknown, foreign, removed or dead project gets one neutral text that
echoes only the integer the client sent and points to ``list_projects``.
``guarded_caller_call`` runs the one caller-scoped tool (``list_projects``):
no project resolution, a READ throttle keyed by the caller.

Both apply the call's throttle, run the use case and turn every expected
domain error into an ``McpToolError`` with user-safe text. One log line per
call: tool, project id, auth kind (``key`` / ``oauth``), grant id, case id,
actor kind (``member`` for OAuth, ``anonymous`` for a Connection key; never a
name), command session id (command tools only), outcome and milliseconds;
never arguments, notes, images, the key or a token.

``McpCall.actor`` is the neutral tester of the Manual tools. The machine
tools write nothing attributed or user-visible; the log line records only
their actor's kind. D5 (docs/plan/feat-mcp-runner-tools.md) is the rule for
any future user-visible text of a machine tool: an OAuth grant acts as
"<member> via Test Runner" (``PersonStepActor.runner(user)``), a Connection
key as "a person at the Test Runner" (``PersonStepActor.runner(None)``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeVar

from django.conf import settings

from controller_client.exceptions import ProtocolError
from knowledge_base.exceptions import KnowledgeBaseError
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.limits import (
    MCP_AI_RATE_PER_MINUTE,
    MCP_COMMAND_RATE_PER_MINUTE,
    MCP_CONTROL_RATE_PER_MINUTE,
    MCP_EVAL_RATE_PER_MINUTE,
    MCP_MACHINE_RATE_PER_MINUTE,
    MCP_MARK_RATE_PER_MINUTE,
    MCP_READ_RATE_PER_MINUTE,
    MCP_SCREEN_RATE_PER_MINUTE,
)
from mcp_gateway.oauth_types import McpCaller
from mcp_gateway.services.oauth_access import link_fingerprint_matches, live_links
from projects.exceptions import ManualRunError, PersonStepError
from projects.manual_case_claim_services import RUNNER_CHANGED_MESSAGE
from projects.manual_case_submit_services import DROPPED_CLAIM
from projects.manual_run_policy import (
    MANUAL_PAUSE_AGENT_BUSY,
    MANUAL_PAUSE_STALE_CLAIM,
)
from projects.models import Project
from projects.person_step_actor import PersonStepActor
from projects.runner_person_step_services import (
    check_answer_throttle,
    hit_runner_throttle,
)
from projects.runner_use import RUNNER_HOLD_MESSAGES

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_WINDOW_SECONDS: Final = 60
_PROJECT_REFUSED: Final = (
    "This connection cannot use project {project_id}. Call list_projects for "
    "the projects you can use."
)
_READ_THROTTLED: Final = "Too many requests. Wait a minute and try again."
_MARK_THROTTLED: Final = "Too many step marks. Wait a minute and try again."
_ANSWER_THROTTLED: Final = (
    "Too many claims, releases or hand-ins. Wait a minute and try again."
)
_SHOT_THROTTLED: Final = "Too many screenshots. Wait a minute and try again."
_MACHINE_THROTTLED: Final = (
    "Too many requests to the test machine. Wait a minute and try again."
)
_CONTROL_THROTTLED: Final = (
    "Too many actions on the test machine. Wait a minute and try again."
)
_AI_THROTTLED: Final = (
    "Too many actions that find elements by description or search the web. "
    "Wait a minute and try again, or use click_at and the other *_at tools "
    "with coordinates."
)
_COMMAND_THROTTLED: Final = "Too many command requests. Wait a minute and try again."
_SCREEN_THROTTLED: Final = (
    "Too many screenshots of the test machine. Wait a minute and try again."
)
_EVAL_THROTTLED: Final = (
    "Too many scripts run in the test machine's browser. Wait a minute and "
    "try again."
)
_INTERNAL: Final = "The server could not complete this call. Try again later."
_MACHINE_BUSY = "The test machine is busy. Try again in a minute."
_CLAIM_ENDED = (
    "This case is not claimed any more (released, re-run or the run was stopped)."
)
_MACHINE_CHANGED = "The run's test machine changed. Look at the list again."
_NEUTRAL_REFUSALS: Final[dict[str, str]] = {
    MANUAL_PAUSE_AGENT_BUSY: _MACHINE_BUSY,
    MANUAL_PAUSE_STALE_CLAIM: _CLAIM_ENDED,
    DROPPED_CLAIM: _CLAIM_ENDED,
    RUNNER_CHANGED_MESSAGE: _MACHINE_CHANGED,
    # A claim refused because the run's Test Runner is held (runner_use).
    **{text: _MACHINE_BUSY for text in RUNNER_HOLD_MESSAGES},
}


class CallKind(StrEnum):
    """Which throttle a tool call counts against."""

    READ = "read"
    # Claim, release and hand-in: the project's shared runner answer budget.
    ANSWER = "answer"
    MARK = "mark"
    # add_screenshot and capture_screenshot share this budget.
    SHOT = "shot"
    # list_windows: a round trip to the test machine.
    MACHINE = "machine"
    # Machine tools inside a claimed case (mcp_gateway.machine_tools).
    # Coordinate input, keys, apps, plain browser tools, phone buttons, wait.
    CONTROL = "control"
    # Description tools, web_search; click_at and the other *_at tools,
    # swipe / rotate_device with an expected_result.
    AI = "ai"
    # The command tools.
    COMMAND = "command"
    # take_screenshot (analysis only).
    SCREEN = "screen"
    # browse_evaluate (a script in the test machine's browser).
    EVAL = "eval"


# kind -> (throttle key prefix, limit per project and minute, refusal).
_RATE_LIMITS: Final[dict[CallKind, tuple[str, int, str]]] = {
    CallKind.MARK: ("mcp:mark", MCP_MARK_RATE_PER_MINUTE, _MARK_THROTTLED),
    CallKind.MACHINE: ("mcp:machine", MCP_MACHINE_RATE_PER_MINUTE, _MACHINE_THROTTLED),
    CallKind.CONTROL: (
        "mcp:control",
        MCP_CONTROL_RATE_PER_MINUTE,
        _CONTROL_THROTTLED,
    ),
    CallKind.AI: ("mcp:ai", MCP_AI_RATE_PER_MINUTE, _AI_THROTTLED),
    CallKind.COMMAND: (
        "mcp:command",
        MCP_COMMAND_RATE_PER_MINUTE,
        _COMMAND_THROTTLED,
    ),
    CallKind.SCREEN: ("mcp:screen", MCP_SCREEN_RATE_PER_MINUTE, _SCREEN_THROTTLED),
    CallKind.EVAL: ("mcp:eval", MCP_EVAL_RATE_PER_MINUTE, _EVAL_THROTTLED),
}


@dataclass(frozen=True)
class McpCall:
    """The resolved caller of one tool call."""

    project: Project
    # The neutral tester ("a tester") of the Manual tools. The machine tools
    # have no actor here: see D5 in the module docstring.
    actor: PersonStepActor


def guarded_call(
    *,
    caller: McpCaller,
    project_id: int,
    tool: str,
    kind: CallKind,
    pivot_id: int | None,
    run: Callable[[McpCall], _T],
    session: str | None = None,
) -> _T:
    """Resolve, throttle and run one project-scoped tool call (sync).

    ``caller`` is the verified bearer; ``project_id`` the project the call
    names; ``session`` the command session id a command tool names (logged
    only). Raises ``McpToolError`` for every refusal.
    """

    def body() -> _T:
        call = _resolve_call(caller, project_id)
        _throttle(call.project.pk, kind)
        return run(call)

    return _guarded(
        caller=caller,
        project_id=project_id,
        tool=tool,
        pivot_id=pivot_id,
        session=session,
        body=body,
    )


def guarded_caller_call(
    *, caller: McpCaller, tool: str, run: Callable[[McpCaller], _T]
) -> _T:
    """Throttle and run one caller-scoped tool call (``list_projects``; sync).

    No project is resolved; the READ throttle is keyed by the caller (the
    OAuth grant, or the Connection key's project). Raises ``McpToolError``
    for every refusal.
    """

    def body() -> _T:
        _throttle_read(_caller_read_key(caller))
        return run(caller)

    return _guarded(
        caller=caller,
        project_id=caller.key_project_id,
        tool=tool,
        pivot_id=None,
        session=None,
        body=body,
    )


def _guarded(
    *,
    caller: McpCaller,
    project_id: int | None,
    tool: str,
    pivot_id: int | None,
    session: str | None,
    body: Callable[[], _T],
) -> _T:
    """Run ``body``, map expected errors to ``McpToolError`` and log once."""
    started = time.monotonic()
    outcome = "error"
    try:
        result = body()
        outcome = "ok"
        return result
    except McpToolError:
        outcome = "refused"
        raise
    except (ManualRunError, PersonStepError, KnowledgeBaseError) as exc:
        outcome = "refused"
        # Shared refusals that name the agent or the Test Runner reach the
        # client in neutral words.
        message = _NEUTRAL_REFUSALS.get(exc.message, exc.message)
        raise McpToolError(message) from exc
    except ProtocolError as exc:
        outcome = "refused"
        raise McpToolError(str(exc)) from exc
    except Exception as exc:
        logger.exception("MCP tool %s failed (project %s)", tool, project_id)
        raise McpToolError(_INTERNAL) from exc
    finally:
        logger.info(
            "mcp tool=%s project=%s auth=%s grant=%s actor=%s pivot=%s "
            "session=%s outcome=%s ms=%d",
            tool,
            project_id,
            caller.auth,
            caller.grant_id,
            _actor_kind(caller),
            pivot_id,
            session,
            outcome,
            int((time.monotonic() - started) * 1000),
        )


def _resolve_call(caller: McpCaller, project_id: int) -> McpCall:
    """The project ``caller`` may use as ``project_id`` (one query) and the
    neutral tester; the same refusal for every project it may not use."""
    project: Project | None = None
    if caller.key_project_id is not None:
        if project_id == caller.key_project_id:
            project = (
                Project.objects.filter(pk=project_id, archived=False)
                .only("id", "name", "archived")
                .first()
            )
    elif caller.grant_id is not None:
        link = (
            live_links()
            .filter(grant_id=caller.grant_id, project_id=project_id)
            .only(
                "api_key_fingerprint",
                "project",
                "project__id",
                "project__name",
                "project__archived",
                "project__api_key",
            )
            .first()
        )
        if link is not None and link_fingerprint_matches(link):
            project = link.project
    if project is None:
        raise McpToolError(_PROJECT_REFUSED.format(project_id=project_id))
    return McpCall(project=project, actor=PersonStepActor.tester())


def _actor_kind(caller: McpCaller) -> str:
    """The machine actor's kind for the log line (never a name): an OAuth
    grant acts as its member, a Connection key anonymously."""
    return "member" if caller.grant_id is not None else "anonymous"


def _caller_read_key(caller: McpCaller) -> str:
    """The READ throttle key of a caller-scoped call: per grant, or the
    Connection key's project budget."""
    if caller.grant_id is not None:
        return f"mcp:read:grant:{caller.grant_id}"
    return f"mcp:read:{caller.key_project_id}"


def _throttle_read(key: str) -> None:
    if not hit_runner_throttle(
        key, limit=MCP_READ_RATE_PER_MINUTE, window_seconds=_WINDOW_SECONDS
    ):
        raise McpToolError(_READ_THROTTLED)


def _throttle(project_id: int, kind: CallKind) -> None:
    if kind is CallKind.ANSWER:
        # Shared with the Test Runner window: together ≤ 20/min per project.
        try:
            check_answer_throttle(project_id)
        except PersonStepError as exc:
            raise McpToolError(_ANSWER_THROTTLED) from exc
        return
    if kind is CallKind.READ:
        _throttle_read(f"mcp:read:{project_id}")
        return
    rate = _RATE_LIMITS.get(kind)
    if rate is None:
        # SHOT: add_screenshot and capture_screenshot.
        rate = ("mcp:shot", settings.MANUAL_SHOT_RATE_PER_MINUTE, _SHOT_THROTTLED)
    prefix, limit, message = rate
    key = f"{prefix}:{project_id}"
    if not hit_runner_throttle(key, limit=limit, window_seconds=_WINDOW_SECONDS):
        raise McpToolError(message)
