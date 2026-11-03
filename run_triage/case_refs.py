"""The references the analysis chat uses for run cases: "T<test id>" or "R<id>".

A case with a TestRail test is named by that test's id (the id its report
link shows); any other case by its run case id. ``R<id>`` is also accepted
for a case that has a test id.
"""

from __future__ import annotations

from django.db.models import Q

from projects.models import TestRunTestCase
from run_triage.exceptions import RunTriageError

# The fields ``pivot_test_id`` reads.
TEST_ID_FIELDS: tuple[str, ...] = (
    "testrail_source_result_id",
    "testrail_test_id",
    "testrail_source_test_id",
)


def case_ref(pivot_id: int, testrail_test_id: int | None) -> str:
    return f"T{testrail_test_id}" if testrail_test_id else f"R{pivot_id}"


def pivot_test_id(
    source_result_id: int | None,
    test_id: int | None,
    source_test_id: int | None,
) -> int | None:
    """The TestRail test a case's link points to, with the same preference as
    ``projects.testrail_results_services.build_testrail_result_url``."""
    if source_result_id is not None:
        return source_test_id or test_id
    return test_id or source_test_id


def pivot_ref(pivot: TestRunTestCase) -> str:
    """``case_ref`` of a loaded case (``TEST_ID_FIELDS`` loaded)."""
    return case_ref(
        pivot.id,
        pivot_test_id(
            pivot.testrail_source_result_id,
            pivot.testrail_test_id,
            pivot.testrail_source_test_id,
        ),
    )


def parse_ref(ref: str) -> tuple[str, int] | None:
    """("T"|"R", number) of a well-formed reference, else None."""
    text = ref.strip().upper()
    digits = text[1:]
    if (
        len(text) < 2
        or text[0] not in ("T", "R")
        or not digits.isascii()
        or not digits.isdigit()
    ):
        return None
    number = int(text[1:])
    return (text[0], number) if number > 0 else None


def run_ref_map(test_run_id: int) -> dict[str, int]:
    """Every accepted reference of the run's cases to its case id (one query)."""
    refs: dict[str, int] = {}
    rows = TestRunTestCase.objects.filter(test_run_id=test_run_id).values_list(
        "id", *TEST_ID_FIELDS
    )
    for pivot_id, source_result_id, test_id, source_test_id in rows:
        refs[f"R{pivot_id}"] = pivot_id
        linked = pivot_test_id(source_result_id, test_id, source_test_id)
        if linked:
            refs[f"T{linked}"] = pivot_id
    return refs


def normalize_ref(ref: str) -> str:
    """The canonical spelling of ``ref`` ("t12" -> "T12"); "" when malformed."""
    parsed = parse_ref(ref)
    return f"{parsed[0]}{parsed[1]}" if parsed is not None else ""


def resolve_case_ref(test_run_id: int, ref: str) -> int:
    """The case id ``ref`` names; ``RunTriageError`` unless it is a case of the run."""
    parsed = parse_ref(ref)
    if parsed is None:
        raise RunTriageError(
            f"{ref!r} is not a case reference: use T<TestRail test id> or "
            "R<run case id>."
        )
    kind, number = parsed
    if kind == "R":
        if TestRunTestCase.objects.filter(test_run_id=test_run_id, id=number).exists():
            return number
        raise RunTriageError(f"R{number} is not a case of this run.")
    matches = TestRunTestCase.objects.filter(
        Q(testrail_test_id=number) | Q(testrail_source_test_id=number),
        test_run_id=test_run_id,
    ).values_list("id", *TEST_ID_FIELDS)
    for pivot_id, source_result_id, test_id, source_test_id in matches:
        if pivot_test_id(source_result_id, test_id, source_test_id) == number:
            return int(pivot_id)
    raise RunTriageError(f"T{number} is not a case of this run.")
