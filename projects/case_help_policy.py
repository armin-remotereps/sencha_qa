"""Whether a Need help case may be handed to the Night-mode auto-fix."""

from __future__ import annotations


def auto_fix_eligible(pivot_id: int, step_num: int) -> bool:
    """False when this step already got a fixed auto-fix since the case last
    started from scratch (Start, or a re-run in place); a repeat goes to a person.
    """
    raise NotImplementedError
