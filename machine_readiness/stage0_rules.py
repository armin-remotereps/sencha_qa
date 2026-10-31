"""Validation of the ``record_stage0`` tool payload (pure: no database access).

The analysis prompt (step 5) gives each precondition text a ref ("T1", ...)
and the current requirement catalog; the model answers with one call:

    {
      "requirements": [
        {"key", "title", "kind", "check_target"?, "install_hint"?,
         "conflicts_with"?: [key, ...]}
      ],
      "texts": [
        {"ref", "requirement_keys": [key, ...], "none_needed"?: bool,
         "unclear"?: str}
      ]
    }

``parse_stage0_payload`` never raises on model output: it keeps every valid
entry and lists what it dropped in ``Stage0BatchResult.problems``. Keys are
normalised (``keys.normalise_key``) before they are checked.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from machine_readiness.keys import key_error, normalise_key
from machine_readiness.models import (
    ANALYSIS_UNCLEAR_MAX_LENGTH,
    INSTALL_HINT_MAX_LENGTH,
    PROJECT_MAX_REQUIREMENTS,
    REQUIREMENT_CHECK_TARGET_MAX_LENGTH,
    REQUIREMENT_MAX_CONFLICTS,
    REQUIREMENT_TITLE_MAX_LENGTH,
    RequirementKind,
)
from machine_readiness.types import (
    Stage0BatchResult,
    Stage0RequirementData,
    Stage0TextResult,
)

# Requirements one text may cite; more means the model listed steps, not S0.
TEXT_MAX_REQUIREMENTS = 30
# Cap on the problems kept, so a wildly wrong answer stays a short log line.
MAX_PROBLEMS = 20


def parse_stage0_payload(
    payload: object,
    *,
    text_refs: Collection[str],
    catalog_keys: Collection[str],
    project_requirement_count: int,
    max_requirements: int = PROJECT_MAX_REQUIREMENTS,
) -> Stage0BatchResult:
    """Validate one ``record_stage0`` call.

    ``text_refs``: the refs of this batch's texts; others are dropped.
    ``catalog_keys``: keys the project already has; a text may cite them
    without redefining them, and redefining one updates it (no new row).
    ``project_requirement_count``: rows the project has now; new keys beyond
    ``max_requirements`` in total are dropped. A text citing a key that is
    neither in the catalog nor a valid new requirement loses that key and is
    marked ``unclear``.
    """
    problems: list[str] = []
    if not isinstance(payload, Mapping):
        return Stage0BatchResult(problems=("The answer was not a JSON object.",))
    known = set(catalog_keys)
    requirements = _requirements(
        payload.get("requirements"),
        known=known,
        room=max(max_requirements - project_requirement_count, 0),
        problems=problems,
    )
    defined = known | {requirement.key for requirement in requirements}
    requirements = tuple(
        _without_unknown_conflicts(requirement, defined) for requirement in requirements
    )
    texts = _texts(
        payload.get("texts"),
        refs=set(text_refs),
        defined=defined,
        problems=problems,
    )
    return Stage0BatchResult(
        requirements=requirements, texts=texts, problems=tuple(problems[:MAX_PROBLEMS])
    )


def requirement_errors(requirement: Stage0RequirementData) -> list[str]:
    """Problems of one requirement (also used for a person's edit)."""
    errors: list[str] = []
    problem = key_error(requirement.key)
    if problem:
        errors.append(problem)
    if not requirement.title.strip():
        errors.append(f"«{requirement.key}»: a title is required.")
    for label, value, limit in (
        ("title", requirement.title, REQUIREMENT_TITLE_MAX_LENGTH),
        ("check target", requirement.check_target, REQUIREMENT_CHECK_TARGET_MAX_LENGTH),
        ("install hint", requirement.install_hint, INSTALL_HINT_MAX_LENGTH),
    ):
        if len(value) > limit:
            errors.append(f"«{requirement.key}»: the {label} has more than {limit} characters.")
    for label, value in (
        ("title", requirement.title),
        ("check target", requirement.check_target),
    ):
        if any(char in value for char in ("\x00", "\n", "\r")):
            errors.append(f"«{requirement.key}»: the {label} must be on one line.")
    if "\x00" in requirement.install_hint:
        errors.append(f"«{requirement.key}»: the install hint contains a NUL character.")
    if requirement.kind not in RequirementKind.values:
        errors.append(f"«{requirement.key}»: unknown kind «{requirement.kind}».")
    if len(requirement.conflicts_with) > REQUIREMENT_MAX_CONFLICTS:
        errors.append(
            f"«{requirement.key}»: at most {REQUIREMENT_MAX_CONFLICTS} conflicts."
        )
    for other in requirement.conflicts_with:
        problem = key_error(other)
        if problem:
            errors.append(f"«{requirement.key}» conflicts: {problem}")
        elif other == requirement.key:
            errors.append(f"«{requirement.key}» cannot conflict with itself.")
    return errors


def _requirements(
    raw: object, *, known: set[str], room: int, problems: list[str]
) -> tuple[Stage0RequirementData, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("«requirements» was not a list.")
        return ()
    accepted: dict[str, Stage0RequirementData] = {}
    new_count = 0
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            problems.append(f"Requirement {index} was not an object.")
            continue
        requirement = Stage0RequirementData(
            key=normalise_key(_text(entry.get("key"))),
            title=_clip(_one_line(_text(entry.get("title"))), REQUIREMENT_TITLE_MAX_LENGTH),
            kind=_text(entry.get("kind")).strip().lower() or RequirementKind.OTHER,
            check_target=_text(entry.get("check_target")).strip(),
            install_hint=_clip(
                _text(entry.get("install_hint")).strip(), INSTALL_HINT_MAX_LENGTH
            ),
            conflicts_with=tuple(
                dict.fromkeys(
                    normalise_key(_text(other))
                    for other in _list(entry.get("conflicts_with"))
                    if _text(other)
                )
            ),
        )
        errors = requirement_errors(requirement)
        if errors:
            problems.append(f"Requirement {index} dropped: {errors[0]}")
            continue
        if requirement.key in accepted:
            problems.append(f"Requirement «{requirement.key}» was given twice.")
            continue
        if requirement.key not in known:
            if new_count >= room:
                problems.append(
                    f"Requirement «{requirement.key}» dropped: the project already "
                    "has the most requirements allowed."
                )
                continue
            new_count += 1
        accepted[requirement.key] = requirement
    return tuple(accepted.values())


def _without_unknown_conflicts(
    requirement: Stage0RequirementData, defined: set[str]
) -> Stage0RequirementData:
    kept = tuple(key for key in requirement.conflicts_with if key in defined)
    if kept == requirement.conflicts_with:
        return requirement
    return Stage0RequirementData(
        key=requirement.key,
        title=requirement.title,
        kind=requirement.kind,
        check_target=requirement.check_target,
        install_hint=requirement.install_hint,
        conflicts_with=kept,
    )


def _texts(
    raw: object, *, refs: set[str], defined: set[str], problems: list[str]
) -> tuple[Stage0TextResult, ...]:
    if not isinstance(raw, list):
        problems.append("«texts» was not a list.")
        return ()
    results: dict[str, Stage0TextResult] = {}
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            problems.append(f"Text {index} was not an object.")
            continue
        ref = _text(entry.get("ref")).strip()
        if ref not in refs:
            problems.append(f"Text {index}: unknown ref «{ref[:20]}».")
            continue
        if ref in results:
            problems.append(f"Text {ref} was answered twice; the first answer is kept.")
            continue
        cited = list(
            dict.fromkeys(
                normalise_key(_text(key))
                for key in _list(entry.get("requirement_keys"))
                if _text(key)
            )
        )
        unknown = [key for key in cited if key not in defined]
        keys = tuple(key for key in cited if key in defined)[:TEXT_MAX_REQUIREMENTS]
        unclear = _text(entry.get("unclear")).strip()
        if unknown:
            note = "Unknown requirement keys: " + ", ".join(unknown[:5])
            unclear = f"{unclear} {note}".strip() if unclear else note
        none_needed = entry.get("none_needed") is True and not keys
        results[ref] = Stage0TextResult(
            ref=ref,
            requirement_keys=keys,
            none_needed=none_needed,
            unclear=_one_line(unclear)[:ANALYSIS_UNCLEAR_MAX_LENGTH],
        )
    return tuple(results.values())


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _clip(text: str, limit: int) -> str:
    """Model prose (titles, hints) is cut rather than dropped; paths never are."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    return " ".join(text.replace("\x00", "").split())
