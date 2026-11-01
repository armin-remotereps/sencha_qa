"""The fingerprint of what a Manual run case's step guide depends on.

Leaf and pure: the case text, the verbatim steps (text and expected result)
and the runner OS. A guide whose fingerprint no longer matches is not shown,
and a claim with a new fingerprint starts the guide afresh.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence


def guide_fingerprint(
    task_description: str, steps: Sequence[tuple[str, str]], os_name: str
) -> str:
    """sha256 (hex, 64 chars) of the case text, ``(text, expected)`` per
    step in order, and the runner OS name."""
    payload = json.dumps(
        [task_description, [[text, expected] for text, expected in steps], os_name],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
