"""Credit run cases to the person who last drove them (the leaderboard owner).

Leaf module: imports only models, so every service can call it. The hooks sit
next to ``record_case_activity`` in the "drive" actions: Start
(``services.start_test_run``), Resume (``run_resume_services._resume_pivots``),
Re-run (``run_rerun_services.rerun_test_run_cases``), redesign re-run
(``case_review_redesign_services.rerun_with_redesigned_plan``), help
Continue / Mark failed / Mark blocked (``case_help_services._resolve``) and a
person step reported by a known user (``person_step_services._after_action``).
Reviews (re-test marks, Undo, applied result changes, Checked, TestRail sends),
Night auto-fix and anonymous Test Runner answers never move the credit. A new
action that makes a case execute or settles it must call ``credit_cases`` too.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from accounts.models import CustomUser
from projects.models import HelpResolution, TestRunTestCase

# Help resolutions by a person that drove the case: they move the credit and
# earn "Help resolved" points. A re-test mark, a cancel and an auto-fix do not.
CREDITED_HELP_RESOLUTIONS: Final = frozenset(
    {
        HelpResolution.CONTINUED,
        HelpResolution.MARKED_FAILED,
        HelpResolution.SKIPPED,
    }
)


def credit_cases(*, user: CustomUser | None, pivot_ids: Sequence[int]) -> None:
    """Set ``credited_user`` on the cases (one query; none for no user or ids).

    Runs in the caller's transaction and lets errors propagate: the credit is
    part of the action. ``update`` leaves ``updated_at`` alone on purpose.
    """
    if user is None or not pivot_ids:
        return
    TestRunTestCase.objects.filter(pk__in=pivot_ids).update(credited_user=user)
