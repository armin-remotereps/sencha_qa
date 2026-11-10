"""Use case: stop the commands of MCP claims that ended (D7, best effort;
docs/plan/feat-mcp-runner-tools.md, Services › stop_claim_commands).

A registered command (``command_registry``) whose claim is no longer live
(the case's token is empty, the case is gone, or its ``manual_claimed_at``
is not the entry's ``claim_started_at``) is terminated on the test machine it
was started on (the entry's runner) and its entry deleted. One query reads
the cases' claim fields; each stop is its own round trip under that test
machine's lock, without waiting for a reconnect.

- Offline or silent test machine: the remaining entries stay until the next
  sweep or their expiry (a restarted test machine has ended them anyway).
- Machine lock taken: the entry stays for the next sweep.
- The test machine does not know the session (or an older one ended it at
  its time limit): the entry is deleted.
- Any other refusal or a timeout: the entry stays for the next sweep.

Orphans (``include_orphans``): a kept start whose reply was lost leaves a
kept session no registry entry owns. Only MCP starts kept sessions, so a
kept session on a ``KEPT_SESSIONS_V1`` test machine that no entry of any
project owns and that is older than the command lock's TTL (no start that
could still register it is in flight) is stopped too. That costs one query
and one list round trip, so only the per-claim stop after a hand-in or
release and the command tools' pre-start stop ask for it; the minute sweep
does not.

Every round trip holds the test machine's lock, the command deadline
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
from runners.types import RunnerId

logger = logging.getLogger(__name__)

# A kept session no entry owns is an orphan only once no start that could
# still register it can be in flight (a start holds the machine lock).
_ORPHAN_AGE_MS: Final = MCP_COMMAND_LOCK_SECONDS * 1000

# Faults that mean the test machine cannot be reached now: stop trying for
# this test machine until the next sweep.
_UNREACHABLE: Final = (
    ControllerNotConnectedError,
    TestRunnerUnavailableError,
    InfrastructureError,
)


def stop_ended_claim_commands(
    project_id: int,
    pivot_id: int | None = None,
    *,
    include_orphans: bool = False,
    runner_id: RunnerId | None = None,
) -> int:
    """Stop the project's (or one case's) commands of ended claims, and with
    ``include_orphans`` the kept sessions no entry owns; the number stopped.

    Each entry is stopped on the test machine it was started on. Orphans are
    looked for on those machines, on ``runner_id`` (the caller's) and on the
    runner of ``pivot_id``'s run; a session counts as owned when an entry of
    any project names it on that machine (one runner serves many projects).
    """
    all_entries = command_registry.entries_by_pivot(project_id)
    by_pivot = all_entries
    if pivot_id is not None:
        by_pivot = {pivot_id: all_entries[pivot_id]} if pivot_id in all_entries else {}
    stopped = 0
    with no_reconnect_wait():
        unreachable: set[RunnerId] = set()
        if by_pivot:
            stopped, unreachable = _stop_ended(project_id, by_pivot)
        if include_orphans:
            runners = {
                entry.runner_id for entries in by_pivot.values() for entry in entries
            }
            if runner_id is not None:
                runners.add(runner_id)
            if pivot_id is not None:
                pivot_runner = _run_runner(project_id, pivot_id)
                if pivot_runner is not None:
                    runners.add(pivot_runner)
            runners -= unreachable
            if runners:
                owned = _owned_sessions(project_id, all_entries)
                for orphan_runner in sorted(runners):
                    stopped += _stop_orphans(
                        orphan_runner, owned.get(orphan_runner, set())
                    )
    if stopped:
        logger.info("MCP claim-end stop project=%s stopped=%d", project_id, stopped)
    return stopped


def _stop_ended(
    project_id: int, by_pivot: dict[int, list[KeptCommand]]
) -> tuple[int, set[RunnerId]]:
    """(stopped, test machines that could not be reached) for the entries of
    ended claims."""
    live = _live_claims(project_id, list(by_pivot))
    ended = [
        (case_id, entry)
        for case_id, entries in by_pivot.items()
        for entry in entries
        if live.get(case_id) != entry.claim_started_at
    ]
    stopped = 0
    unreachable: set[RunnerId] = set()
    for case_id, entry in ended:
        if entry.runner_id in unreachable:
            continue
        outcome = _stop(project_id, case_id, entry)
        if outcome is None:
            unreachable.add(entry.runner_id)
            continue
        stopped += outcome
    return stopped, unreachable


def _owned_sessions(
    project_id: int, project_entries: dict[int, list[KeptCommand]]
) -> dict[RunnerId, set[str]]:
    """The registered session ids by test machine, of every project with
    registered commands (``project_entries`` is ``project_id``'s, already
    read)."""
    owned: dict[RunnerId, set[str]] = {}
    by_project = {project_id: project_entries}
    for other in command_registry.registered_projects():
        if other not in by_project:
            by_project[other] = command_registry.entries_by_pivot(other)
    for entries_by_pivot in by_project.values():
        for entries in entries_by_pivot.values():
            for entry in entries:
                owned.setdefault(entry.runner_id, set()).add(entry.session_id)
    return owned


def _run_runner(project_id: int, pivot_id: int) -> RunnerId | None:
    """The runner of the case's run now (one query), or None."""
    runner_id = (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project_id=project_id)
        .values_list("test_run__runner_id", flat=True)
        .first()
    )
    return None if runner_id is None else RunnerId(runner_id)


def _stop_orphans(runner_id: RunnerId, owned: set[str]) -> int:
    """Stop the kept sessions on the test machine no entry owns that are
    older than the command lock's TTL (best effort; 0 on any fault)."""
    if not runner_supports(runner_id, ClientCapability.KEPT_SESSIONS_V1):
        return 0
    try:
        with _guards(runner_id):
            infos = controller_list_interactive_sessions(runner_id)
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
            with _guards(runner_id):
                controller_terminate_interactive_command(runner_id, session_id)
        except _UNREACHABLE:
            break
        except (McpToolError, ControllerActionError):
            continue
        stopped += 1
    if stopped:
        logger.info("MCP orphan stop runner=%s stopped=%d", runner_id, stopped)
    return stopped


@contextmanager
def _guards(runner_id: RunnerId) -> Iterator[None]:
    """One round trip's guards: the test machine's lock and the command
    deadline (the caller holds ``no_reconnect_wait``)."""
    with (
        machine_lock(runner_id, MCP_COMMAND_LOCK_SECONDS),
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
    be reached (skip its other entries)."""
    try:
        with _guards(entry.runner_id):
            controller_terminate_interactive_command(entry.runner_id, entry.session_id)
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
