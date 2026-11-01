"""A "How to do it" item as a page shows it, with its source link.

Leaf module (``agents.types`` and URL reversing only): the person step card
and the Manual run case card both render these.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from agents.types import PersonHowToItem


@dataclass(frozen=True)
class PersonHowToView:
    """One "How to do it" item as the case card shows it."""

    text: str
    # An AI-suggested command for the runner's shell; "" when none.
    command: str
    # Server-built ("«Install guide.pdf», page 3" / "Artifacts profile");
    # "" for general guidance.
    source_label: str
    # The knowledge base item (at its page) or the Machine readiness tab;
    # "" without a source.
    source_url: str


def how_to_view(item: PersonHowToItem, project_id: int) -> PersonHowToView:
    """``item`` with its source link (no query: ids come from the item)."""
    url = ""
    if item.source_kind == "kb" and item.source_item_id is not None:
        url = reverse(
            "knowledge_base:item",
            kwargs={"project_id": project_id, "item_id": item.source_item_id},
        )
        if item.source_page is not None:
            url = f"{url}?page={item.source_page}"
    elif item.source_kind == "profile":
        url = reverse("machine_readiness:overview", kwargs={"project_id": project_id})
    return PersonHowToView(
        text=item.text,
        command=item.command,
        source_label=item.source_label if url else "",
        source_url=url,
    )
