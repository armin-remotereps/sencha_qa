"""Once-per-case knowledge base research for Human in the loop guides.

``research_person_guide`` runs one bounded knowledge base look-up loop over
the whole test case and returns compact notes (facts with the item and page
they came from). The orchestrator stores them in the checkpoint
``person_guide`` key, so every later hand-off of the case reads them without
another loop. No ORM: the index, the profile and the id check come from the
injected ``PersonGuideSources``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Final

from django.conf import settings
from django.utils import timezone

from agents.case_log import log_line
from agents.exceptions import AIServiceError
from agents.json_values import positive_int
from agents.secret_redaction import redact_secrets
from agents.services.knowledge_tool_loop import run_knowledge_tool_loop
from agents.services.person_guide_prompts import (
    FACT_MAX_CHARS,
    MAX_FACTS,
    NOTES_MAX_CHARS,
    RESEARCH_NOW_PROMPT,
    build_research_system_prompt,
    build_research_user_prompt,
)
from agents.services.person_step_prompts import cap_text
from agents.types import (
    CancellationCheck,
    ChatMessage,
    LLMConfig,
    LogCallback,
    PersonGuideNotes,
    PersonGuideSources,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX: Final = "[Orchestrator]"
_JSON_BLOCK: Final = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
# A cited item's name in a note is cut to this.
_NAME_MAX_CHARS: Final = 80
# "… [KB #12 «Install guide.pdf» p.3]": how a note cites its item; the
# orchestrator reads the ids back with NOTE_CITATION.
NOTE_CITATION: Final = re.compile(r"\[KB #(\d+)\b")


class _UnusableNotesError(Exception):
    pass


def research_person_guide(
    llm: LLMConfig,
    *,
    task_description: str,
    os_name: str,
    knowledge_index: str,
    sources: PersonGuideSources,
    project_id: int,
    on_log: LogCallback | None,
    cancellation_check: CancellationCheck | None,
) -> PersonGuideNotes:
    """The case's research notes, redacted and capped.

    ``AgentCancelledError`` propagates. An ``AIServiceError`` or an unusable
    answer gives ``status="failed"`` (logged); the orchestrator retries it
    once, in a later execution (``person_guide_context.MAX_RESEARCH_ATTEMPTS``).
    No facts give ``status="empty"``.
    """
    _log(on_log, f"{_LOG_PREFIX} Guide research: looking up the knowledge base")
    messages = (
        ChatMessage(
            role="system",
            content=build_research_system_prompt(
                os_name=os_name,
                knowledge_index=knowledge_index,
                artifacts_profile=sources.artifacts_profile(),
            ),
        ),
        ChatMessage(role="user", content=build_research_user_prompt(task_description)),
    )
    try:
        raw = run_knowledge_tool_loop(
            llm,
            messages,
            project_id=project_id,
            max_rounds=settings.PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS,
            timeout_seconds=settings.PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS,
            final_prompt=RESEARCH_NOW_PROMPT,
            ended_note="asking for the notes.",
            log_prefix=_LOG_PREFIX,
            on_log=on_log,
            cancellation_check=cancellation_check,
        )
        facts = _parse_facts(raw)
    except (AIServiceError, _UnusableNotesError) as exc:
        _log(on_log, f"{_LOG_PREFIX} Guide research failed: {exc}")
        return PersonGuideNotes(text="", status="failed", researched_at=timezone.now())
    text = _render(facts, sources, task_description)
    if not text:
        _log(on_log, f"{_LOG_PREFIX} Guide research: nothing found")
        return PersonGuideNotes(text="", status="empty", researched_at=timezone.now())
    _log(
        on_log,
        f"{_LOG_PREFIX} Guide research: {text.count(chr(10)) + 1} fact(s) noted",
    )
    return PersonGuideNotes(text=text, status="ready", researched_at=timezone.now())


def _parse_facts(raw: str) -> list[tuple[str, int | None, int | None]]:
    """(fact, item id, page) per usable note; unusable JSON raises."""
    match = _JSON_BLOCK.search(raw)
    text = match.group(1) if match else raw
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise _UnusableNotesError("the answer holds no JSON object")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise _UnusableNotesError("the answer is not valid JSON") from exc
    notes = parsed.get("notes") if isinstance(parsed, dict) else None
    if not isinstance(notes, list):
        raise _UnusableNotesError('the answer has no "notes" list')
    facts: list[tuple[str, int | None, int | None]] = []
    for entry in notes:
        if not isinstance(entry, dict):
            continue
        fact = entry.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            continue
        facts.append(
            (
                " ".join(fact.split()),
                positive_int(entry.get("item_id")),
                positive_int(entry.get("page")),
            )
        )
        if len(facts) >= MAX_FACTS:
            break
    return facts


def _render(
    facts: list[tuple[str, int | None, int | None]],
    sources: PersonGuideSources,
    task_description: str,
) -> str:
    """Numbered lines "fact [KB #id «name» p.N]"; a fact citing an unknown id
    loses the citation. Redacted, each fact cut, the whole capped."""
    if not facts:
        return ""
    cited = tuple(sorted({item_id for _, item_id, _ in facts if item_id is not None}))
    names = sources.resolve_items(cited) if cited else {}
    lines: list[str] = []
    used = 0
    for fact, item_id, page in facts:
        body = cap_text(
            redact_secrets(fact, known_text=task_description), FACT_MAX_CHARS
        )
        name = None if item_id is None else names.get(item_id)
        citation = (
            ""
            if item_id is None or name is None
            else f" [KB #{item_id} «{cap_text(' '.join(name.split()), _NAME_MAX_CHARS)}»"
            + ("" if page is None else f" p.{page}")
            + "]"
        )
        line = f"{len(lines) + 1}. {body}{citation}"
        if used + len(line) + 1 > NOTES_MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _log(on_log: LogCallback | None, message: str) -> None:
    log_line(on_log, message, logger)
