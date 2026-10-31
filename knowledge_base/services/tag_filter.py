"""The one definition of "filter knowledge items by tag"."""

from __future__ import annotations

from django.db.models import Q

from knowledge_base.tags import normalise_folder_query, normalise_tag


def item_tag_q(tag: str, *, prefix: str = "") -> Q:
    """Items carrying manual tag ``tag``, or in folder ``tag`` or a subfolder.

    The folder is compared case-insensitively. ``prefix`` is ``"item__"`` for
    chunk querysets. An empty tag matches nothing.
    """
    manual = normalise_tag(tag)
    folder = normalise_folder_query(tag)
    condition = Q(pk__in=[])
    if manual:
        condition |= Q(**{f"{prefix}tags__contains": [manual]})
    if folder:
        condition |= Q(**{f"{prefix}folder__iexact": folder}) | Q(
            **{f"{prefix}folder__istartswith": f"{folder}/"}
        )
    return condition
