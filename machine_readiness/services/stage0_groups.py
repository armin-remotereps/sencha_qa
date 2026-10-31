"""Group a project's cases by the stage 0 their preconditions need.

Plan: docs/plan/feat-machine-setup-intake.md › Services › stage0_groups.py.
Cases share a group when their analysed preconditions cite the same set of
(not ignored) requirements; unanalysed cases form one group of their own.
Two queries, no LLM. Groups carry counts, and a few case ids only when asked
(the agent tool), so memory stays bounded however many cases there are.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from django.db.models import BigIntegerField, Count, Func

from machine_readiness.models import PreconditionAnalysis
from projects.models import TestCase

# Group keys besides the hash of a requirement set (stable across calls, so a
# selector such as propose_test_runs' stage0_group can name a group).
NONE_NEEDED_GROUP = "none"
UNANALYSED_GROUP = "unanalysed"
_HASH_CHARS = 12
# Most case ids a group carries (``list_stage0_groups(sample_ids=)``).
MAX_SAMPLE_CASE_IDS = 20


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
    # The lowest case ids of the group, at most ``sample_ids``; empty when
    # none were asked for.
    sample_case_ids: tuple[int, ...]


def group_key(requirement_keys: tuple[str, ...]) -> str:
    """The stable key of a sorted requirement-key set."""
    if not requirement_keys:
        return NONE_NEEDED_GROUP
    digest = hashlib.sha256(",".join(requirement_keys).encode()).hexdigest()
    return digest[:_HASH_CHARS]


def list_stage0_groups(project_id: int, *, sample_ids: int = 0) -> list[Stage0Group]:
    """The project's groups, largest first; unanalysed cases last.

    ``sample_ids`` (at most MAX_SAMPLE_CASE_IDS) case ids per group; 0 for
    counts only (the requirements page).
    """
    sample = max(min(sample_ids, MAX_SAMPLE_CASE_IDS), 0)
    case_rows = (
        TestCase.objects.filter(project_id=project_id)
        .values("preconditions_md5")
        .annotate(cases=Count("id"))
        .order_by()
    )
    if sample:
        case_rows = case_rows.annotate(
            first_ids=Func(
                ArrayAgg("id", ordering="id"),
                # (array_agg(id ORDER BY id))[1:N]; N is our own constant.
                template=f"(%(expressions)s)[1:{sample}]",
                output_field=ArrayField(BigIntegerField()),
            )
        )
    needs = _analysed_needs(project_id)
    counts: dict[str, int] = defaultdict(int)
    samples: dict[str, list[int]] = defaultdict(list)
    titles: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for row in case_rows:
        cited = needs.get(row["preconditions_md5"])
        if cited is None:
            key = UNANALYSED_GROUP
        else:
            keys = tuple(sorted(cited))
            key = group_key(keys)
            titles[key] = (keys, tuple(cited[item] for item in keys))
        counts[key] += row["cases"]
        if sample:
            samples[key].extend(row["first_ids"] or ())
    groups = [
        Stage0Group(
            key=key,
            requirement_keys=titles[key][0],
            requirement_titles=titles[key][1],
            analysed=True,
            case_count=count,
            sample_case_ids=tuple(sorted(samples[key])[:sample]),
        )
        for key, count in counts.items()
        if key != UNANALYSED_GROUP
    ]
    groups.sort(key=lambda group: (-group.case_count, group.key))
    if counts.get(UNANALYSED_GROUP):
        groups.append(
            Stage0Group(
                key=UNANALYSED_GROUP,
                requirement_keys=(),
                requirement_titles=(),
                analysed=False,
                case_count=counts[UNANALYSED_GROUP],
                sample_case_ids=tuple(sorted(samples[UNANALYSED_GROUP])[:sample]),
            )
        )
    return groups


def stage0_group_md5s(project_id: int) -> dict[str, frozenset[str]]:
    """Group key -> the ``preconditions_md5`` values of its cases (2 queries).

    For selecting a group's cases with ``preconditions_md5__in`` instead of
    loading their ids. Resolve once per proposal, not per run.
    """
    needs = _analysed_needs(project_id)
    md5s: dict[str, set[str]] = defaultdict(set)
    for md5 in (
        TestCase.objects.filter(project_id=project_id)
        .values_list("preconditions_md5", flat=True)
        .distinct()
        .order_by()
    ):
        cited = needs.get(md5)
        key = UNANALYSED_GROUP if cited is None else group_key(tuple(sorted(cited)))
        md5s[key].add(md5)
    return {key: frozenset(values) for key, values in md5s.items()}


def _analysed_needs(project_id: int) -> dict[str, dict[str, str]]:
    """md5 -> {requirement key: title} of every analysis (one query).

    A md5 present here is analysed; ignored requirements are left out.
    """
    needs: dict[str, dict[str, str]] = {}
    for md5, key, title, ignored in (
        PreconditionAnalysis.objects.filter(project_id=project_id)
        .values_list(
            "preconditions_md5",
            "requirements__key",
            "requirements__title",
            "requirements__ignored",
        )
        .order_by()
    ):
        analysis_needs = needs.setdefault(md5, {})
        if key is not None and not ignored:
            analysis_needs[key] = title
    return needs
