"""Shapes and limits of test runs the main agent proposes (no ORM)."""

from __future__ import annotations

from dataclasses import dataclass

MAX_PROPOSED_RUNS = 50
MAX_CASES_PER_PROPOSED_RUN = 1000
MAX_IDS_PER_LIST = 1000
RUN_NAME_MAX = 255
PROPOSAL_NOTE_MAX = 1000
SAMPLE_TITLES = 5

READ_DOCUMENT_TOOL = "read_document"
LIST_TEST_RUNS_TOOL = "list_test_runs"
GET_TEST_RUN_STATE_TOOL = "get_test_run_state"
LIST_CASE_GROUPS_TOOL = "list_test_case_groups"
PROPOSE_TEST_RUNS_TOOL = "propose_test_runs"


@dataclass(frozen=True)
class RunSpec:
    """One proposed run: a name and the criteria that pick its cases.

    The filters (``title_contains``, ``testrail_suite_id``, ``testrail_run_id``,
    ``priorities``, ``types``) are ANDed; ``all_cases`` selects every case. The
    explicit ``case_ids`` and ``testrail_case_ids`` are unioned with that set,
    and ``exclude_case_ids`` is removed last. At least one selector is set.
    """

    name: str
    all_cases: bool = False
    case_ids: tuple[int, ...] = ()
    # "C123" or "123".
    testrail_case_ids: tuple[str, ...] = ()
    title_contains: str = ""
    testrail_suite_id: int | None = None
    # ``TestCase.testrail_source_run_id``.
    testrail_run_id: int | None = None
    priorities: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    exclude_case_ids: tuple[int, ...] = ()

    def has_filter(self) -> bool:
        return bool(
            self.title_contains
            or self.testrail_suite_id is not None
            or self.testrail_run_id is not None
            or self.priorities
            or self.types
        )

    def has_selector(self) -> bool:
        return (
            self.all_cases
            or self.has_filter()
            or bool(self.case_ids)
            or bool(self.testrail_case_ids)
        )


@dataclass(frozen=True)
class ResolvedSelection:
    """A run's cases, in picker order, and the first few titles."""

    case_ids: list[int]
    sample_titles: list[str]
