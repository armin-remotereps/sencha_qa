from __future__ import annotations

from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase

from agents.services import agent_loop
from agents.types import (
    AgentConfig,
    ChatMessage,
    LLMConfig,
    LLMResponse,
    ToolCall,
    ToolContext,
    ToolResult,
)

_LLM = LLMConfig(
    model="m",
    api_key="k",
    endpoint_url="http://llm.test",
    temperature=0.0,
    max_tokens=10,
)


def _response(message: ChatMessage, finish_reason: str) -> LLMResponse:
    return LLMResponse(
        message=message,
        finish_reason=finish_reason,
        usage_prompt_tokens=1,
        usage_completion_tokens=1,
    )


def _passthrough(messages: list[ChatMessage], **kwargs: Any) -> list[ChatMessage]:
    return messages


class AgentLoopDeadlineTests(SimpleTestCase):
    def test_tool_context_carries_step_deadline(self) -> None:
        tool_turn = _response(
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=(ToolCall("c1", "wait", {"seconds": 1}),),
            ),
            "tool_calls",
        )
        final_turn = _response(
            ChatMessage(role="assistant", content="RESULT: PASS"), "stop"
        )
        seen: list[ToolContext] = []

        def fake_dispatch(tool_call: ToolCall, context: ToolContext) -> ToolResult:
            seen.append(context)
            return ToolResult(
                tool_call_id=tool_call.tool_call_id, content="ok", is_error=False
            )

        with (
            patch.object(
                agent_loop,
                "send_chat_completion",
                side_effect=[tool_turn, final_turn],
            ),
            patch.object(agent_loop, "dispatch_tool_call", side_effect=fake_dispatch),
            patch.object(
                agent_loop, "summarize_context_if_needed", side_effect=_passthrough
            ),
            patch("agents.services.agent_loop.time.monotonic", return_value=1000.0),
        ):
            agent_loop._run_agent_loop(
                "task",
                ToolContext(project_id=1),
                config=AgentConfig(llm=_LLM, timeout_seconds=600),
                system_prompt="sys",
            )

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].deadline, 1600.0)
