"""The grounding of Human in the loop hand-offs ("How to do it").

``refresh_person_guide_notes`` decides whether the case's knowledge base
research is due and runs it; ``build_person_guide_context`` assembles what
one ``prepare_person_step`` call gets. The orchestrator stores the notes and
checkpoints them; this module knows nothing of checkpoints or the ORM.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Final

from django.conf import settings

from agents.case_log import log_line
from agents.services.person_guide_research import NOTE_CITATION, research_person_guide
from agents.types import (
    CancellationCheck,
    LLMConfig,
    LogCallback,
    PersonGuideContext,
    PersonGuideNotes,
    PersonGuideSources,
)

logger = logging.getLogger(__name__)

# Research runs per case at most this often: a failed one is retried once (in
# a later execution), then the case goes on without notes.
MAX_RESEARCH_ATTEMPTS: Final = 2


def research_due(notes: PersonGuideNotes | None) -> bool:
    """Whether the case's research has not run yet, or failed and may retry."""
    return notes is None or (
        notes.status == "failed" and notes.attempts < MAX_RESEARCH_ATTEMPTS
    )


def refresh_person_guide_notes(
    notes: PersonGuideNotes | None,
    sources: PersonGuideSources,
    *,
    llm: LLMConfig,
    task_description: str,
    os_name: str,
    project_id: int,
    on_log: LogCallback | None,
    cancellation_check: CancellationCheck | None,
) -> PersonGuideNotes | None:
    """New notes when research was due and ran (also a failed run, counted);
    None when nothing changed (not due, research off, or no ready knowledge
    base item). ``AgentCancelledError`` propagates."""
    if settings.PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS <= 0 or not research_due(notes):
        return None
    knowledge_index = sources.knowledge_index()
    if knowledge_index is None:
        return None
    researched = research_person_guide(
        llm,
        task_description=task_description,
        os_name=os_name,
        knowledge_index=knowledge_index,
        sources=sources,
        project_id=project_id,
        on_log=on_log,
        cancellation_check=cancellation_check,
    )
    return replace(researched, attempts=1 if notes is None else notes.attempts + 1)


def build_person_guide_context(
    notes: PersonGuideNotes | None,
    sources: PersonGuideSources,
    *,
    related_text: str,
    on_log: LogCallback | None,
) -> PersonGuideContext | None:
    """The references of one hand-off; None when the project has none.

    The ids the model may cite are the excerpts', the notes' and the
    profile's, resolved in one look-up.
    """
    research_notes = notes.text if notes is not None and notes.status == "ready" else ""
    profile = sources.artifacts_profile() or ""
    related = sources.related(related_text)
    if not (research_notes.strip() or profile.strip() or related.text.strip()):
        return None
    cited = set(related.item_names) | {
        int(match) for match in NOTE_CITATION.findall(research_notes)
    }
    if profile.strip():
        cited |= set(sources.profile_item_ids())
    allowed = sources.resolve_items(tuple(sorted(cited))) if cited else {}
    present = [
        label
        for label, text in (
            ("artifacts profile", profile),
            ("research notes", research_notes),
            ("knowledge excerpts", related.text),
        )
        if text.strip()
    ]
    log_line(on_log, f"[Orchestrator]   Guide references: {', '.join(present)}", logger)
    return PersonGuideContext(
        artifacts_profile=profile,
        research_notes=research_notes,
        knowledge_excerpts=related.text,
        allowed_items=allowed,
    )
