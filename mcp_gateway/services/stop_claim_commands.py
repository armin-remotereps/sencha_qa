"""Use case: stop the commands of MCP claims that ended (D7, best effort;
docs/plan/feat-mcp-runner-tools.md, Services › stop_claim_commands).

A registered command (``command_registry``) whose claim is no longer live
(the case's token is empty, the case is gone, or its ``manual_claimed_at``
is not the entry's ``claim_started_at``) is terminated on the test machine
and its entry deleted. One query reads the cases' claim fields; each stop is
its own round trip under the project's machine lock, without waiting for a
reconnect.

- Offline or silent test machine: the remaining entries stay until the next
  sweep or their expiry (a restarted test machine has ended them anyway).
- Machine lock taken: the entry stays for the next sweep.
- The test machine does not know the session (or an older one ended it at
  its time limit): the entry is deleted.
- Any other refusal or a timeout: the entry stays for the next sweep.

Orphans (``include_orphans``): a kept start whose reply was lost leaves a
kept session no registry entry owns. Only MCP starts kept sessions, so a
kept session on a ``KEPT_SESSIONS_V1`` test machine that no entry of the
project owns and that is older than the command lock's TTL (no start that
could still register it is in flight) is stopped too. That costs one query
and one list round trip, so only the per-claim stop after a hand-in or
release and the command tools' pre-start stop ask for it; the minute sweep
does not.

Every round trip holds the project's machine lock, the command deadline
(``call_deadline``) and never waits for a reconnect.

Called right after an MCP hand-in or release (task ``stop_claim_commands``),
before a kept start (``command_sessions``) and by the minute sweep
(``sweep_ended_claim_commands``) for every other end of a claim.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Final

from agents.exceptions import InfrastructureError
from controller_client.protocol import ClientCapability
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.limits import MCP_COMMAND_DEADLINE_SECONDS, MCP_COMMAND_LOCK_SECONDS
from mcp_gateway.services import command_registry
from mcp_gateway.services.command_registry import KeptCommand
from mcp_gateway.services.machine_round_trip import machine_lock
from mcp_gateway.services.session_replies import (
    is_timed_out_session,
    is_unknown_session,
)
from projects.call_deadline import call_deadline
from projects.controller_command_sessions import controller_list_interactive_sessions
from projects.controller_reconnect_services import no_reconnect_wait
from projects.exceptions import (
    ControllerActionError,
    ControllerNotConnectedError,
    TestRunnerUnavailableError,
)
from projects.models import TestRunTestCase
from projects.runner_capability_services import runner_supports
from projects.services import controller_terminate_interactive_command

logger = logging.getLogger(__name__)

# A kept session no entry owns is an orphan only once no start that could
# still register it can be in flight (a start holds the machine lock).
_ORPHAN_AGE_MS: Final = MCP_COMMAND_LOCK_SECONDS * 1000

# Faults that mean the test machine cannot be reached now: stop trying for
# this project until the next sweep.
_UNREACHABLE: Final = (
    ControllerNotConnectedError,
    TestRunnerUnavailableError,
    InfrastructureError,
)


def stop_ended_claim_commands(
    project_id: int, pivot_id: int | None = None, *, include_orphans: bool = False
) -> int:
    """Stop the project's (or one case's) commands of ended claims, and with
    ``include_orphans`` the kept sessions no entry owns; the number stopped."""
    all_entries = command_registry.entries_by_pivot(project_id)
    by_pivot = all_entries
    if pivot_id is not None:
        by_pivot = {pivot_id: all_entries[pivot_id]} if pivot_id in all_entries else {}
    stopped = 0
    with no_reconnect_wait():
        reachable = True
        if by_pivot:
            stopped, reachable = _stop_ended(project_id, by_pivot)
        if include_orphans and reachable:
            owned = {
                entry.session_id
                for entries in all_entries.values()
                for entry in entries
            }
            stopped += _stop_orphans(project_id, owned)
    if stopped:
        logger.info("MCP claim-end stop project=%s stopped=%d", project_id, stopped)
    return stopped


def _stop_ended(
    project_id: int, by_pivot: dict[int, list[KeptCommand]]
) -> tuple[int, bool]:
    """(stopped, test machine still reachable) for the entries of ended
    claims."""
    live = _live_claims(project_id, list(by_pivot))
    ended = [
        (case_id, entry)
        for case_id, entries in by_pivot.items()
        for entry in entries
        if live.get(case_id) != entry.claim_started_at
    ]
    stopped = 0
    for case_id, entry in ended:
        outcome = _stop(project_id, case_id, entry)
        if outcome is None:
            return stopped, False
        stopped += outcome
    return stopped, True


def _stop_orphans(project_id: int, owned: set[str]) -> int:
    """Stop the kept sessions no entry owns that are older than the command
    lock's TTL (best effort; 0 on any fault)."""
    if not runner_supports(project_id, ClientCapability.KEPT_SESSIONS_V1):
        return 0
    try:
        with _guards(project_id):
            infos = controller_list_interactive_sessions(project_id)
    except (McpToolError, ControllerActionError, *_UNREACHABLE):
        return 0
    orphans = [
        info.session_id
        for info in infos
        if info.kept
        and info.session_id not in owned
        and info.elapsed_ms > _ORPHAN_AGE_MS
    ]
    stopped = 0
    for session_id in orphans:
        try:
            with _guards(project_id):
                controller_terminate_interactive_command(project_id, session_id)
        except _UNREACHABLE:
            break
        except (McpToolError, ControllerActionError):
            continue
        stopped += 1
    if stopped:
        logger.info("MCP orphan stop project=%s stopped=%d", project_id, stopped)
    return stopped


@contextmanager
def _guards(project_id: int) -> Iterator[None]:
    """One round trip's guards: the machine lock and the command deadline
    (the caller holds ``no_reconnect_wait``)."""
    with (
        machine_lock(project_id, MCP_COMMAND_LOCK_SECONDS),
        call_deadline(MCP_COMMAND_DEADLINE_SECONDS),
    ):
        yield


def _live_claims(project_id: int, pivot_ids: list[int]) -> dict[int, datetime]:
    """``manual_claimed_at`` of the cases that are claimed now (one query)."""
    rows = (
        TestRunTestCase.objects.filter(
            pk__in=pivot_ids,
            test_run__project_id=project_id,
            manual_claimed_at__isnull=False,
        )
        .exclude(manual_claim_token="")
        .values_list("pk", "manual_claimed_at")
    )
    return {pk: claimed_at for pk, claimed_at in rows if claimed_at is not None}


def _stop(project_id: int, pivot_id: int, entry: KeptCommand) -> int | None:
    """1 stopped, 0 left or already gone, None when the test machine cannot
    be reached (stop the project's loop)."""
    try:
        with _guards(project_id):
            controller_terminate_interactive_command(project_id, entry.session_id)
    except McpToolError:
        # The machine lock is held (a live claim's action): next sweep.
        return 0
    except _UNREACHABLE:
        return None
    except ControllerActionError as exc:
        text = str(exc)
        if is_unknown_session(text) or is_timed_out_session(text):
            # The test machine no longer runs it: it ended already.
            command_registry.forget(project_id, pivot_id, entry.session_id)
        # Any other refusal: the entry stays for the next sweep.
        return 0
    command_registry.forget(project_id, pivot_id, entry.session_id)
    return 1
