"""Answer a person's question about the step they are performing (Ask AI).

The advisor explains; it never acts on the machine and never changes a step,
case or run. When the project has ready knowledge base items it first reads
the knowledge base in a small bounded loop of its own (read-only tools,
dispatched only through ``dispatch_knowledge_tool`` for the context's project),
then answers. Every call gets the remaining part of one time budget, so a slow
provider fails the question instead of outliving the Celery time limit. This
module knows nothing of the ORM.
"""

from __future__ import annotations

import logging
import time
from typing import Final

from django.conf import settings

from agents.exceptions import AIServiceError, AIServiceErrorKind
from agents.services.llm_client import ToolChoice, send_chat_completion
from agents.services.person_step_advice_prompts import (
    ANSWER_NOW_PROMPT,
    build_advice_system_prompt,
    build_advice_user_prompt,
)
from agents.services.person_step_prompts import cap_text
from agents.types import (
    ChatMessage,
    LLMConfig,
    PersonStepAdviceContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from knowledge_base.services.agent_tools import (
    dispatch_knowledge_tool,
    knowledge_tool_definitions,
)

logger = logging.getLogger(__name__)

# Same cap as ``projects.models.PERSON_STEP_ANSWER_MAX_LENGTH`` (agents never
# import projects); projects truncates again when it stores the answer.
ANSWER_MAX_CHARS: Final = 4000
# Seconds of the budget kept for the final answer: a look-up call gets the
# remaining time minus this, and no look-up starts with less than
# ``_MIN_LOOKUP_SECONDS`` left for it.
# Keep in step with the "TIMEOUT_SECONDS < 60" check in auto_tester/settings.py:
# that minimum must exceed their sum, or no look-up round could ever run.
_ANSWER_RESERVE_SECONDS: Final = 40
_MIN_LOOKUP_SECONDS: Final = 5
# Tool calls run per look-up round; extra calls in one response are refused.
_MAX_TOOL_CALLS_PER_ROUND: Final = 3
_TOO_MANY_CALLS = "Too many look-ups in one round; search again if needed."


def answer_person_step_question(
    llm: LLMConfig,
    context: PersonStepAdviceContext,
    question: str,
    *,
    deadline: float,
    max_tool_rounds: int,
) -> str:
    """The advisor's answer to ``question``, capped at ``ANSWER_MAX_CHARS``.

    ``deadline`` is a ``time.monotonic()`` value. With ``max_tool_rounds`` > 0,
    a knowledge base (``context.knowledge_index``) and time for at least one
    look-up round, it takes up to that many rounds (the first one must call a
    tool, at most ``_MAX_TOOL_CALLS_PER_ROUND`` calls run per round), then
    answers; otherwise one call without tools. Every LLM call's timeout is the
    time left before ``deadline`` (look-ups keep ``_ANSWER_RESERVE_SECONDS``
    for the answer; the answer call gets at least 1 s), so the advisor ends
    about 1 s past ``deadline`` at most, plus the knowledge base queries run
    in the last round (not time-limited; the task's soft time limit is the
    backstop). An ``AIServiceError`` (the provider failed, or answered with no
    text) propagates; the caller stores the question as failed.
    """
    use_tools = (
        max_tool_rounds > 0
        and bool(context.knowledge_index)
        and _remaining(deadline) - _ANSWER_RESERVE_SECONDS >= _MIN_LOOKUP_SECONDS
    )
    messages: list[ChatMessage] = [
        ChatMessage(
            role="system",
            content=build_advice_system_prompt(context, knowledge_tools=use_tools),
        ),
        ChatMessage(role="user", content=build_advice_user_prompt(context, question)),
    ]
    if not use_tools:
        return _final_answer(llm, messages, (), deadline=deadline)

    tools = knowledge_tool_definitions(include_list=True)
    names = frozenset(tool.name for tool in tools)
    for round_index in range(max_tool_rounds):
        call_timeout = _remaining(deadline) - _ANSWER_RESERVE_SECONDS
        if call_timeout < _MIN_LOOKUP_SECONDS:
            break
        # The knowledge base is read on every question: the first round must
        # call a tool (only the knowledge base tools are offered).
        tool_choice: ToolChoice = "required" if round_index == 0 else "auto"
        response = send_chat_completion(
            llm,
            tuple(messages),
            tools,
            tool_choice=tool_choice,
            timeout_seconds=call_timeout,
        )
        tool_calls = response.message.tool_calls
        if not tool_calls:
            return _answer_text(response.message)
        messages.append(response.message)
        for index, tool_call in enumerate(tool_calls):
            result = (
                _run_knowledge_tool(tool_call, names, context.project_id)
                if index < _MAX_TOOL_CALLS_PER_ROUND
                else ToolResult(
                    tool_call_id=tool_call.tool_call_id,
                    content=_TOO_MANY_CALLS,
                    is_error=True,
                )
            )
            messages.append(
                ChatMessage(
                    role="tool",
                    content=cap_text(
                        result.content, max(1, settings.AGENT_TOOL_RESULT_MAX_CHARS)
                    ),
                    tool_call_id=result.tool_call_id,
                )
            )
    messages.append(ChatMessage(role="user", content=ANSWER_NOW_PROMPT))
    return _final_answer(llm, messages, tools, deadline=deadline)


def _final_answer(
    llm: LLMConfig,
    messages: list[ChatMessage],
    tools: tuple[ToolDefinition, ...],
    *,
    deadline: float,
) -> str:
    """One call without tool use, with the rest of the budget."""
    response = send_chat_completion(
        llm,
        tuple(messages),
        tools,
        tool_choice="none",
        timeout_seconds=max(1.0, _remaining(deadline)),
    )
    return _answer_text(response.message)


def _answer_text(message: ChatMessage) -> str:
    content = message.content
    answer = cap_text(content, ANSWER_MAX_CHARS) if isinstance(content, str) else ""
    if not answer:
        raise AIServiceError(
            AIServiceErrorKind.BAD_RESPONSE, "The AI service returned an empty answer."
        )
    return answer


def _run_knowledge_tool(
    tool_call: ToolCall, names: frozenset[str], project_id: int
) -> ToolResult:
    """Run one offered knowledge base tool; any other name is refused."""
    if tool_call.tool_name not in names:
        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            content=f"Unknown tool: {tool_call.tool_name[:80]}",
            is_error=True,
        )
    arguments = tool_call.arguments
    query = arguments.get("query")
    item_id = arguments.get("item_id", arguments.get("archive_id"))
    # Never the query text: it may carry what the person typed.
    logger.info(
        "Ask AI KB: %s item=%s query_chars=%d",
        tool_call.tool_name,
        item_id if isinstance(item_id, int) else "-",
        len(query) if isinstance(query, str) else 0,
    )
    return dispatch_knowledge_tool(tool_call, project_id)


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()
