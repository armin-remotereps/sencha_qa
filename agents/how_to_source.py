"""Source labels of "How to do it" items (leaf module, no ``agents.services``).

``projects`` imports these without pulling in the agent services package.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from agents.json_values import positive_int
from agents.types import PersonHowToSourceKind

# A how-to item's server-built source label ("«Install guide.pdf», page 3").
SOURCE_LABEL_MAX_CHARS: Final = 200
PROFILE_SOURCE_LABEL: Final = "Artifacts profile"


StoredSource = tuple[PersonHowToSourceKind, int | None, int | None, str]


def parse_stored_source(entry: Mapping[str, object]) -> StoredSource | None:
    """A stored how-to item's source as (kind, item id, page, label); None
    when absent or malformed (the item is kept without one).

    Reads the keys ``source_kind``, ``source_item_id``, ``source_page`` and
    ``source_label`` (checkpoint ``how_to`` items and ``CasePersonStep.how_to``
    rows). ``kb`` needs a positive item id and a label; its page is dropped
    unless a positive int. ``profile`` has no id or page and a blank label
    becomes ``PROFILE_SOURCE_LABEL``. The label is cut to
    ``SOURCE_LABEL_MAX_CHARS``.
    """
    kind, label = entry.get("source_kind"), entry.get("source_label", "")
    if not isinstance(label, str):
        return None
    text = _cut(label, SOURCE_LABEL_MAX_CHARS)
    if kind == "kb":
        item_id = positive_int(entry.get("source_item_id"))
        if item_id is None or not text:
            return None
        return "kb", item_id, positive_int(entry.get("source_page")), text
    if kind == "profile":
        return "profile", None, None, text or PROFILE_SOURCE_LABEL
    return None


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
