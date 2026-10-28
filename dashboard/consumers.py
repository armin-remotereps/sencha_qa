"""Per-user attention socket: live nav badge, tab title and dashboard card."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from channels.db import database_sync_to_async

from accounts.models import CustomUser
from dashboard.services.attention import count_attention
from notifications.services.inbox_broadcast import attention_group
from projects.consumers import AuthenticatedConsumer

logger = logging.getLogger(__name__)

# At most one recount per socket in this window; a burst of events inside it
# is coalesced into one trailing send.
ATTENTION_RECOUNT_INTERVAL_SECONDS = 2.0


class AttentionConsumer(AuthenticatedConsumer):
    """Joins ``attention_group(user.pk)`` and sends ``{"type": "attention", "count": N}``.

    ``attention.changed`` events carry no payload; each socket recounts its own
    user's items, so no user's data reaches another user's socket. Client
    frames are ignored.
    """

    _user: CustomUser
    _last_sent_at: float
    _trailing: asyncio.Task[None] | None

    async def _authorize(self) -> bool:
        self._user = self.scope["user"]
        self._last_sent_at = float("-inf")
        self._trailing = None
        self.group_name = attention_group(self._user.pk)
        return True

    async def _send_initial_state(self) -> None:
        # The page already rendered the count.
        return None

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        return None

    async def disconnect(self, close_code: int) -> None:
        trailing = getattr(self, "_trailing", None)
        if trailing is not None:
            trailing.cancel()
            self._trailing = None
        await super().disconnect(close_code)

    async def attention_changed(self, event: dict[str, Any]) -> None:
        if self._trailing is not None:
            return  # a trailing send is already scheduled and will see this change
        loop = asyncio.get_running_loop()
        wait = self._last_sent_at + ATTENTION_RECOUNT_INTERVAL_SECONDS - loop.time()
        if wait > 0:
            self._trailing = loop.create_task(self._send_later(wait))
            return
        await self._send_count()

    async def _send_later(self, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
            self._trailing = None
            await self._send_count()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Attention recount failed for user %s", self._user.pk)

    async def _send_count(self) -> None:
        self._last_sent_at = asyncio.get_running_loop().time()
        count = await database_sync_to_async(count_attention)(self._user)
        await self.send(text_data=json.dumps({"type": "attention", "count": count}))
