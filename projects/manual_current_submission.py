"""The current hand-in of Manual run cases (read-only queries).

A case's current hand-in is its newest ``ManualCaseSubmission`` that Re-run
or Reset did not supersede. Shared by the TestRail rewrite, the evidence
freeze and the check gate; one query per call whatever the number of cases.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from django.db.models import QuerySet

from projects.models import ManualCaseSubmission, ManualUploadState

# What the comment and the rewrite read from a hand-in.
_SUBMISSION_FIELDS: Final = (
    "id",
    "test_run_test_case_id",
    "submitted_label",
    "verdict_note",
    "shots_expected",
    "shots_received",
    "upload_state",
    "refine_state",
    "refined_summary",
    "refined_step_notes",
    "refined_input_hash",
)


def _current(ids: list[int]) -> QuerySet[ManualCaseSubmission]:
    return (
        ManualCaseSubmission.objects.filter(test_run_test_case_id__in=ids)
        .exclude(upload_state=ManualUploadState.SUPERSEDED)
        .order_by("test_run_test_case_id", "-submitted_at", "-id")
        .distinct("test_run_test_case_id")
    )


def current_manual_submissions(
    pivot_ids: Iterable[int],
) -> dict[int, ManualCaseSubmission]:
    """Each case's current hand-in, by case id (1 query; none for no ids).

    Loads the upload, label, note and rewrite fields only.
    """
    ids = list(pivot_ids)
    if not ids:
        return {}
    rows = _current(ids).only(*_SUBMISSION_FIELDS)
    return {row.test_run_test_case_id: row for row in rows}


def current_manual_shot_keys(pivot_ids: Iterable[int]) -> dict[int, frozenset[str]]:
    """The screenshot keys each case's current hand-in announced, by case id."""
    ids = list(pivot_ids)
    if not ids:
        return {}
    keys: dict[int, frozenset[str]] = {}
    for case_id, manifest in _current(ids).values_list(
        "test_run_test_case_id", "shot_manifest"
    ):
        entries = manifest if isinstance(manifest, list) else []
        keys[case_id] = frozenset(
            entry["key"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)
        )
    return keys


def uploading_manual_case_ids(pivot_ids: Iterable[int]) -> set[int]:
    """The cases whose current hand-in is still uploading screenshots."""
    ids = list(pivot_ids)
    if not ids:
        return set()
    return {
        case_id
        for case_id, state in _current(ids).values_list(
            "test_run_test_case_id", "upload_state"
        )
        if state == ManualUploadState.UPLOADING
    }
