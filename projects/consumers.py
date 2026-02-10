from __future__ import annotations

import json
from typing import Any

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer

from accounts.models import CustomUser
from projects.models import Project, TestCaseUpload
from projects.services import get_project_for_user


class UploadProgressConsumer(WebsocketConsumer):  # type: ignore[misc]
    group_name: str
    project: Project

    def connect(self) -> None:
        user: CustomUser = self.scope["user"]
        if not user.is_authenticated:
            self.close()
            return

        project_id: int = self.scope["url_route"]["kwargs"]["project_id"]
        project = get_project_for_user(project_id, user)
        if project is None:
            self.close()
            return

        self.project = project
        self.group_name = f"upload_{project_id}"
        async_to_sync(self.channel_layer.group_add)(
            self.group_name,
            self.channel_name,
        )
        self.accept()
        self._send_current_state()

    def disconnect(self, close_code: int) -> None:
        if hasattr(self, "group_name"):
            async_to_sync(self.channel_layer.group_discard)(
                self.group_name,
                self.channel_name,
            )

    def upload_progress(self, event: dict[str, Any]) -> None:
        self.send(
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

    def _send_current_state(self) -> None:
        uploads: list[TestCaseUpload] = list(
            TestCaseUpload.objects.filter(project=self.project)
        )
        for upload in uploads:
            self.send(
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
