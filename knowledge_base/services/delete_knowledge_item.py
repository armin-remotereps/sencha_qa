"""Delete a knowledge item, its members and chunks, and blobs no one uses."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.models import KnowledgeBlob, KnowledgeItem
from knowledge_base.services.blob_storage import BLOB_GRACE, delete_blob_files
from knowledge_base.services.item_processing import ITEM_NOT_FOUND_MESSAGE


def delete_knowledge_item(*, project_id: int, item_id: int) -> int | None:
    """Delete the item (members and chunks cascade); return its parent id.

    Blob rows no other item references are deleted with the item, and their
    files while those rows are locked. Blobs shared with other items (a duplicated project,
    the same file uploaded twice) stay, and so do blobs stored within the
    last hour (left to the sweep).
    """
    with transaction.atomic():
        found = (
            KnowledgeItem.objects.select_for_update()
            .filter(pk=item_id, project_id=project_id)
            .values_list("parent_id", flat=True)
        )
        rows = list(found)
        if not rows:
            raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
        parent_id = rows[0]
        blob_ids = set(
            KnowledgeItem.objects.filter(Q(pk=item_id) | Q(parent_id=item_id))
            .values_list("blob_id", flat=True)
            .distinct()
        )
        KnowledgeItem.objects.filter(pk=item_id).delete()
        # Locked so a concurrent upload cannot attach a new item to a blob
        # that is being removed (its insert waits, then fails on the FK).
        # Blobs stored within the grace may be about to be referenced by an
        # upload in progress (stored before its items exist): the sweep
        # removes them later if they stay unused.
        locked = dict(
            KnowledgeBlob.objects.select_for_update()
            .filter(pk__in=blob_ids, stored_at__lt=timezone.now() - BLOB_GRACE)
            .values_list("pk", "storage_name")
        )
        still_used = set(
            KnowledgeItem.objects.filter(blob_id__in=locked)
            .values_list("blob_id", flat=True)
            .distinct()
        )
        unused = {pk: name for pk, name in locked.items() if pk not in still_used}
        if unused:
            KnowledgeBlob.objects.filter(pk__in=unused).delete()
            # Unlinked while the rows are locked, not after commit: an upload
            # of the same bytes waits on the lock, then finds no file and
            # writes it again (store_blob rewrites a missing file).
            delete_blob_files(list(unused.values()))
    return parent_id
