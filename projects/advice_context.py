"""The Ask AI advisor's context for one step, whatever the step's source.

The shared seam: the Human in the loop mapper
(``projects.person_step_question_services``) passes an
``AdviceStep(source="plan")``; a Manual run can pass an
``AdviceStep(source="case")`` for a verbatim case step. This module adds what
every source shares: the runner's OS and shell, the case text, the application
context, the artifacts profile, the knowledge base index and the knowledge
base excerpts for the question.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from agents.os_shell import get_os_name, shell_label
from agents.types import AdviceEarlierStep, AdviceStep, PersonStepAdviceContext
from knowledge_base.exceptions import KnowledgeBaseError
from knowledge_base.services.knowledge_index import knowledge_index_text
from knowledge_base.services.related_knowledge import related_knowledge
from knowledge_base.tool_text import render_excerpts
from machine_readiness.profile_text import artifacts_profile_text
from projects.case_task_text import build_task_description
from projects.models import TestRunTestCase
from projects.services import build_agent_project_context

logger = logging.getLogger(__name__)

_PROJECT_CONTEXT_MAX_LENGTH: Final = 4000
# Defensive: the profile is already capped when it is written.
_PROFILE_MAX_CHARS: Final = 8000
# Knowledge base excerpts given to the advisor (one query).
_KNOWLEDGE_EXCERPT_LIMIT: Final = 4
_KNOWLEDGE_EXCERPT_MAX_CHARS: Final = 6000


def build_advice_context(
    *,
    pivot: TestRunTestCase,
    step: AdviceStep,
    earlier_steps: Sequence[AdviceEarlierStep],
    history: Sequence[tuple[str, str]],
    question: str,
) -> PersonStepAdviceContext:
    """The advisor's context for ``step`` of ``pivot``'s case.

    ``pivot`` must have ``test_case`` and ``test_run__project`` loaded.
    Queries: the artifacts profile (2–3), the knowledge base index (1) and the
    excerpts (1–2). A knowledge base problem leaves its parts empty.
    """
    project = pivot.test_run.project
    system_info = project.agent_system_info
    os_name = get_os_name(system_info if isinstance(system_info, dict) else None)
    project_context = build_agent_project_context(project) or ""
    knowledge_index = knowledge_index_text(project.pk) or ""
    return PersonStepAdviceContext(
        os_name=os_name,
        shell_label=shell_label(os_name),
        case_text=build_task_description(pivot.test_case),
        project_prompt=project_context[:_PROJECT_CONTEXT_MAX_LENGTH],
        step=step,
        project_id=project.pk,
        earlier_steps=tuple(earlier_steps),
        artifacts_profile=(artifacts_profile_text(project.pk) or "")[
            :_PROFILE_MAX_CHARS
        ],
        knowledge_index=knowledge_index,
        history=tuple(history),
        # Prefetched even though the advisor can search: they are its only
        # knowledge base text when the look-up loop is skipped (rounds set to
        # 0, too little time left) and a head start for the first search.
        knowledge_excerpts=(
            _knowledge_excerpts(
                project.pk, _excerpt_query(question, pivot.test_case.title, step)
            )
            if knowledge_index
            else ""
        ),
    )


def _excerpt_query(question: str, case_title: str, step: AdviceStep) -> str:
    """Question first: ``related_knowledge`` keeps only the leading words."""
    covered = "\n".join(item.text for item in step.covered_case_steps)
    return "\n".join(
        part for part in (question, case_title, covered, step.instruction) if part
    )


def _knowledge_excerpts(project_id: int, text: str) -> str:
    """The rendered knowledge base excerpts related to ``text``; "" for none.

    A knowledge base problem never keeps the question from being answered.
    """
    try:
        excerpts = related_knowledge(
            project_id,
            text,
            limit=_KNOWLEDGE_EXCERPT_LIMIT,
            max_chars=_KNOWLEDGE_EXCERPT_MAX_CHARS,
        )
    except KnowledgeBaseError as exc:
        logger.warning("Ask AI knowledge excerpts skipped: %s", exc.message)
        return ""
    return render_excerpts(excerpts)
