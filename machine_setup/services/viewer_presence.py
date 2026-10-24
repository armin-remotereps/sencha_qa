"""Whether a person is looking at a setup conversation right now.

A chat tab that is open, connected and visible sends a presence heartbeat;
each one (re)writes a short-lived cache key per conversation and user, holding
the socket's channel name. The key expires on its own when the tab goes away
without saying so (a crashed Daphne process, a dropped connection), so it
works across any number of web processes without counting sockets.
"""

from __future__ import annotations

from django.core.cache import cache

# The page sends a heartbeat every 15 s while visible; three missed ones expire.
PRESENCE_TTL_SECONDS = 45


def _key(conversation_id: int, user_id: int) -> str:
    return f"machine_setup:viewing:{conversation_id}:{user_id}"


async def mark_viewing(conversation_id: int, user_id: int, channel_name: str) -> None:
    """Record that this socket shows the conversation in a visible tab."""
    await cache.aset(
        _key(conversation_id, user_id), channel_name, timeout=PRESENCE_TTL_SECONDS
    )


async def clear_viewing(conversation_id: int, user_id: int, channel_name: str) -> None:
    """Forget this socket's presence; another socket's key is left alone.

    Get-then-delete is not atomic: if another visible tab rewrote the key in
    between it is lost until that tab's next heartbeat (at most 15 s).
    """
    key = _key(conversation_id, user_id)
    if await cache.aget(key) == channel_name:
        await cache.adelete(key)


async def update_viewing(
    *,
    previous_id: int | None,
    conversation_id: int,
    visible: bool,
    user_id: int,
    channel_name: str,
) -> int | None:
    """Apply one presence frame; returns the conversation this socket now views.

    A socket views one conversation at a time: marking a new one first clears
    the one it marked before (the main chat adopts a new id after a reset).
    """
    if not visible:
        await clear_viewing(conversation_id, user_id, channel_name)
        return None if previous_id == conversation_id else previous_id
    if previous_id is not None and previous_id != conversation_id:
        await clear_viewing(previous_id, user_id, channel_name)
    await mark_viewing(conversation_id, user_id, channel_name)
    return conversation_id


def is_viewing(conversation_id: int, user_id: int) -> bool:
    """True while some tab of the user shows the conversation in the foreground."""
    return cache.get(_key(conversation_id, user_id)) is not None
