"""Saved Failures page filter presets (per user)."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from accounts.models import CustomUser
from dashboard.exceptions import PresetLimitError, PresetNotFoundError
from dashboard.models import PRESET_LIMIT, FailedCaseFilterPreset


@dataclass(frozen=True)
class PresetItem:
    """A preset chip: its name and the query string it links to."""

    id: int
    name: str
    query: str


def list_presets(user: CustomUser) -> list[PresetItem]:
    """The user's presets by name (1 query)."""
    return [
        PresetItem(id=preset_id, name=name, query=query)
        for preset_id, name, query in FailedCaseFilterPreset.objects.filter(user=user)
        .order_by("name", "id")
        .values_list("id", "name", "query")
    ]


def save_preset(user: CustomUser, name: str, query: str) -> PresetItem:
    """Save ``query`` (an already re-encoded, cleaned filter query string)
    under ``name``, replacing the user's preset of that name.

    Raises ``PresetLimitError`` when a new name would exceed ``PRESET_LIMIT``.
    """
    with transaction.atomic():
        presets = FailedCaseFilterPreset.objects.filter(user=user)
        if not presets.filter(name=name).exists() and presets.count() >= PRESET_LIMIT:
            raise PresetLimitError(PRESET_LIMIT)
        preset, _ = FailedCaseFilterPreset.objects.update_or_create(
            user=user, name=name, defaults={"query": query}
        )
    return PresetItem(id=preset.pk, name=preset.name, query=preset.query)


def delete_preset(user: CustomUser, preset_id: int) -> None:
    """Delete the user's preset; raises ``PresetNotFoundError`` when there is
    no such preset of this user."""
    deleted, _ = FailedCaseFilterPreset.objects.filter(user=user, id=preset_id).delete()
    if not deleted:
        raise PresetNotFoundError()
