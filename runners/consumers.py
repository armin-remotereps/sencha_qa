"""Live Test Runner status for the pages (``runner.status``).

- ``RunnerStatusConsumer`` (``ws/runners/<id>/status/``): one runner, for the
  run card, the setup chat header and the runner page. Open to whoever
  ``runner_access.can_view`` allows. Sends the
  runner's status on connect, then relays every ``runner.status`` of
  ``runner_status_{id}``; the owner also gets the machine details and the
  self-update panel (``runner_update``). Anyone else is re-checked before each
  relay and the socket closes once they may no longer watch (the run that
  held the runner ended or moved); they never get the machine details. The global visual engine state is relayed too, since
  it decides a connected runner's badge.
- ``OwnedRunnersStatusConsumer`` (``ws/runners/status/``): the viewer's own
  runners (``runner_owner_{user_id}``), for the Test Runners list.

Messages sent to the browser:
``{"type": "runner.status", ...RunnerStatusEvent}``,
``{"type": "runner_update", "runner_id", "runner_update"}`` and
``{"type": "visual_engine", "visual_engine"}``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from accounts.models import CustomUser
from projects.runner_update_offer import RunnerUpdatePayload
from runners.channels import runner_owner_group, runner_status_group
from runners.services.runner_status import (
    RunnerStatusEvent,
    RunnerStatusViewer,
    authorize_runner_status_viewer,
    load_runner_status_for_viewer,
    runner_status_viewer_allowed,
    status_event_for_viewer,
)
from runners.types import RunnerId
from visual_engine.services.pool_status import (
    VISUAL_ENGINE_GROUP,
    get_visual_engine_status,
)
from visual_engine.types import VisualEngineStatusPayload

_RUNNER_UPDATE_MESSAGE = "runner_update"
_VISUAL_ENGINE_MESSAGE = "visual_engine"


class _RunnerGroupsConsumer(AsyncWebsocketConsumer):
    """Joins one status group plus the visual engine group; relays both."""

    _group_name: str = ""

    async def disconnect(self, close_code: int) -> None:
        if self._group_name:
            await self.channel_layer.group_discard(self._group_name, self.channel_name)
            await self.channel_layer.group_discard(
                VISUAL_ENGINE_GROUP, self.channel_name
            )

    async def _join(self, group_name: str) -> None:
        self._group_name = group_name
        await self.channel_layer.group_add(group_name, self.channel_name)
        await self.channel_layer.group_add(VISUAL_ENGINE_GROUP, self.channel_name)

    async def runner_status(self, event: dict[str, Any]) -> None:
        # The event is a ``RunnerStatusEvent`` (its ``type`` is "runner.status").
        await self.send(text_data=json.dumps(event))

    async def visual_engine_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": _VISUAL_ENGINE_MESSAGE,
                    "visual_engine": event["visual_engine"],
                }
            )
        )


class RunnerStatusConsumer(_RunnerGroupsConsumer):
    _runner_id: RunnerId
    _user: CustomUser
    _viewer: RunnerStatusViewer

    async def connect(self) -> None:
        user: CustomUser = self.scope["user"]
        if not user.is_authenticated:
            await self.close()
            return
        self._user = user
        self._runner_id = RunnerId(int(self.scope["url_route"]["kwargs"]["runner_id"]))
        viewer = await database_sync_to_async(authorize_runner_status_viewer)(
            user, self._runner_id
        )
        if viewer is None:
            await self.close()
            return
        self._viewer = viewer
        # Join before reading the state so no update is missed in between.
        await self._join(runner_status_group(self._runner_id))
        await self.accept()
        status, update, engine = await database_sync_to_async(self._initial_state)()
        if status is None:
            await self.close()
            return
        await self.send(text_data=json.dumps(status))
        if update is not None:
            await self._send_runner_update(update)
        await self.send(
            text_data=json.dumps(
                {"type": _VISUAL_ENGINE_MESSAGE, "visual_engine": engine}
            )
        )

    def _initial_state(
        self,
    ) -> tuple[
        RunnerStatusEvent | None, RunnerUpdatePayload | None, VisualEngineStatusPayload
    ]:
        engine = get_visual_engine_status()
        state = load_runner_status_for_viewer(self._runner_id, self._viewer, engine)
        if state is None:
            return None, None, engine.to_payload()
        return state.status, state.update, engine.to_payload()

    async def runner_status(self, event: dict[str, Any]) -> None:
        """The event is a ``RunnerStatusEvent`` (its ``type`` is
        "runner.status"); non-owners are re-checked first."""
        if not self._viewer.is_owner:
            allowed = await database_sync_to_async(runner_status_viewer_allowed)(
                self._user, self._runner_id, self._viewer
            )
            if not allowed:
                await self.close()
                return
        status = cast(RunnerStatusEvent, event)
        await self.send(
            text_data=json.dumps(status_event_for_viewer(status, self._viewer))
        )

    async def runner_update(self, event: dict[str, Any]) -> None:
        """``projects.runner_update_offer.RUNNER_UPDATE_EVENT``: owner only."""
        if self._viewer.is_owner:
            payload: RunnerUpdatePayload = event["runner_update"]
            await self._send_runner_update(payload)

    async def _send_runner_update(self, payload: RunnerUpdatePayload) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": _RUNNER_UPDATE_MESSAGE,
                    "runner_id": self._runner_id,
                    "runner_update": payload,
                }
            )
        )


class OwnedRunnersStatusConsumer(_RunnerGroupsConsumer):
    """The signed-in user's own runners (the list page renders the initial
    state; this relays changes)."""

    async def connect(self) -> None:
        user: CustomUser = self.scope["user"]
        if not user.is_authenticated or not user.is_active:
            await self.close()
            return
        await self._join(runner_owner_group(user.pk))
        await self.accept()

    async def runner_update(self, event: dict[str, Any]) -> None:
        """Not sent to the owner group; ignored if it ever is."""
