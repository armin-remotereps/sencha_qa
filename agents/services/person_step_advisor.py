"""Answer a person's question about the step they are performing (Ask AI).

One tool-free LLM call: the advisor explains, it never acts on the machine and
never changes a step, case or run. This module knows nothing of the ORM.
"""

from __future__ import annotations

from typing import Final

from agents.exceptions import AIServiceError, AIServiceErrorKind
from agents.services.llm_client import send_chat_completion
from agents.services.person_step_advice_prompts import (
    build_advice_system_prompt,
    build_advice_user_prompt,
)
from agents.services.person_step_prompts import cap_text
from agents.types import ChatMessage, LLMConfig, PersonStepAdviceContext

# Same cap as ``projects.models.PERSON_STEP_ANSWER_MAX_LENGTH`` (agents never
# import projects); projects truncates again when it stores the answer.
ANSWER_MAX_CHARS: Final = 4000


def answer_person_step_question(
    llm: LLMConfig,
    context: PersonStepAdviceContext,
    question: str,
    *,
    timeout_seconds: int,
) -> str:
    """The advisor's answer to ``question``, capped at ``ANSWER_MAX_CHARS``.

    An ``AIServiceError`` (the provider failed, or answered with no text)
    propagates; the caller stores the question as failed.
    """
    response = send_chat_completion(
        llm,
        (
            ChatMessage(role="system", content=build_advice_system_prompt(context)),
            ChatMessage(
                role="user", content=build_advice_user_prompt(context, question)
            ),
        ),
        timeout_seconds=timeout_seconds,
    )
    content = response.message.content
    answer = cap_text(content, ANSWER_MAX_CHARS) if isinstance(content, str) else ""
    if not answer:
        raise AIServiceError(
            AIServiceErrorKind.BAD_RESPONSE, "The AI service returned an empty answer."
        )
    return answer
