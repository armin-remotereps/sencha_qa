from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    PERSON_STEP_ACTION_MAX_BYTES,
    ClientCapability,
    CloseCode,
    ErrorCode,
    MessageType,
    PersonStepActionKind,
    PersonStepActionResultPayload,
    PersonStepStatePayload,
    parse_handshake_capabilities,
    parse_person_email,
    parse_update_in_flight_id,
    parse_update_status_payload,
    serialize_person_step_action_result,
    serialize_person_step_state,
)
from projects.controller_authenticator import (
    ControllerAuthenticator,
    HandshakeMessageBuilder,
    HandshakeStatus,
)
from projects.controller_protocol import (
    ActionTypeRegistry,
    BaseActionEvent,
    BrowserClickActionEvent,
    BrowserDoubleClickActionEvent,
    BrowserDownloadActionEvent,
    BrowserGetElementsActionEvent,
    BrowserGetPageContentActionEvent,
    BrowserGetUrlActionEvent,
    BrowserHoverActionEvent,
    BrowserListDownloadsActionEvent,
    BrowserNavigateActionEvent,
    BrowserTakeScreenshotActionEvent,
    BrowserTypeActionEvent,
    CheckAppInstalledActionEvent,
    CleanupEnvironmentActionEvent,
    ClickActionEvent,
    CloseAppActionEvent,
    CloseTabActionEvent,
    ControllerMessageBuilder,
    DoubleClickActionEvent,
    DragActionEvent,
    HoverActionEvent,
    KeyPressActionEvent,
    LaunchAppActionEvent,
    RunCommandActionEvent,
    ScreenshotActionEvent,
    ScrollActionEvent,
    SendInputActionEvent,
    StartInteractiveCmdActionEvent,
    TerminateInteractiveCmdActionEvent,
    TypeTextActionEvent,
    UpdateRunnerEvent,
    WaitForCommandActionEvent,
)
from projects.controller_reply_tracker import ReplyTracker
from projects.exceptions import PersonStepError
from projects.forms import RunnerPersonStepActionForm, form_error_text
from projects.models import Project
from projects.person_step_services import PersonStepActionResult
from projects.runner_person_step_services import (
    RunnerPersonStepAnswer,
    answer_from_runner,
    get_runner_person_steps,
    resolve_runner_person,
)
from projects.runner_update_progress import (
    record_runner_update_status,
    settle_runner_update_on_handshake,
)
from projects.services import broadcast_agent_status, release_agent_connection

logger = logging.getLogger(__name__)

# What the Test Runner window says after an answer was saved and the run
# continues (the web card's flash texts).
_PERSON_STEP_DONE: dict[PersonStepActionKind, str] = {
    PersonStepActionKind.REPORT: "Reported. The agent is checking it.",
    PersonStepActionKind.OVERRULE: "Overruled. The step is recorded as passed.",
    PersonStepActionKind.FAIL: "Marked the case failed. The run continues.",
    PersonStepActionKind.BLOCK: "Marked the case blocked. The run continues.",
}
_PERSON_STEPS_UNSUPPORTED = "This Test Runner did not offer person steps."
_PERSON_STEP_TOO_LARGE = "The answer is too long."
_PERSON_STEP_FAILED = "The answer could not be saved. Answer on Punk Hazard."
# Longest ``request_id`` echoed back; a longer one gets a fresh id.
_MAX_REQUEST_ID_LENGTH = 128


class ControllerConsumer(AsyncWebsocketConsumer):
    _authenticated: bool
    _project: Project | None
    _reply_tracker: ReplyTracker
    _authenticator: ControllerAuthenticator
    _message_builder: ControllerMessageBuilder
    # The runner shows person steps (it advertised PERSON_STEPS_V1).
    _person_steps_enabled: bool
    # The active member the handshake's ``person_email`` named; re-checked
    # when an answer arrives. The email itself is never kept.
    _person_user_id: int | None
    # The last ``person_step_state`` sent; None before the first one.
    _person_steps_sent: tuple[PersonStepStatePayload, ...] | None

    async def connect(self) -> None:
        self._authenticated = False
        self._project = None
        self._person_steps_enabled = False
        self._person_user_id = None
        self._person_steps_sent = None
        self._reply_tracker = ReplyTracker(self.channel_layer)
        self._authenticator = ControllerAuthenticator()
        self._message_builder = ControllerMessageBuilder(ActionTypeRegistry())
        await self.accept()

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        if text_data is None:
            return

        try:
            data: dict[str, Any] = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON")
            return

        if not self._authenticated:
            await self._handle_unauthenticated_message(data)
            return

        await self._route_authenticated_message(data, len(text_data))

    async def disconnect(self, close_code: int) -> None:
        if self._authenticated and self._project is not None:
            await self._cleanup_connection()

    async def _handle_unauthenticated_message(self, data: dict[str, Any]) -> None:
        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        if msg_type == "handshake":
            await self._handle_handshake(data, request_id)
        else:
            await self._send_error(
                "Not authenticated. Send handshake first.", request_id
            )
            await self.close()

    async def _route_authenticated_message(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        # Unknown types fall through silently, including the
        # ``omniparser_status`` reports a 0.5.x Test Runner still sends.
        if msg_type == "pong":
            return

        if msg_type == MessageType.UPDATE_STATUS:
            await self._handle_update_status(data)
            return

        if msg_type == MessageType.PERSON_STEP_ACTION:
            await self._handle_person_step_action(data, raw_length)
            return

        handlers = {
            "action_result": self._reply_tracker.send_action_result,
            "screenshot_response": self._reply_tracker.send_screenshot_result,
            "command_output": self._reply_tracker.send_command_output,
            "command_result": self._reply_tracker.send_command_result,
            "browser_content_result": self._reply_tracker.send_browser_content_result,
            "interactive_output": self._reply_tracker.send_interactive_output,
            "error": self._reply_tracker.send_error,
        }

        handler = handlers.get(msg_type)
        if handler:
            await handler(request_id, data)

    async def _handle_update_status(self, data: dict[str, Any]) -> None:
        if self._project is None:
            return
        try:
            status = parse_update_status_payload(data)
        except ProtocolError as exc:
            logger.warning(
                "Ignoring malformed update_status for project %s: %s",
                self._project.id,
                exc,
            )
            return
        await sync_to_async(record_runner_update_status)(
            self._project.id, status.update_id, status.phase, status.detail
        )

    async def _handle_handshake(self, data: dict[str, Any], request_id: str) -> None:
        api_key: str = data.get("api_key", "")
        system_info: dict[str, Any] = data.get("system_info", {})
        client_version: str = data.get("client_version", "")

        try:
            capabilities = parse_handshake_capabilities(data)
            update_in_flight_id = parse_update_in_flight_id(data)
            person_email = parse_person_email(data)
        except ProtocolError as exc:
            await self._send_handshake_ack(
                "error", f"Invalid handshake: {exc}", request_id
            )
            await self.close()
            return

        result = await self._authenticator.authenticate_handshake(
            api_key, system_info, client_version, capabilities
        )

        if not result.success or result.project is None:
            logger.warning(
                "Controller handshake rejected (%s, client_version=%s): %s",
                result.status,
                client_version or "unknown",
                result.reason,
            )
            await self._send_handshake_ack(result.status, result.reason, request_id)
            await self.close()
            return

        # Never echoed and never an error: the runner does not learn whether
        # the email belongs to a member.
        self._person_user_id = await sync_to_async(resolve_runner_person)(
            result.project.id, person_email
        )
        self._person_steps_enabled = (
            ClientCapability.PERSON_STEPS_V1.value in capabilities
        )
        await self._establish_authenticated_connection(
            result.project,
            system_info,
            client_version,
            capabilities,
            update_in_flight_id,
            request_id,
        )

    async def _establish_authenticated_connection(
        self,
        project: Project,
        system_info: dict[str, Any],
        client_version: str,
        capabilities: Sequence[str],
        update_in_flight_id: str | None,
        request_id: str,
    ) -> None:
        self._authenticated = True
        self._project = project

        # Join the group before ownership becomes visible in the DB, so an
        # action dispatched to the new owner always reaches this consumer.
        group_name = self._controller_group_name(project.id)
        await self.channel_layer.group_add(group_name, self.channel_name)
        previous_channel_name = await self._authenticator.claim_connection(
            project, self.channel_name, system_info, client_version, capabilities
        )
        # A runner started by a self-update reports the target version here;
        # an old runner reconnecting mid-update reports the update it is
        # installing; any other runner ends the in-flight update as lost.
        # Settling before the old runner is superseded keeps the panel from
        # showing a lost update in between.
        await sync_to_async(settle_runner_update_on_handshake)(
            project.id, client_version, update_in_flight_id
        )
        if previous_channel_name:
            # Drop the replaced connection from the group even if its consumer
            # is gone (e.g. Daphne restarted), then ask it to close if alive.
            await self.channel_layer.group_discard(group_name, previous_channel_name)
            await self.channel_layer.send(
                previous_channel_name, {"type": "controller.superseded"}
            )
        await self._authenticator.broadcast_status(project)
        await self._send_handshake_ack(
            status="ok",
            message="Connected",
            request_id=request_id,
            project_id=str(project.id),
            project_name=project.name,
        )
        # Covers a reconnect or a runner restart while a step waits.
        await self._push_person_steps()

    async def _cleanup_connection(self) -> None:
        if self._project is None:
            return

        await self.channel_layer.group_discard(
            self._controller_group_name(self._project.id), self.channel_name
        )
        # Only the current owner clears the connection; a superseded consumer
        # releases nothing. A running test waits for the runner to reconnect.
        released = await sync_to_async(release_agent_connection)(
            self._project.id, self.channel_name
        )
        # Wake the waiters only after ownership is released, so their
        # wait_for_controller never hands this dead channel back to them.
        await self._reply_tracker.fail_pending_replies(
            ErrorCode.CONTROLLER_DISCONNECTED, "Test Runner connection lost"
        )
        if released is not None:
            await sync_to_async(broadcast_agent_status)(released)

    async def controller_superseded(self, event: dict[str, Any]) -> None:
        await self.close(code=CloseCode.SUPERSEDED)

    async def controller_force_disconnect(self, event: dict[str, Any]) -> None:
        await self.close(code=CloseCode.FORCE_DISCONNECTED)

    async def controller_person_steps_changed(self, event: dict[str, Any]) -> None:
        await self._push_person_steps()

    async def _push_person_steps(self, *, force: bool = False) -> None:
        """Send the open person steps when they changed since the last send.

        Only to a runner that advertised PERSON_STEPS_V1. ``force`` resends an
        unchanged snapshot (after a refused answer the window must re-sync).
        """
        if not self._person_steps_enabled or self._project is None:
            return
        try:
            views = await sync_to_async(get_runner_person_steps)(self._project.id)
        except Exception:
            logger.exception(
                "Reading person steps failed for project %s", self._project.id
            )
            return
        steps = tuple(view.to_payload() for view in views)
        # The first push of a connection always goes out (even empty), so a
        # reconnected runner drops a step answered meanwhile.
        if not force and steps == self._person_steps_sent:
            return
        self._person_steps_sent = steps
        await self.send(text_data=serialize_person_step_state(steps))

    async def _handle_person_step_action(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        raw_request_id = data.get("request_id")
        request_id = (
            raw_request_id
            if isinstance(raw_request_id, str)
            and 0 < len(raw_request_id) <= _MAX_REQUEST_ID_LENGTH
            else ""
        )
        result = await self._answer_person_step(data, raw_length)
        await self.send(
            text_data=serialize_person_step_action_result(result, request_id)
        )
        if not result.ok:
            await self._push_person_steps(force=True)

    async def _answer_person_step(
        self, data: dict[str, Any], raw_length: int
    ) -> PersonStepActionResultPayload:
        if not self._person_steps_enabled or self._project is None:
            return _refused(_PERSON_STEPS_UNSUPPORTED)
        if raw_length > PERSON_STEP_ACTION_MAX_BYTES:
            return _refused(_PERSON_STEP_TOO_LARGE)
        form = RunnerPersonStepActionForm(
            {field: data.get(field) for field in RunnerPersonStepActionForm.base_fields}
        )
        if not form.is_valid():
            return _refused(form_error_text(form))
        answer = RunnerPersonStepAnswer(
            action=PersonStepActionKind(form.cleaned_data["action"]),
            person_step_id=form.cleaned_data["person_step_id"],
            round=form.cleaned_data["round"],
            outcome=form.cleaned_data["outcome"],
            note=form.cleaned_data["note"],
            output=form.cleaned_data["output"],
        )
        try:
            outcome = await sync_to_async(answer_from_runner)(
                project_id=self._project.id,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                action=answer,
            )
        except PersonStepError as exc:
            return _refused(exc.message)
        except Exception:
            logger.exception(
                "Person step answer failed for project %s", self._project.id
            )
            return _refused(_PERSON_STEP_FAILED)
        return _answered(answer.action, outcome)

    async def _forward_action(self, event: BaseActionEvent) -> None:
        request_id: str = event.get("request_id") or str(uuid.uuid4())
        reply_channel: str = event.get("reply_channel", "")

        if reply_channel:
            self._reply_tracker.register_reply_channel(request_id, reply_channel)

        message = self._message_builder.build_action_message(event, request_id)
        await self.send(text_data=message)

    async def controller_click(self, event: ClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_double_click(self, event: DoubleClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_hover(self, event: HoverActionEvent) -> None:
        await self._forward_action(event)

    async def controller_scroll(self, event: ScrollActionEvent) -> None:
        await self._forward_action(event)

    async def controller_drag(self, event: DragActionEvent) -> None:
        await self._forward_action(event)

    async def controller_type_text(self, event: TypeTextActionEvent) -> None:
        await self._forward_action(event)

    async def controller_key_press(self, event: KeyPressActionEvent) -> None:
        await self._forward_action(event)

    async def controller_screenshot(self, event: ScreenshotActionEvent) -> None:
        await self._forward_action(event)

    async def controller_run_command(self, event: RunCommandActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_navigate(
        self, event: BrowserNavigateActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_click(self, event: BrowserClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_double_click(
        self, event: BrowserDoubleClickActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_type(self, event: BrowserTypeActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_hover(self, event: BrowserHoverActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_get_elements(
        self, event: BrowserGetElementsActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_get_page_content(
        self, event: BrowserGetPageContentActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_get_url(self, event: BrowserGetUrlActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_take_screenshot(
        self, event: BrowserTakeScreenshotActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_download(
        self, event: BrowserDownloadActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_list_downloads(
        self, event: BrowserListDownloadsActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_start_interactive_cmd(
        self, event: StartInteractiveCmdActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_send_input(self, event: SendInputActionEvent) -> None:
        await self._forward_action(event)

    async def controller_wait_for_command(
        self, event: WaitForCommandActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_terminate_interactive_cmd(
        self, event: TerminateInteractiveCmdActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_launch_app(self, event: LaunchAppActionEvent) -> None:
        await self._forward_action(event)

    async def controller_close_app(self, event: CloseAppActionEvent) -> None:
        await self._forward_action(event)

    async def controller_close_tab(self, event: CloseTabActionEvent) -> None:
        await self._forward_action(event)

    async def controller_check_app_installed(
        self, event: CheckAppInstalledActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_cleanup_environment(
        self, event: CleanupEnvironmentActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_update_runner(self, event: UpdateRunnerEvent) -> None:
        # Fire and forget: progress comes back as ``update_status`` messages.
        await self.send(
            text_data=self._message_builder.build_update_runner_message(event)
        )

    async def _send_handshake_ack(
        self,
        status: HandshakeStatus,
        message: str,
        request_id: str = "",
        project_id: str = "",
        project_name: str = "",
    ) -> None:
        ack_message = HandshakeMessageBuilder.build_handshake_ack(
            status, message, request_id, project_id, project_name
        )
        await self.send(text_data=ack_message)

    async def _send_error(self, message: str, request_id: str = "") -> None:
        error_message = HandshakeMessageBuilder.build_error(message, request_id)
        await self.send(text_data=error_message)

    @staticmethod
    def _controller_group_name(project_id: int) -> str:
        return f"controller_{project_id}"


def _refused(message: str) -> PersonStepActionResultPayload:
    return PersonStepActionResultPayload(ok=False, message=message, queued=False)


def _answered(
    action: PersonStepActionKind, result: PersonStepActionResult
) -> PersonStepActionResultPayload:
    if result.queued:
        return PersonStepActionResultPayload(
            ok=True, message=_PERSON_STEP_DONE[action], queued=True
        )
    return PersonStepActionResultPayload(
        ok=True,
        message=(
            f"Saved, but the run cannot continue yet: {result.blocker} "
            "Then use Continue run on the run page."
        ),
        queued=False,
    )
