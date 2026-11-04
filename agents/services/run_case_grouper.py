"""Group one triage category's cases by root cause, with a category note.

One tool-free LLM call; the answer is strict JSON. Code drops unknown refs,
keeps only the first occurrence of a duplicated ref and drops empty groups.
Refs the answer left out are not added here: the caller puts them under
"Other findings". ``AIServiceError`` when the answer is not a usable JSON
object. This module knows nothing of the ORM.
"""

from __future__ import annotations

from typing import Final

from agents.services.llm_client import send_chat_completion
from agents.services.run_case_classifier import (
    bad_answer,
    one_line,
    parse_json_object,
)
from agents.services.run_case_grouper_prompts import (
    GROUPER_SYSTEM_PROMPT,
    build_grouper_user_prompt,
)
from agents.triage_types import (
    CategoryGrouping,
    GroupItem,
    GroupResult,
    SubGroupResult,
)
from agents.types import ChatMessage, LLMConfig

GROUP_TITLE_MAX_CHARS: Final = 300
NOTE_MAX_CHARS: Final = 2000


def group_category_cases(
    llm: LLMConfig,
    category: tuple[str, str, str],
    items: tuple[GroupItem, ...],
    *,
    application_context: str,
    timeout_seconds: float,
) -> CategoryGrouping:
    """The groups and note for ``category`` (value, label, meaning)."""
    value = category[0]
    if not items:
        return CategoryGrouping(category=value, note="", groups=())
    payload: list[dict[str, object]] = [
        {
            "ref": item.ref,
            "title": item.title,
            "severity": item.severity,
            "finding": item.finding,
        }
        for item in items
    ]
    messages = (
        ChatMessage(role="system", content=GROUPER_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=build_grouper_user_prompt(category, payload, application_context),
        ),
    )
    response = send_chat_completion(
        llm, messages, (), timeout_seconds=max(1.0, timeout_seconds)
    )
    content = response.message.content
    if not isinstance(content, str) or not content.strip():
        raise bad_answer("The AI service returned an empty grouping.")
    return _validate(value, items, parse_json_object(content))


def _validate(
    category: str, items: tuple[GroupItem, ...], parsed: dict[str, object]
) -> CategoryGrouping:
    raw_groups = parsed.get("groups")
    if not isinstance(raw_groups, list):
        raise bad_answer("The AI service returned a grouping without groups.")
    raw_note = parsed.get("note")
    note = raw_note.strip()[:NOTE_MAX_CHARS] if isinstance(raw_note, str) else ""
    findings = {item.ref: item.finding for item in items}
    seen: set[str] = set()

    def take(raw_refs: object) -> tuple[str, ...]:
        # Known refs, each only at its first place in the whole answer.
        if not isinstance(raw_refs, list):
            return ()
        kept: list[str] = []
        for ref in raw_refs:
            if isinstance(ref, str) and ref in findings and ref not in seen:
                seen.add(ref)
                kept.append(ref)
        return tuple(kept)

    groups: list[GroupResult] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        refs = take(raw.get("refs"))
        sub_groups: list[SubGroupResult] = []
        raw_subs = raw.get("sub_groups")
        for raw_sub in raw_subs if isinstance(raw_subs, list) else ():
            if not isinstance(raw_sub, dict):
                continue
            sub_refs = take(raw_sub.get("refs"))
            if sub_refs:
                sub_title = _title(raw_sub.get("title")) or one_line(
                    findings[sub_refs[0]], GROUP_TITLE_MAX_CHARS
                )
                sub_groups.append(SubGroupResult(title=sub_title, refs=sub_refs))
        members = refs or tuple(ref for sub in sub_groups for ref in sub.refs)
        if not members:
            continue
        title = _title(raw.get("title")) or one_line(
            findings[members[0]], GROUP_TITLE_MAX_CHARS
        )
        groups.append(GroupResult(title=title, refs=refs, sub_groups=tuple(sub_groups)))
    return CategoryGrouping(category=category, note=note, groups=tuple(groups))


def _title(value: object) -> str:
    return one_line(value, GROUP_TITLE_MAX_CHARS) if isinstance(value, str) else ""
