"""The commands an MCP claim started on the test machine (D7: commands
belong to the claim; docs/plan/feat-mcp-runner-tools.md).

Redis, one hash per claimed case: ``mcp:commands:{project_id}:{pivot_id}``
maps a command session id to JSON ``{runner_id, claim_started_at,
started_at, detached, command_head}``; ``runner_id`` is the test machine
(Test Runner) the command runs on, so it is stopped there even when the
run's runner changed meanwhile. ``claim_started_at`` (the claim's
``manual_claimed_at``) ties the entry to one claim: a later claim of the
same case does not own it. The pivots with entries are kept in
``mcp:commands:pivots:{project_id}`` and the projects in
``mcp:commands:projects``, so the claim-end sweep reads one set when nothing
is registered. Every key expires ``MCP_DETACHED_MAX_SECONDS`` + 1 h after
the last registration (the test machine ends a kept command after
``MCP_DETACHED_MAX_SECONDS``); the sets are pruned atomically once their
hashes are gone.

``command_head`` is the command's start, returned to its own claim only;
it is never logged.

The client is its own (like ``machine_round_trip``'s) with short socket
timeouts: an unreachable Redis raises ``CommandRegistryUnavailable`` instead
of holding a worker thread.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypeVar

import redis
from django.conf import settings

from mcp_gateway.limits import (
    MCP_DETACHED_MAX_SECONDS,
    MCP_MACHINE_LOCK_REDIS_TIMEOUT_SECONDS,
)
from runners.types import RunnerId

logger = logging.getLogger(__name__)

COMMAND_HEAD_MAX_CHARS: Final = 120
_TTL_SECONDS: Final = MCP_DETACHED_MAX_SECONDS + 3600
_CLAIM_KEY: Final = "mcp:commands:{project_id}:{pivot_id}"
_PIVOTS_KEY: Final = "mcp:commands:pivots:{project_id}"
_PROJECTS_KEY: Final = "mcp:commands:projects"
# Removes ARGV[1] from the set KEYS[2] only while KEYS[1] (the hash, or the
# set of pivots) is empty, atomically, so a registration racing the prune
# is never dropped from the index.
_PRUNE_SCRIPT: Final = (
    "if redis.call('exists', KEYS[1]) == 0 then "
    "return redis.call('srem', KEYS[2], ARGV[1]) else return 0 end"
)

_REDIS: Final = redis.Redis.from_url(
    settings.REDIS_URL,
    socket_connect_timeout=MCP_MACHINE_LOCK_REDIS_TIMEOUT_SECONDS,
    socket_timeout=MCP_MACHINE_LOCK_REDIS_TIMEOUT_SECONDS,
    decode_responses=True,
)
_prune = _REDIS.register_script(_PRUNE_SCRIPT)

_T = TypeVar("_T")


class CommandRegistryUnavailable(Exception):
    """Redis did not answer; nothing was read or written."""


@dataclass(frozen=True)
class KeptCommand:
    session_id: str
    # The test machine (Test Runner) the command was started on.
    runner_id: RunnerId
    # The owning claim's ``manual_claimed_at``.
    claim_started_at: datetime
    started_at: datetime
    detached: bool
    # The command's first characters (data; never logged).
    command_head: str


def command_head(command: str) -> str:
    """The stored start of ``command``."""
    return command[:COMMAND_HEAD_MAX_CHARS]


def register(project_id: int, pivot_id: int, entry: KeptCommand) -> None:
    """Record ``entry`` for the claim (and refresh the keys' expiry)."""
    claim_key = _CLAIM_KEY.format(project_id=project_id, pivot_id=pivot_id)
    pivots_key = _PIVOTS_KEY.format(project_id=project_id)
    value = json.dumps(
        {
            "runner_id": entry.runner_id,
            "claim_started_at": entry.claim_started_at.isoformat(),
            "started_at": entry.started_at.isoformat(),
            "detached": entry.detached,
            "command_head": entry.command_head,
        }
    )
    try:
        pipe = _REDIS.pipeline(transaction=True)
        pipe.hset(claim_key, entry.session_id, value)
        pipe.expire(claim_key, _TTL_SECONDS)
        pipe.sadd(pivots_key, str(pivot_id))
        pipe.expire(pivots_key, _TTL_SECONDS)
        pipe.sadd(_PROJECTS_KEY, str(project_id))
        pipe.expire(_PROJECTS_KEY, _TTL_SECONDS)
        pipe.execute()
    except redis.RedisError as exc:
        logger.warning("command registry unavailable project=%s", project_id)
        raise CommandRegistryUnavailable from exc


def owned(
    project_id: int, pivot_id: int, claim_started_at: datetime, session_id: str
) -> KeptCommand | None:
    """The claim's entry for ``session_id``, or None (not registered, or
    registered under another claim of the case)."""
    claim_key = _CLAIM_KEY.format(project_id=project_id, pivot_id=pivot_id)
    try:
        raw = _sync(_REDIS.hget(claim_key, session_id))
    except redis.RedisError as exc:
        logger.warning("command registry unavailable project=%s", project_id)
        raise CommandRegistryUnavailable from exc
    if raw is None:
        return None
    entry = _parse(session_id, raw)
    if entry is None or entry.claim_started_at != claim_started_at:
        return None
    return entry


def list_owned(
    project_id: int, pivot_id: int, claim_started_at: datetime
) -> list[KeptCommand]:
    """The claim's entries, oldest first."""
    entries = _read_claim(project_id, pivot_id)
    owned_entries = [e for e in entries if e.claim_started_at == claim_started_at]
    return sorted(owned_entries, key=lambda e: e.started_at)


def forget(project_id: int, pivot_id: int, session_id: str) -> None:
    """Remove the entry (and prune the indexes once empty). Best effort: a
    Redis fault is logged and the entry expires on its own."""
    claim_key = _CLAIM_KEY.format(project_id=project_id, pivot_id=pivot_id)
    pivots_key = _PIVOTS_KEY.format(project_id=project_id)
    try:
        _REDIS.hdel(claim_key, session_id)
        _prune(keys=[claim_key, pivots_key], args=[str(pivot_id)])
        _prune(keys=[pivots_key, _PROJECTS_KEY], args=[str(project_id)])
    except redis.RedisError:
        logger.warning("command registry forget failed project=%s", project_id)


def registered_projects() -> list[int]:
    """The projects with registered commands (one Redis call when none)."""
    try:
        members = _sync(_REDIS.smembers(_PROJECTS_KEY))
    except redis.RedisError as exc:
        logger.warning("command registry unavailable")
        raise CommandRegistryUnavailable from exc
    return sorted(int(m) for m in members if str(m).isdigit())


def has_other_pivots(project_id: int, pivot_id: int) -> bool:
    """Commands of other cases of the project are registered (one Redis
    call; the index may still name a case whose hash expired)."""
    pivots_key = _PIVOTS_KEY.format(project_id=project_id)
    try:
        members = _sync(_REDIS.smembers(pivots_key))
    except redis.RedisError as exc:
        logger.warning("command registry unavailable project=%s", project_id)
        raise CommandRegistryUnavailable from exc
    return any(str(member) != str(pivot_id) for member in members)


def entries_by_pivot(project_id: int) -> dict[int, list[KeptCommand]]:
    """Every registered entry of the project by case (pivot id), whatever
    its claim; the indexes of expired hashes are pruned on the way."""
    pivots_key = _PIVOTS_KEY.format(project_id=project_id)
    try:
        members = _sync(_REDIS.smembers(pivots_key))
    except redis.RedisError as exc:
        logger.warning("command registry unavailable project=%s", project_id)
        raise CommandRegistryUnavailable from exc
    result: dict[int, list[KeptCommand]] = {}
    for member in members:
        if not str(member).isdigit():
            continue
        pivot_id = int(member)
        entries = _read_claim(project_id, pivot_id)
        if entries:
            result[pivot_id] = entries
            continue
        claim_key = _CLAIM_KEY.format(project_id=project_id, pivot_id=pivot_id)
        try:
            _prune(keys=[claim_key, pivots_key], args=[str(pivot_id)])
        except redis.RedisError:
            logger.warning("command registry prune failed project=%s", project_id)
    if not result:
        try:
            _prune(keys=[pivots_key, _PROJECTS_KEY], args=[str(project_id)])
        except redis.RedisError:
            logger.warning("command registry prune failed project=%s", project_id)
    return result


def _read_claim(project_id: int, pivot_id: int) -> list[KeptCommand]:
    claim_key = _CLAIM_KEY.format(project_id=project_id, pivot_id=pivot_id)
    try:
        raw: dict[str, str] = _sync(_REDIS.hgetall(claim_key))
    except redis.RedisError as exc:
        logger.warning("command registry unavailable project=%s", project_id)
        raise CommandRegistryUnavailable from exc
    entries: list[KeptCommand] = []
    for session_id, value in raw.items():
        entry = _parse(session_id, value)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse(session_id: str, raw: str) -> KeptCommand | None:
    """The entry, or None for a value this module did not write (also one
    written before entries named their runner)."""
    try:
        data = json.loads(raw)
        runner_id = data["runner_id"]
        if not isinstance(runner_id, int) or isinstance(runner_id, bool):
            raise TypeError("runner_id")
        return KeptCommand(
            session_id=session_id,
            runner_id=RunnerId(runner_id),
            claim_started_at=datetime.fromisoformat(str(data["claim_started_at"])),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            detached=bool(data["detached"]),
            command_head=str(data["command_head"]),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("command registry entry unreadable")
        return None


def _sync(value: Awaitable[_T] | _T) -> _T:
    """A synchronous client's reply (redis-py types every command as maybe
    awaitable)."""
    if isinstance(value, Awaitable):
        raise TypeError("the command registry uses a synchronous Redis client")
    return value
