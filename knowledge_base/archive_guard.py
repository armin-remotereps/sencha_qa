"""One safety policy for archives (zip, tar, 7z) and OOXML containers.

Pure: no ORM, never writes to disk. Limits come from settings:
``KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS`` (files per archive),
``KNOWLEDGE_BASE_ARCHIVE_MAX_UNCOMPRESSED_BYTES`` (bytes read in total),
``KNOWLEDGE_BASE_MAX_FILE_BYTES`` (one member) and a compression ratio of at
most ``MAX_COMPRESSION_RATIO``, per member where the format declares sizes and
for the whole archive (bytes read against compressed bytes) for every format.

Ratios are enforced only once more than ``RATIO_FLOOR_BYTES`` were read (or
are declared), so a small, very repetitive text file is not refused while a
bomb still is.

Member paths are display labels only: they are never joined to a filesystem
path, and ``normalise_label`` strips ``..`` and leading slashes for display.
"""

from __future__ import annotations

import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass
from typing import IO

from django.conf import settings

from knowledge_base.exceptions import ArchiveRejected, EncryptedDocument
from knowledge_base.formats import kind_for_name
from knowledge_base.models import KNOWLEDGE_NAME_MAX_LENGTH, KnowledgeKind

MAX_COMPRESSION_RATIO = 100
RATIO_FLOOR_BYTES = 1024 * 1024
READ_BLOCK_BYTES = 64 * 1024
# zip general purpose flag bit 0: the member is encrypted.
_ZIP_FLAG_ENCRYPTED = 0x1
_ZIP_UNIX_SYSTEM = 3


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int
    max_total_bytes: int
    max_member_bytes: int


def archive_limits() -> ArchiveLimits:
    return ArchiveLimits(
        max_members=settings.KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS,
        max_total_bytes=settings.KNOWLEDGE_BASE_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
        max_member_bytes=settings.KNOWLEDGE_BASE_MAX_FILE_BYTES,
    )


def format_size(size_bytes: int) -> str:
    """A short human size ("25 MiB", "300 KiB", "12 bytes")."""
    for unit, factor in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if size_bytes >= factor:
            value = size_bytes / factor
            text = (
                f"{value:.0f}" if value >= 10 or value.is_integer() else f"{value:.1f}"
            )
            return f"{text} {unit}"
    return f"{size_bytes} bytes"


def too_many_members(noun: str, limits: ArchiveLimits) -> ArchiveRejected:
    return ArchiveRejected(f"This {noun} holds more than {limits.max_members} files.")


def too_big(noun: str, limits: ArchiveLimits) -> ArchiveRejected:
    return ArchiveRejected(
        f"This {noun} expands to more than {format_size(limits.max_total_bytes)}."
    )


def ratio_too_high(noun: str) -> ArchiveRejected:
    return ArchiveRejected(
        f"This {noun} is compressed more than {MAX_COMPRESSION_RATIO} times "
        "(it looks like a zip bomb)."
    )


def damaged(noun: str) -> ArchiveRejected:
    return ArchiveRejected(f"This {noun} is damaged or could not be read.")


def encrypted(noun: str) -> EncryptedDocument:
    return EncryptedDocument(
        f"This {noun} is password protected; upload it without a password."
    )


def ratio_exceeded(uncompressed: int, compressed: int) -> bool:
    """True when ``uncompressed`` is above the floor and the ratio limit."""
    if uncompressed <= RATIO_FLOOR_BYTES:
        return False
    return uncompressed > MAX_COMPRESSION_RATIO * max(compressed, 1)


def check_member_count(count: int, noun: str, limits: ArchiveLimits) -> None:
    if count > limits.max_members:
        raise too_many_members(noun, limits)


def check_declared_total(
    declared_bytes: int, archive_bytes: int, noun: str, limits: ArchiveLimits
) -> None:
    """Refuse before reading when the declared sizes already break a limit."""
    if declared_bytes > limits.max_total_bytes:
        raise too_big(noun, limits)
    if ratio_exceeded(declared_bytes, archive_bytes):
        raise ratio_too_high(noun)


class ArchiveBudget:
    """Counts every byte read out of one archive against the total and ratio."""

    def __init__(self, archive_bytes: int, noun: str, limits: ArchiveLimits) -> None:
        self._archive_bytes = archive_bytes
        self._noun = noun
        self._limits = limits
        self.total_bytes = 0

    def add(self, count: int, *, compressed_bytes: int | None = None) -> None:
        """Count ``count`` more bytes read.

        ``compressed_bytes`` is how much of the archive was consumed so far
        when the reader knows it (tar.gz); otherwise the archive size is used.
        """
        self.total_bytes += count
        if self.total_bytes > self._limits.max_total_bytes:
            raise too_big(self._noun, self._limits)
        consumed = self._archive_bytes if compressed_bytes is None else compressed_bytes
        if ratio_exceeded(self.total_bytes, consumed):
            raise ratio_too_high(self._noun)


def read_capped(
    stream: IO[bytes], max_bytes: int, budget: ArchiveBudget | None, noun: str
) -> bytes:
    """Read ``stream`` in blocks; refuse once it exceeds ``max_bytes``.

    Declared sizes are never trusted: the cap is applied to bytes actually
    read. ``budget`` is None when the bytes are already counted upstream.
    """
    buffer = bytearray()
    while True:
        block = stream.read(READ_BLOCK_BYTES)
        if not block:
            return bytes(buffer)
        if len(buffer) + len(block) > max_bytes:
            raise ArchiveRejected(
                f"A file in this {noun} is larger than its declared size allows."
            )
        if budget is not None:
            budget.add(len(block))
        buffer += block


def normalise_label(name: str) -> str:
    """Display path of a member: "/" separators, no ".", "..", drive or root."""
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):
        cleaned = "".join(char for char in part if char.isprintable()).strip()
        if cleaned in ("", ".", ".."):
            continue
        if len(parts) == 0 and len(cleaned) == 2 and cleaned.endswith(":"):
            continue
        parts.append(cleaned)
    label = "/".join(parts)
    if len(label) > KNOWLEDGE_NAME_MAX_LENGTH:
        # Keep the end: the file name and its extension matter most.
        label = "…" + label[-(KNOWLEDGE_NAME_MAX_LENGTH - 1) :]
    return label


def skip_reason(label: str, size_bytes: int, limits: ArchiveLimits) -> str | None:
    """Why a regular member is not read, or None when it is accepted."""
    parts = label.split("/")
    if parts[0] == "__MACOSX":
        return "macOS metadata"
    if any(part.startswith(".") for part in parts):
        return "hidden file"
    kind = kind_for_name(posixpath.basename(label))
    if kind is None:
        return "unsupported type"
    if kind == KnowledgeKind.ARCHIVE:
        return "nested archive"
    if size_bytes > limits.max_member_bytes:
        return f"larger than {format_size(limits.max_member_bytes)}"
    return None


def zip_member_is_special(info: zipfile.ZipInfo) -> bool:
    """True for a Unix symlink, device, FIFO or socket stored in a zip."""
    if info.create_system != _ZIP_UNIX_SYSTEM:
        return False
    mode = info.external_attr >> 16
    return mode != 0 and stat.S_IFMT(mode) != 0 and not stat.S_ISREG(mode)


def zip_member_is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & _ZIP_FLAG_ENCRYPTED)


def check_ooxml_container(data: bytes) -> None:
    """Run the archive policy on a .docx / .xlsx before its library opens it.

    ``zipfile`` (which python-docx and openpyxl use) never returns more than a
    member's declared size, so checking the declared sizes bounds what the
    libraries can read.
    """
    noun = "document"
    limits = archive_limits()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise damaged(noun) from exc
    with archive:
        infos = archive.infolist()
        check_member_count(len(infos), noun, limits)
        if any(zip_member_is_encrypted(info) for info in infos):
            raise encrypted(noun)
        declared = 0
        for info in infos:
            if ratio_exceeded(info.file_size, info.compress_size):
                raise ratio_too_high(noun)
            declared += info.file_size
        check_declared_total(declared, len(data), noun, limits)
