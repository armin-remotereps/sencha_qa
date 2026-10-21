"""TestRail suite order, computed from sections and cases. Pure: no I/O, no ORM.

TestRail order is the section tree walked depth-first (siblings by
``display_order``, then ID), then cases within a section by ``display_order``,
then by case ID.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from projects.testrail_client import TestRailCase, TestRailSection

# Sorts after every real display_order / section rank.
_LAST = float("inf")


def _section_sort_key(section: TestRailSection) -> tuple[int, int]:
    return (section.display_order, section.id)


def _rank_sections(sections: Sequence[TestRailSection]) -> dict[int, int]:
    """Section ID -> 0-based depth-first pre-order rank.

    A section whose parent is unknown (or itself) is treated as a root. Sections
    caught in a parent cycle are unreachable from any root; they are walked as
    extra roots afterwards, so every section gets a rank and the walk never loops.
    """
    by_id = {section.id: section for section in sections}
    children: dict[int, list[TestRailSection]] = {}
    roots: list[TestRailSection] = []
    for section in by_id.values():
        parent_id = section.parent_id
        if parent_id is None or parent_id == section.id or parent_id not in by_id:
            roots.append(section)
        else:
            children.setdefault(parent_id, []).append(section)
    for siblings in children.values():
        siblings.sort(key=_section_sort_key)

    ranks: dict[int, int] = {}

    def walk(start: TestRailSection) -> None:
        stack = [start]
        while stack:
            section = stack.pop()
            if section.id in ranks:
                continue
            ranks[section.id] = len(ranks)
            # Reversed so the first sibling is popped first.
            stack.extend(reversed(children.get(section.id, [])))

    for root in sorted(roots, key=_section_sort_key):
        walk(root)
    for section in sorted(by_id.values(), key=_section_sort_key):
        if section.id not in ranks:
            walk(section)
    return ranks


def rank_testrail_cases(
    sections: Sequence[TestRailSection], cases: Iterable[TestRailCase]
) -> dict[int, int]:
    """Case ID -> 0-based rank in TestRail suite order.

    Cases whose section is unknown sort after all known sections; a missing
    ``display_order`` sorts last within its section; ties break by case ID.
    """
    section_ranks = _rank_sections(sections)
    # Keep only the sort key, so a large suite's case bodies are not held twice.
    keys: list[tuple[float, float, int]] = []
    for case in cases:
        section_rank: float = _LAST
        if case.section_id is not None and case.section_id in section_ranks:
            section_rank = section_ranks[case.section_id]
        display_order: float = (
            _LAST if case.display_order is None else case.display_order
        )
        keys.append((section_rank, display_order, case.id))
    keys.sort()

    ranks: dict[int, int] = {}
    for _, _, case_id in keys:
        if case_id not in ranks:
            ranks[case_id] = len(ranks)
    return ranks


def append_unranked(
    ranks: Mapping[int, int], case_ids_in_order: Sequence[int]
) -> dict[int, int]:
    """``ranks`` restricted to ``case_ids_in_order``; IDs without a rank are
    appended after the highest rank, in the given order."""
    result: dict[int, int] = {}
    next_rank = max(ranks.values(), default=-1) + 1
    for case_id in case_ids_in_order:
        if case_id in result:
            continue
        rank = ranks.get(case_id)
        if rank is None:
            rank = next_rank
            next_rank += 1
        result[case_id] = rank
    return result
