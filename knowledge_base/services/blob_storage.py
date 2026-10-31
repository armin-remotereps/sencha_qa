"""The only code that touches the knowledge base file storage.

Files are content addressed (``ab/abcdef…`` by sha256). A file is written to a
temp name under ``tmp/`` first and renamed into place, so a reader never sees
a half-written blob; abandoned temp files are removed by the daily sweep.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from django.core.files import File
from django.db import IntegrityError, transaction

from knowledge_base.models import KnowledgeBlob
from knowledge_base.storage import knowledge_storage

logger = logging.getLogger(__name__)

_TEMP_DIR = "tmp"
# get_or_create races with a concurrent insert of the same hash at most once.
_CREATE_ATTEMPTS = 2


@dataclass(frozen=True)
class StoredFile:
    """A file found in the storage: its storage name and last change."""

    name: str
    modified_at: datetime


def _storage_name(sha256: str) -> str:
    return f"{sha256[:2]}/{sha256}"


def _write_temp(chunks: Iterable[bytes], root: Path) -> tuple[Path, str, int]:
    """Stream ``chunks`` to a new temp file; return its path, hash and size."""
    temp_dir = root / _TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"upload-{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("xb") as handle:
            for chunk in chunks:
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path, digest.hexdigest(), size


def _place_file(temp_path: Path, final_path: Path) -> None:
    """Move the temp file into place, or drop it when the blob file exists."""
    if final_path.is_file():
        temp_path.unlink(missing_ok=True)
        # Refresh the mtime so the sweep (1 h grace) never takes a file that
        # an uncommitted upload is about to reference again.
        os.utime(final_path)
        return
    final_path.parent.mkdir(parents=True, exist_ok=True)
    # Same file system (both under the storage root): an atomic rename.
    os.replace(temp_path, final_path)


def _get_or_create_row(sha256: str, size: int) -> KnowledgeBlob:
    """Get or create the row, locked (must run inside a transaction).

    The lock keeps a concurrent delete from dropping the row (and its file)
    between here and the caller's item insert. ``get_or_create`` inserts in a
    savepoint, so a lost insert race leaves the transaction usable to retry.
    """
    attempt = 1
    while True:
        try:
            blob, _created = KnowledgeBlob.objects.select_for_update().get_or_create(
                sha256=sha256,
                defaults={"size_bytes": size, "storage_name": _storage_name(sha256)},
            )
            return blob
        except IntegrityError:
            # A concurrent insert of the same hash won: read its row.
            if attempt >= _CREATE_ATTEMPTS:
                raise
            attempt += 1


def store_blob(chunks: Iterable[bytes]) -> KnowledgeBlob:
    """Store the streamed bytes once by content hash and return the blob row.

    An existing row whose file went missing gets its file rewritten.
    """
    storage = knowledge_storage()
    root = Path(storage.location)
    temp_path, sha256, size = _write_temp(chunks, root)
    try:
        # No savepoint when nested: the caller's transaction holds the lock.
        with transaction.atomic(savepoint=False):
            blob = _get_or_create_row(sha256, size)
            _place_file(temp_path, Path(storage.path(blob.storage_name)))
    finally:
        temp_path.unlink(missing_ok=True)
    return blob


def store_blob_bytes(data: bytes) -> KnowledgeBlob:
    return store_blob((data,))


def open_blob(blob: KnowledgeBlob) -> File[bytes]:
    """Open the blob's file for reading (raises FileNotFoundError if missing)."""
    return knowledge_storage().open(blob.storage_name, "rb")


def read_blob_bytes(blob: KnowledgeBlob) -> bytes:
    with open_blob(blob) as handle:
        data: bytes = handle.read()
    return data


def delete_blob_files(storage_names: Sequence[str]) -> None:
    """Delete stored files; a file that cannot be removed is left to the sweep."""
    storage = knowledge_storage()
    for name in storage_names:
        try:
            storage.delete(name)
        except OSError:
            logger.warning("Could not delete knowledge base file %s", name)


def iter_stored_files() -> Iterator[StoredFile]:
    """Every file in the storage (blob files and temp files), with its mtime."""
    root = Path(knowledge_storage().location)
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                modified_at = datetime.fromtimestamp(
                    entry.stat(follow_symlinks=False).st_mtime, tz=timezone.utc
                )
                yield StoredFile(
                    name=f"{directory.name}/{entry.name}", modified_at=modified_at
                )
