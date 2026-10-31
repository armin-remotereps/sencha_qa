"""Live item status for a project's Knowledge base tab."""

from __future__ import annotations

import json
from typing import Any

from knowledge_base.events import knowledge_group
from projects.consumers import AuthenticatedConsumer


class KnowledgeBaseConsumer(AuthenticatedConsumer):
    """Any project member may listen; there is no initial state."""

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False
        self.group_name = knowledge_group(project.id)
        return True

    async def _send_initial_state(self) -> None:
        return None

    async def knowledge_base_item(self, event: dict[str, Any]) -> None:
        await self.send(text_data=json.dumps({"type": "item", "item": event["item"]}))
