"""Every MCP tool call: resolve the caller's project, throttle, run, log.

``guarded_call`` is what the MCP handlers run (in a worker thread): it loads
the project the verified bearer named (one query; the Connection key or the
OAuth access token itself was checked by ``authenticate_bearer`` on this very
HTTP request), applies the call's throttle, runs the use case and turns every
expected domain error into an ``McpToolError`` with user-safe text. One log
line per call: tool, project id, auth kind (``key`` / ``oauth``), grant id,
case id, outcome and milliseconds; never arguments, notes, images, the key
or a token.
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
from mcp_gateway.limits import MCP_MARK_RATE_PER_MINUTE, MCP_READ_RATE_PER_MINUTE
from mcp_gateway.oauth_types import AuthKind
from projects.exceptions import ManualRunError, PersonStepError
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

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_WINDOW_SECONDS: Final = 60
_NO_PROJECT: Final = "Your access to this project is not valid any more. Reconnect."
_READ_THROTTLED: Final = "Too many requests. Wait a minute and try again."
_MARK_THROTTLED: Final = "Too many step marks. Wait a minute and try again."
_ANSWER_THROTTLED: Final = (
    "Too many claims, releases or hand-ins. Wait a minute and try again."
)
_SHOT_THROTTLED: Final = "Too many screenshots. Wait a minute and try again."
_INTERNAL: Final = "The server could not complete this call. Try again later."
_MACHINE_BUSY = "The test machine is busy. Try again in a minute."
_CLAIM_ENDED = (
    "This case is not claimed any more (released, re-run or the run was stopped)."
)
_NEUTRAL_REFUSALS: Final[dict[str, str]] = {
    MANUAL_PAUSE_AGENT_BUSY: _MACHINE_BUSY,
    MANUAL_PAUSE_STALE_CLAIM: _CLAIM_ENDED,
    DROPPED_CLAIM: _CLAIM_ENDED,
}


class CallKind(StrEnum):
    """Which throttle a tool call counts against."""

    READ = "read"
    # Claim, release and hand-in: the project's shared runner answer budget.
    ANSWER = "answer"
    MARK = "mark"
    SHOT = "shot"


@dataclass(frozen=True)
class McpCall:
    """The resolved caller of one tool call."""

    project: Project
    actor: PersonStepActor


def guarded_call(
    *,
    project_id: int | None,
    auth: AuthKind,
    grant_id: int | None,
    tool: str,
    kind: CallKind,
    pivot_id: int | None,
    run: Callable[[McpCall], _T],
) -> _T:
    """Resolve, throttle and run one tool call (sync; database work).

    ``project_id`` is the verified bearer's project (None when the request
    had no verified bearer); ``auth`` (``key`` / ``oauth``) and ``grant_id``
    (the OAuth connection, None for the key) are only logged. Raises
    ``McpToolError`` for every refusal.
    """
    started = time.monotonic()
    outcome = "error"
    try:
        call = _resolve_call(project_id)
        _throttle(call.project.pk, kind)
        result = run(call)
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
            "mcp tool=%s project=%s auth=%s grant=%s pivot=%s outcome=%s ms=%d",
            tool,
            project_id,
            auth,
            grant_id,
            pivot_id,
            outcome,
            int((time.monotonic() - started) * 1000),
        )


def _resolve_call(project_id: int | None) -> McpCall:
    """The bearer's project (non-archived; one query) and the neutral tester."""
    if project_id is None:
        raise McpToolError(_NO_PROJECT)
    project = (
        Project.objects.filter(pk=project_id, archived=False)
        .only("id", "name", "archived")
        .first()
    )
    if project is None:
        raise McpToolError(_NO_PROJECT)
    return McpCall(project=project, actor=PersonStepActor.tester())


def _throttle(project_id: int, kind: CallKind) -> None:
    if kind is CallKind.ANSWER:
        # Shared with the Test Runner window: together ≤ 20/min per project.
        try:
            check_answer_throttle(project_id)
        except PersonStepError as exc:
            raise McpToolError(_ANSWER_THROTTLED) from exc
        return
    if kind is CallKind.READ:
        key, limit, message = (
            f"mcp:read:{project_id}",
            MCP_READ_RATE_PER_MINUTE,
            _READ_THROTTLED,
        )
    elif kind is CallKind.MARK:
        key, limit, message = (
            f"mcp:mark:{project_id}",
            MCP_MARK_RATE_PER_MINUTE,
            _MARK_THROTTLED,
        )
    else:
        key, limit, message = (
            f"mcp:shot:{project_id}",
            settings.MANUAL_SHOT_RATE_PER_MINUTE,
            _SHOT_THROTTLED,
        )
    if not hit_runner_throttle(key, limit=limit, window_seconds=_WINDOW_SECONDS):
        raise McpToolError(message)
