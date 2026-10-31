"""One safety policy for archives (zip, tar, 7z) and OOXML containers.

Pure: no ORM, never writes to disk. Limits come from settings:
``KNOWLEDGE_BASE_ARCHIVE_MAX_ENTRIES`` (raw entries, directories excluded),
``KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS`` (readable files, nested archives'
files included), ``KNOWLEDGE_BASE_ARCHIVE_MAX_UNCOMPRESSED_BYTES`` (bytes
expanded in total), ``KNOWLEDGE_BASE_MAX_FILE_BYTES`` (one member) and a
compression ratio of at most ``MAX_COMPRESSION_RATIO``, per member where the
format declares sizes and for the whole archive (bytes read against
compressed bytes) for every format. Over any limit the whole archive is
refused; nothing is expanded partly.

Ratios are enforced only once more than ``RATIO_FLOOR_BYTES`` were read (or
are declared), so a small, very repetitive text file is not refused while a
bomb still is.

Member paths are display and filter values only: they are never joined to a
filesystem path, and ``normalise_member_path`` strips ``..``, drives and
leading slashes.
"""

from __future__ import annotations

import io
import posixpath
import stat
import struct
import zipfile
from dataclasses import dataclass
from typing import IO

from django.conf import settings

from knowledge_base.exceptions import (
    ArchiveDamaged,
    ArchiveRejected,
    EncryptedDocument,
)
from knowledge_base.formats import kind_for_name
from knowledge_base.models import KNOWLEDGE_NAME_MAX_LENGTH, KnowledgeKind

MAX_COMPRESSION_RATIO = 100
RATIO_FLOOR_BYTES = 1024 * 1024
READ_BLOCK_BYTES = 64 * 1024
ENCRYPTED_MEMBER_REASON = "password protected"
# zip general purpose flag bit 0: the member is encrypted.
_ZIP_FLAG_ENCRYPTED = 0x1
_ZIP_UNIX_SYSTEM = 3
_ARCHIVE_NOUN = "archive"
_SPLIT_HINT = " Split it into smaller archives."

# zip end-of-central-directory records (APPNOTE 4.3.14 - 4.3.16).
_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_MAX_COMMENT = 0xFFFF
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_CENTRAL_HEADER = struct.Struct("<4s4B4HL2L5H2L")
_CENTRAL_HEADER_SIGNATURE = b"PK\x01\x02"


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int
    max_entries: int
    max_total_bytes: int
    max_member_bytes: int


def archive_limits() -> ArchiveLimits:
    return ArchiveLimits(
        max_members=settings.KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS,
        max_entries=settings.KNOWLEDGE_BASE_ARCHIVE_MAX_ENTRIES,
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


def _hint(noun: str) -> str:
    return _SPLIT_HINT if noun == _ARCHIVE_NOUN else ""


def too_many_members(found: int, noun: str, limits: ArchiveLimits) -> ArchiveRejected:
    return ArchiveRejected(
        f"This {noun} holds {found} readable files; the limit is "
        f"{limits.max_members}.{_hint(noun)}"
    )


def too_many_entries(found: int, noun: str, limits: ArchiveLimits) -> ArchiveRejected:
    return ArchiveRejected(
        f"This {noun} holds {found} entries; the limit is "
        f"{limits.max_entries}.{_hint(noun)}"
    )


def too_big_declared(found: int, noun: str, limits: ArchiveLimits) -> ArchiveRejected:
    return ArchiveRejected(
        f"This {noun} expands to {format_size(found)} of readable files; the "
        f"limit is {format_size(limits.max_total_bytes)}.{_hint(noun)}"
    )


def too_big(noun: str, limits: ArchiveLimits) -> ArchiveRejected:
    """Refusal on bytes actually read (the size is unknown in advance)."""
    return ArchiveRejected(
        f"This {noun} expands to more than "
        f"{format_size(limits.max_total_bytes)}.{_hint(noun)}"
    )


def ratio_too_high(noun: str) -> ArchiveRejected:
    return ArchiveRejected(
        f"This {noun} is compressed more than {MAX_COMPRESSION_RATIO} times "
        "(it looks like a zip bomb)."
    )


def damaged(noun: str) -> ArchiveDamaged:
    return ArchiveDamaged(f"This {noun} is damaged or could not be read.")


def encrypted(noun: str) -> EncryptedDocument:
    return EncryptedDocument(
        f"This {noun} is password protected; upload it without a password."
    )


def ratio_exceeded(uncompressed: int, compressed: int) -> bool:
    """True when ``uncompressed`` is above the floor and the ratio limit."""
    if uncompressed <= RATIO_FLOOR_BYTES:
        return False
    return uncompressed > MAX_COMPRESSION_RATIO * max(compressed, 1)


def check_entry_count(count: int, noun: str, limits: ArchiveLimits) -> None:
    """Refuse on the raw entry count (directories excluded)."""
    if count > limits.max_entries:
        raise too_many_entries(count, noun, limits)


def check_member_count(count: int, noun: str, limits: ArchiveLimits) -> None:
    """Refuse on the readable file count (entries that will become items)."""
    if count > limits.max_members:
        raise too_many_members(count, noun, limits)


def check_declared_total(
    declared_bytes: int, archive_bytes: int, noun: str, limits: ArchiveLimits
) -> None:
    """Refuse before reading when the declared sizes already break a limit."""
    if declared_bytes > limits.max_total_bytes:
        raise too_big_declared(declared_bytes, noun, limits)
    if ratio_exceeded(declared_bytes, archive_bytes):
        raise ratio_too_high(noun)


class ArchiveAllowance:
    """What one upload may expand to, shared by an archive and its nested ones.

    ``files`` counts readable files and ``declared_bytes`` their declared
    sizes (plus the nested archives' own bytes, which are read too) across
    both levels; ``read_bytes`` counts every byte actually read. Over a limit
    the whole outer archive is refused.
    """

    def __init__(self, limits: ArchiveLimits) -> None:
        self.limits = limits
        self.files = 0
        self.declared_bytes = 0
        self.read_bytes = 0

    def add_files(self, count: int, noun: str) -> None:
        self.files += count
        check_member_count(self.files, noun, self.limits)

    def add_declared(self, size_bytes: int, noun: str) -> None:
        self.declared_bytes += size_bytes
        if self.declared_bytes > self.limits.max_total_bytes:
            raise too_big_declared(self.declared_bytes, noun, self.limits)

    def add_read(self, count: int, noun: str) -> None:
        self.read_bytes += count
        if self.read_bytes > self.limits.max_total_bytes:
            raise too_big(noun, self.limits)

    @property
    def read_bytes_left(self) -> int:
        return max(self.limits.max_total_bytes - self.read_bytes, 0)


class ArchiveBudget:
    """Counts every byte read out of one archive against the total and ratio.

    With an ``allowance`` the bytes also count toward the total shared with
    the outer (or nested) archive.
    """

    def __init__(
        self,
        archive_bytes: int,
        noun: str,
        limits: ArchiveLimits,
        allowance: ArchiveAllowance | None = None,
    ) -> None:
        self._archive_bytes = archive_bytes
        self._noun = noun
        self._limits = limits
        self._allowance = allowance
        self.total_bytes = 0

    def add(self, count: int, *, compressed_bytes: int | None = None) -> None:
        """Count ``count`` more bytes read.

        ``compressed_bytes`` is how much of the archive was consumed so far
        when the reader knows it (tar.gz); otherwise the archive size is used.
        """
        self.total_bytes += count
        if self.total_bytes > self._limits.max_total_bytes:
            raise too_big(self._noun, self._limits)
        if self._allowance is not None:
            self._allowance.add_read(count, self._noun)
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


def normalise_member_path(name: str) -> str:
    """Path of a member: "/" separators, no ".", "..", drive or root; uncapped."""
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):
        cleaned = "".join(char for char in part if char.isprintable()).strip()
        if cleaned in ("", ".", ".."):
            continue
        if len(parts) == 0 and len(cleaned) == 2 and cleaned.endswith(":"):
            continue
        parts.append(cleaned)
    return "/".join(parts)


def display_label(path: str) -> str:
    """``path`` within ``KNOWLEDGE_NAME_MAX_LENGTH``, keeping its end."""
    if len(path) <= KNOWLEDGE_NAME_MAX_LENGTH:
        return path
    # Keep the end: the file name and its extension matter most.
    return "…" + path[-(KNOWLEDGE_NAME_MAX_LENGTH - 1) :]


def skip_reason(
    path: str, size_bytes: int, limits: ArchiveLimits, *, allow_archives: bool
) -> str | None:
    """Why a regular member is not read, or None when it is accepted.

    A nested archive is accepted only when ``allow_archives`` (one level).
    """
    parts = path.split("/")
    if parts[0] == "__MACOSX":
        return "macOS metadata"
    if any(part.startswith(".") for part in parts):
        return "hidden file"
    kind = kind_for_name(posixpath.basename(path))
    if kind is None:
        return "unsupported type"
    if kind == KnowledgeKind.ARCHIVE and not allow_archives:
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


def zip_entry_count(data: bytes) -> int | None:
    """Non-directory entries in a zip's central directory, without ``ZipFile``.

    ``zipfile`` builds a ``ZipInfo`` for every entry when it opens, so a
    header bomb (a central directory of millions of tiny entries) costs
    memory before any count applies. This walks the headers in place and
    keeps nothing. None when the layout is unusual (no end record, a
    multi-disk archive, a header out of place); the caller then lets
    ``zipfile`` decide.
    """
    start = max(len(data) - _EOCD.size - _EOCD_MAX_COMMENT, 0)
    eocd_at = data.rfind(_EOCD_SIGNATURE, start)
    if eocd_at < 0 or eocd_at + _EOCD.size > len(data):
        return None
    _sig, disk, cd_disk, _, _, cd_size, _, _ = _EOCD.unpack_from(data, eocd_at)
    if disk != 0 or cd_disk != 0:
        return None
    end_of_cd = eocd_at
    locator_at = eocd_at - _ZIP64_LOCATOR.size
    if (
        locator_at >= 0
        and data[locator_at : locator_at + 4] == _ZIP64_LOCATOR_SIGNATURE
    ):
        # ZIP64: the 64-bit record sits right before the locator.
        record_at = locator_at - _ZIP64_EOCD.size
        if record_at < 0 or data[record_at : record_at + 4] != _ZIP64_EOCD_SIGNATURE:
            return None
        cd_size = _ZIP64_EOCD.unpack_from(data, record_at)[8]
        end_of_cd = record_at
    # Measured back from the end record, like zipfile, so data prepended to
    # the archive (a self-extractor stub) does not shift the directory.
    position = end_of_cd - cd_size
    if position < 0:
        return None
    count = 0
    while position < end_of_cd:
        if position + _CENTRAL_HEADER.size > end_of_cd:
            return None
        header = _CENTRAL_HEADER.unpack_from(data, position)
        if header[0] != _CENTRAL_HEADER_SIGNATURE:
            return None
        name_length, extra_length, comment_length = header[12], header[13], header[14]
        name_end = position + _CENTRAL_HEADER.size + name_length
        # zipfile's ZipInfo.is_dir(): the name ends with "/".
        if data[name_end - 1 : name_end] != b"/" or name_length == 0:
            count += 1
        position = name_end + extra_length + comment_length
    return count


def check_zip_entry_count(data: bytes, noun: str, limits: ArchiveLimits) -> None:
    """Refuse a zip over the entry limit before ``zipfile`` parses it."""
    count = zip_entry_count(data)
    if count is not None:
        check_entry_count(count, noun, limits)


def check_ooxml_container(data: bytes) -> None:
    """Run the archive policy on a .docx / .xlsx before its library opens it.

    ``zipfile`` (which python-docx and openpyxl use) never returns more than a
    member's declared size, so checking the declared sizes bounds what the
    libraries can read. Every part counts here (a document has no "readable
    file" notion), so the raw entry limit applies.
    """
    noun = "document"
    limits = archive_limits()
    check_zip_entry_count(data, noun, limits)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise damaged(noun) from exc
    with archive:
        infos = archive.infolist()
        check_entry_count(len(infos), noun, limits)
        if any(zip_member_is_encrypted(info) for info in infos):
            raise encrypted(noun)
        declared = 0
        for info in infos:
            if ratio_exceeded(info.file_size, info.compress_size):
                raise ratio_too_high(noun)
            declared += info.file_size
        check_declared_total(declared, len(data), noun, limits)
