"""Discard Manual run hand-ins whose case is reset or re-run.

A leaf (models only), so Reset (``services.reset_test_run``) and Re-run
(``run_rerun_services``) can both call it. A superseded hand-in refuses its
later screenshot uploads with "drop" and the runner deletes them.
"""

from __future__ import annotations

from collections.abc import Sequence

from projects.models import ManualCaseSubmission, ManualUploadState


def supersede_manual_submissions(
    *, test_run_id: int | None = None, pivot_ids: Sequence[int] = ()
) -> int:
    """Discard the hand-ins of a run's cases (or of ``pivot_ids``).

    Call it in the caller's transaction with the cases already locked (or
    updated), the order a screenshot upload takes (case, then hand-in).
    Stored screenshots are left to the caller. One UPDATE; the number of
    hand-ins discarded.
    """
    submissions = ManualCaseSubmission.objects.exclude(
        upload_state=ManualUploadState.SUPERSEDED
    )
    if test_run_id is not None:
        submissions = submissions.filter(test_run_test_case__test_run_id=test_run_id)
    elif pivot_ids:
        submissions = submissions.filter(test_run_test_case_id__in=list(pivot_ids))
    else:
        return 0
    return submissions.update(upload_state=ManualUploadState.SUPERSEDED)
