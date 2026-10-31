"""Delete a knowledge item, its members and chunks, and blobs no one uses."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.models import KnowledgeBlob, KnowledgeItem
from knowledge_base.services.blob_storage import delete_blob_files
from knowledge_base.services.item_processing import ITEM_NOT_FOUND_MESSAGE


def delete_knowledge_item(*, project_id: int, item_id: int) -> int | None:
    """Delete the item (members and chunks cascade); return its parent id.

    Blob rows no other item references are deleted with the item, and their
    files after commit. Blobs shared with other items (a duplicated project,
    the same file uploaded twice) stay.
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
        locked = dict(
            KnowledgeBlob.objects.select_for_update()
            .filter(pk__in=blob_ids)
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
            names = list(unused.values())
            transaction.on_commit(lambda: delete_blob_files(names))
    return parent_id
