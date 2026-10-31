"""Remove knowledge base blobs and files that no item uses any more."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from knowledge_base.models import KnowledgeBlob, KnowledgeItem
from knowledge_base.services.blob_storage import (
    BLOB_GRACE,
    delete_blob_files,
    iter_stored_files,
)

_BLOB_BATCH = 500


def sweep_knowledge_storage() -> tuple[int, int]:
    """Delete unreferenced blob rows and row-less files older than the grace.

    Returns (blob rows removed, files removed).
    """
    cutoff = timezone.now() - BLOB_GRACE
    blobs_removed, files_removed = _sweep_blob_rows(cutoff)
    return blobs_removed, files_removed + _sweep_orphan_files(cutoff)


def _sweep_blob_rows(cutoff: datetime) -> tuple[int, int]:
    removed = 0
    files = 0
    last_pk = 0
    while True:
        with transaction.atomic():
            batch = dict(
                KnowledgeBlob.objects.select_for_update(skip_locked=True)
                .filter(pk__gt=last_pk, stored_at__lt=cutoff)
                .filter(~Exists(KnowledgeItem.objects.filter(blob_id=OuterRef("pk"))))
                .order_by("pk")
                .values_list("pk", "storage_name")[:_BLOB_BATCH]
            )
            if not batch:
                break
            last_pk = max(batch)
            KnowledgeBlob.objects.filter(pk__in=batch).delete()
            names = list(batch.values())
            # Under the row lock, as in delete_knowledge_item.
            delete_blob_files(names)
            removed += len(batch)
            files += len(names)
    return removed, files


def _sweep_orphan_files(cutoff: datetime) -> int:
    """Files (blob files and abandoned temp files) with no blob row."""
    old_by_dir: dict[str, list[str]] = defaultdict(list)
    for stored in iter_stored_files():
        if stored.modified_at < cutoff:
            old_by_dir[stored.name.split("/", 1)[0]].append(stored.name)
    removed = 0
    for names in old_by_dir.values():
        # One query per storage directory (at most 256 plus the temp dir).
        known = set(
            KnowledgeBlob.objects.filter(storage_name__in=names).values_list(
                "storage_name", flat=True
            )
        )
        orphans = [name for name in names if name not in known]
        # Re-checked right before the unlink: an upload reusing the file
        # refreshes its mtime before its new row commits.
        removed += delete_blob_files(orphans, modified_before=cutoff)
    return removed
