"""Group a project's cases by the stage 0 their preconditions need.

Plan: docs/plan/feat-machine-setup-intake.md › Services › stage0_groups.py.
Cases share a group when their analysed preconditions cite the same set of
(not ignored) requirements; unanalysed cases form one group of their own.
Two queries, no LLM.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from django.contrib.postgres.aggregates import ArrayAgg

from machine_readiness.models import PreconditionAnalysis
from projects.models import TestCase

# Group keys besides the hash of a requirement set (stable across calls, so a
# selector such as propose_test_runs' stage0_group can name a group).
NONE_NEEDED_GROUP = "none"
UNANALYSED_GROUP = "unanalysed"
_HASH_CHARS = 12


@dataclass(frozen=True)
class Stage0Group:
    """Cases that need the same stage 0."""

    # NONE_NEEDED_GROUP, UNANALYSED_GROUP, or a short hash of the sorted keys.
    key: str
    # Sorted requirement keys, and their titles in the same order.
    requirement_keys: tuple[str, ...]
    requirement_titles: tuple[str, ...]
    analysed: bool
    case_count: int
    # Ordered by id.
    case_ids: tuple[int, ...]


def group_key(requirement_keys: tuple[str, ...]) -> str:
    """The stable key of a sorted requirement-key set."""
    if not requirement_keys:
        return NONE_NEEDED_GROUP
    digest = hashlib.sha256(",".join(requirement_keys).encode()).hexdigest()
    return digest[:_HASH_CHARS]


def list_stage0_groups(project_id: int) -> list[Stage0Group]:
    """The project's groups, largest first; unanalysed cases last."""
    case_rows = (
        TestCase.objects.filter(project_id=project_id)
        .values("preconditions_md5")
        .annotate(case_ids=ArrayAgg("id", ordering="id"))
        .order_by()
    )
    analysis_rows = (
        PreconditionAnalysis.objects.filter(project_id=project_id)
        .values_list(
            "preconditions_md5",
            "requirements__key",
            "requirements__title",
            "requirements__ignored",
        )
        .order_by()
    )
    # md5 -> {key: title}; a md5 present here is analysed.
    needs: dict[str, dict[str, str]] = {}
    for md5, key, title, ignored in analysis_rows:
        analysis_needs = needs.setdefault(md5, {})
        if key is not None and not ignored:
            analysis_needs[key] = title
    grouped: dict[str, list[int]] = defaultdict(list)
    titles: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    unanalysed: list[int] = []
    for row in case_rows:
        cited = needs.get(row["preconditions_md5"])
        if cited is None:
            unanalysed.extend(row["case_ids"])
            continue
        keys = tuple(sorted(cited))
        key = group_key(keys)
        grouped[key].extend(row["case_ids"])
        titles[key] = (keys, tuple(cited[item] for item in keys))
    groups = [
        Stage0Group(
            key=key,
            requirement_keys=titles[key][0],
            requirement_titles=titles[key][1],
            analysed=True,
            case_count=len(case_ids),
            case_ids=tuple(sorted(case_ids)),
        )
        for key, case_ids in grouped.items()
    ]
    groups.sort(key=lambda group: (-group.case_count, group.key))
    if unanalysed:
        groups.append(
            Stage0Group(
                key=UNANALYSED_GROUP,
                requirement_keys=(),
                requirement_titles=(),
                analysed=False,
                case_count=len(unanalysed),
                case_ids=tuple(sorted(unanalysed)),
            )
        )
    return groups
