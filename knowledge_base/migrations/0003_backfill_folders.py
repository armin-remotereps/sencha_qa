"""Fill ``folder`` and ``is_folder_instructions`` of existing archive members.

The rules are inlined (as of this migration) on purpose: migrations must not
import app code that may change later. Members' chunk vectors are rewritten
with the new weighting (B gains the folder words). Reverse is a no-op: 0002's
reversal drops the fields.
"""

from __future__ import annotations

import re

from django.apps.registry import Apps
from django.contrib.postgres.search import SearchVector
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Value

_SEARCH_CONFIG = "english"
_FOLDER_MAX = 1024
_PATH_WORD_SEPARATORS = re.compile(r"[/\\_.\-]+")
_STEM_RANK = {"instructions": 0, "readme": 1}
_EXTENSION_RANK = {".md": 0, ".markdown": 0, ".txt": 1, "": 2}


def _folder_of(original_name: str) -> str:
    folder = original_name.rpartition("/")[0]
    if original_name.startswith("…"):
        # Truncated label: its first segment is partial.
        folder = folder.partition("/")[2]
    if len(folder) > _FOLDER_MAX:
        kept: list[str] = []
        length = 0
        for segment in folder.split("/"):
            extra = len(segment) + (1 if kept else 0)
            if length + extra > _FOLDER_MAX:
                break
            kept.append(segment)
            length += extra
        folder = "/".join(kept)
    return folder


def _rank(file_name: str) -> tuple[int, int, int, str] | None:
    stem, dot, extension = file_name.casefold().partition(".")
    suffix = f"{dot}{extension}"
    if stem not in _STEM_RANK or suffix not in _EXTENSION_RANK:
        return None
    return (_STEM_RANK[stem], _EXTENSION_RANK[suffix], len(file_name), file_name)


def backfill(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    item_model = apps.get_model("knowledge_base", "KnowledgeItem")
    chunk_model = apps.get_model("knowledge_base", "KnowledgeChunk")
    members = list(
        item_model.objects.filter(parent__isnull=False).values_list(
            "id", "parent_id", "original_name", "name", "description", "tags"
        )
    )
    best: dict[tuple[int, str], tuple[tuple[int, int, int, str], int]] = {}
    folders: dict[int, str] = {}
    for item_id, parent_id, original_name, _name, _description, _tags in members:
        folder = _folder_of(original_name)
        folders[item_id] = folder
        rank = _rank(original_name.rpartition("/")[2])
        if rank is None:
            continue
        key = (parent_id, folder)
        current = best.get(key)
        if current is None or rank < current[0]:
            best[key] = (rank, item_id)
    instruction_ids = {item_id for _, item_id in best.values()}
    for item_id, _parent_id, _original, name, description, tags in members:
        folder = folders[item_id]
        if not folder and item_id not in instruction_ids:
            continue
        item_model.objects.filter(pk=item_id).update(
            folder=folder, is_folder_instructions=item_id in instruction_ids
        )
        words = " ".join(p for p in _PATH_WORD_SEPARATORS.split(folder) if p)
        label = " ".join(p for p in (words, *tags) if p)
        chunk_model.objects.filter(item_id=item_id).update(
            search_vector=SearchVector(
                Value(f"{name} {description}"), weight="A", config=_SEARCH_CONFIG
            )
            + SearchVector(Value(label), weight="B", config=_SEARCH_CONFIG)
            + SearchVector("heading", weight="B", config=_SEARCH_CONFIG)
            + SearchVector("text", weight="C", config=_SEARCH_CONFIG)
        )


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge_base", "0002_item_folder_tags"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
