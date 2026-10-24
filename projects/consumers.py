from __future__ import annotations

import json
from typing import Any

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import dateformat

from accounts.models import CustomUser
from projects.case_step_progress import pivot_step_payload
from projects.models import (
    Project,
    TestCaseUpload,
    TestRun,
    TestRunScreenshot,
    TestRunTestCase,
)
from projects.services import (
    _agent_status_group,
    fetch_test_case_state,
    fetch_test_run_state,
    get_blocked_reason_fields,
    get_project_for_user,
)
from projects.testrail_checked_push_services import checked_push_display_status
from visual_engine.services.pool_status import (
    VISUAL_ENGINE_GROUP,
    get_visual_engine_status,
)


class AuthenticatedConsumer(AsyncWebsocketConsumer):
    group_name: str

    async def connect(self) -> None:
        user: CustomUser = self.scope["user"]
        if not user.is_authenticated:
            await self.close()
            return

        if not await self._authorize():
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._send_initial_state()

    async def disconnect(self, close_code: int) -> None:
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def _authorize(self) -> bool:
        raise NotImplementedError

    async def _send_initial_state(self) -> None:
        raise NotImplementedError

    async def _get_project(self) -> Project | None:
        user: CustomUser = self.scope["user"]
        project_id: int = self.scope["url_route"]["kwargs"]["project_id"]
        return await sync_to_async(get_project_for_user)(project_id, user)


class UploadProgressConsumer(AuthenticatedConsumer):
    project: Project

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False

        self.project = project
        project_id: int = self.scope["url_route"]["kwargs"]["project_id"]
        self.group_name = f"upload_{project_id}"
        return True

    async def upload_progress(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "upload_id": event["upload_id"],
                    "status": event["status"],
                    "total_cases": event["total_cases"],
                    "processed_cases": event["processed_cases"],
                    "error_message": event["error_message"],
                }
            )
        )

    async def _send_initial_state(self) -> None:
        def _get_uploads() -> list[TestCaseUpload]:
            return list(TestCaseUpload.objects.filter(project=self.project))

        uploads: list[TestCaseUpload] = await sync_to_async(_get_uploads)()
        for upload in uploads:
            await self.send(
                text_data=json.dumps(
                    {
                        "upload_id": upload.id,
                        "status": upload.status,
                        "total_cases": upload.total_cases,
                        "processed_cases": upload.processed_cases,
                        "error_message": upload.error_message,
                    }
                )
            )


class TestRunConsumer(AuthenticatedConsumer):
    _test_run_id: int

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False

        self._test_run_id = self.scope["url_route"]["kwargs"]["test_run_id"]
        exists: bool = await sync_to_async(
            TestRun.objects.filter(id=self._test_run_id, project=project).exists
        )()
        if not exists:
            return False

        self.group_name = f"test_run_{self._test_run_id}"
        return True

    async def _send_initial_state(self) -> None:
        test_run, summary, pivots, help_state = await sync_to_async(
            fetch_test_run_state
        )(self._test_run_id)
        await self.send(
            text_data=json.dumps(
                {
                    "type": "current_state",
                    "test_run_status": test_run.status,
                    "summary": summary,
                    "help_state": help_state,
                    "pivots": [
                        {"pivot_id": pid, "status": status} for pid, status in pivots
                    ],
                    "testrail_push_status": test_run.testrail_push_status,
                    "testrail_source_push_status": (
                        checked_push_display_status(test_run)
                    ),
                }
            )
        )

    async def test_run_pivot_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "pivot_status",
                    "pivot_id": event["pivot_id"],
                    "status": event["status"],
                    "summary": event["summary"],
                }
            )
        )

    async def test_run_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "test_run_status",
                    "test_run_status": event["test_run_status"],
                    "summary": event["summary"],
                    "help_state": event.get("help_state", "none"),
                }
            )
        )

    async def test_run_testrail_push(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "testrail_push",
                    "push_status": event["push_status"],
                }
            )
        )

    async def test_run_testrail_source_push(self, event: dict[str, Any]) -> None:
        """Relay the "Send checked to TestRail" status (pushing/pushed/failed)."""
        await self.send(
            text_data=json.dumps(
                {
                    "type": "testrail_source_push",
                    "push_status": event["push_status"],
                }
            )
        )


class TestRunCaseConsumer(AuthenticatedConsumer):
    _pivot_id: int

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False

        self._pivot_id = self.scope["url_route"]["kwargs"]["pivot_id"]
        exists: bool = await sync_to_async(
            TestRunTestCase.objects.filter(
                id=self._pivot_id, test_run__project=project
            ).exists
        )()
        if not exists:
            return False

        self.group_name = f"test_run_case_{self._pivot_id}"
        return True

    async def _send_initial_state(self) -> None:
        pivot, screenshots = await sync_to_async(fetch_test_case_state)(self._pivot_id)
        blocked_reason, blocked_reason_label = get_blocked_reason_fields(pivot)
        await self.send(
            text_data=json.dumps(
                {
                    "type": "current_state",
                    "status": pivot.status,
                    "result": pivot.result,
                    "blocked_reason": blocked_reason,
                    "blocked_reason_label": blocked_reason_label,
                    "logs": pivot.logs,
                    "steps": pivot_step_payload(pivot),
                    "screenshots": [
                        {
                            "screenshot_id": s.id,
                            "image_url": s.image.url,
                            "tool_name": s.tool_name,
                            "created_at": dateformat.format(s.created_at, "M d, H:i"),
                        }
                        for s in screenshots
                    ],
                }
            )
        )

    async def test_run_case_log(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "log",
                    "message": event["message"],
                }
            )
        )

    async def test_run_case_screenshot(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "screenshot",
                    "screenshot_id": event["screenshot_id"],
                    "image_url": event["image_url"],
                    "tool_name": event["tool_name"],
                    "created_at": event["created_at"],
                }
            )
        )

    async def test_run_case_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "status",
                    "status": event["status"],
                    "result": event["result"],
                    "blocked_reason": event["blocked_reason"],
                    "blocked_reason_label": event["blocked_reason_label"],
                    "steps": event["steps"],
                }
            )
        )

    async def test_run_case_steps(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps({"type": "steps", "steps": event["steps"]})
        )


class AgentStatusConsumer(AuthenticatedConsumer):

    _project_id: int

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False

        self._project_id = project.id
        self.group_name = _agent_status_group(project.id)
        return True

    async def _send_initial_state(self) -> None:
        # Join before reading state so no engine update is missed in between.
        await self.channel_layer.group_add(VISUAL_ENGINE_GROUP, self.channel_name)
        project_id = self._project_id

        def _fetch_project() -> dict[str, Any]:
            p = Project.objects.get(pk=project_id)
            return {
                "agent_connected": p.agent_connected,
                "agent_system_info": p.agent_system_info,
                "agent_client_version": p.agent_client_version,
                "agent_capabilities": p.agent_capabilities,
                "last_connected_at": (
                    p.last_connected_at.isoformat() if p.last_connected_at else None
                ),
            }

        state = await sync_to_async(_fetch_project)()
        visual_engine = await sync_to_async(get_visual_engine_status)()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "agent_status",
                    **state,
                    "visual_engine": visual_engine.to_payload(),
                }
            )
        )

    async def disconnect(self, close_code: int) -> None:
        await super().disconnect(close_code)
        await self.channel_layer.group_discard(VISUAL_ENGINE_GROUP, self.channel_name)

    async def agent_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "agent_status",
                    "agent_connected": event["agent_connected"],
                    "agent_system_info": event["agent_system_info"],
                    "agent_client_version": event["agent_client_version"],
                    "agent_capabilities": event["agent_capabilities"],
                    "last_connected_at": event["last_connected_at"],
                }
            )
        )

    async def visual_engine_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {"type": "visual_engine", "visual_engine": event["visual_engine"]}
            )
        )

    async def prompt_refined(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "prompt_refined",
                    "refined_prompt": event["refined_prompt"],
                    "error": event["error"],
                }
            )
        )
