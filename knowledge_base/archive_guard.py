"""One safety policy for archives (zip, tar, 7z) and OOXML containers.

Pure: no ORM, never writes to disk. Limits come from settings:
``KNOWLEDGE_BASE_ARCHIVE_MAX_ENTRIES`` (raw entries, directories excluded),
``KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS`` (readable files, nested archives'
files included), ``KNOWLEDGE_BASE_ARCHIVE_MAX_UNCOMPRESSED_BYTES`` (bytes
expanded in total), ``KNOWLEDGE_BASE_MAX_FILE_BYTES`` (one member) and a
compression ratio of at most ``MAX_COMPRESSION_RATIO``, per member where the
format declares sizes and for the whole archive (bytes read against
compressed bytes) for every format. Over any limit the whole archive is
refused; nothing is expanded partly. A .docx / .xlsx container has its own,
smaller part limit (``OOXML_MAX_PARTS``).

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
# Parts of a .docx / .xlsx container (directories included); real documents
# hold a few dozen.
OOXML_MAX_PARTS = 500
# Skip reasons kept for the warning; past it only a count is kept, so an
# archive of many skipped entries never becomes as many strings.
SKIPPED_KEEP_MAX = 200
# zip general purpose flag bit 0: the member is encrypted.
_ZIP_FLAG_ENCRYPTED = 0x1
_ZIP_UNIX_SYSTEM = 3
_ARCHIVE_NOUN = "archive"
_SPLIT_HINT = " Split it into smaller archives."

# zip end-of-central-directory records (APPNOTE 4.3.14 - 4.3.16).
_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_MAX_COMMENT = 0xFFFF
_EOCD_NO_COMMENT = b"\x00\x00"
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


def _too_many(found: int | None, what: str, limit: int, noun: str) -> ArchiveRejected:
    """``found`` is the true total, or None when counting stopped past the
    limit (streamed, a running count across nested archives, or a walk that
    stops early): then the message says "more than"."""
    held = f"more than {limit}" if found is None else str(found)
    return ArchiveRejected(
        f"This {noun} holds {held} {what}; the limit is {limit}.{_hint(noun)}"
    )


def too_many_members(
    found: int | None, noun: str, limits: ArchiveLimits
) -> ArchiveRejected:
    return _too_many(found, "readable files", limits.max_members, noun)


def too_many_entries(
    found: int | None, noun: str, limits: ArchiveLimits
) -> ArchiveRejected:
    return _too_many(found, "entries", limits.max_entries, noun)


def too_many_parts(found: int | None, noun: str) -> ArchiveRejected:
    return _too_many(found, "parts", OOXML_MAX_PARTS, noun)


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


def check_declared_total(
    declared_bytes: int, archive_bytes: int, noun: str, limits: ArchiveLimits
) -> None:
    """Refuse before reading when the declared sizes already break a limit."""
    if declared_bytes > limits.max_total_bytes:
        raise too_big_declared(declared_bytes, noun, limits)
    if ratio_exceeded(declared_bytes, archive_bytes):
        raise ratio_too_high(noun)


class SkippedEntries:
    """Why members were not read: at most ``SKIPPED_KEEP_MAX`` reasons are
    kept, the rest only counted (``overflow``)."""

    def __init__(self) -> None:
        self.entries: list[str] = []
        self.overflow = 0

    def add(self, entry: str) -> None:
        if len(self.entries) < SKIPPED_KEEP_MAX:
            self.entries.append(entry)
        else:
            self.overflow += 1

    @property
    def total(self) -> int:
        return len(self.entries) + self.overflow


class ArchiveAllowance:
    """What one upload may expand to, shared by an archive and its nested ones.

    ``entries`` counts raw entries (directories excluded), ``files`` readable
    files and ``declared_bytes`` their declared sizes (plus the nested
    archives' own bytes, which are read too) across both levels;
    ``read_bytes`` counts every byte actually read. Over a limit the whole
    outer archive is refused.

    ``complete`` tells that ``count`` is a level's whole count: a refusal on
    the first such count names it, any other says "more than" the limit.
    """

    def __init__(self, limits: ArchiveLimits) -> None:
        self.limits = limits
        self.entries = 0
        self.files = 0
        self.declared_bytes = 0
        self.read_bytes = 0

    def add_entries(self, count: int, noun: str, *, complete: bool = False) -> None:
        first = self.entries == 0
        self.entries += count
        if self.entries > self.limits.max_entries:
            found = self.entries if complete and first else None
            raise too_many_entries(found, noun, self.limits)

    def add_files(self, count: int, noun: str, *, complete: bool = False) -> None:
        first = self.files == 0
        self.files += count
        if self.files > self.limits.max_members:
            found = self.files if complete and first else None
            raise too_many_members(found, noun, self.limits)

    def check_zip_entries(self, data: bytes, noun: str) -> None:
        """Refuse a zip that would pass the entry limit before ``zipfile``
        parses it; the entries already counted (outer or earlier nested
        archives) count too."""
        left = max(self.limits.max_entries - self.entries, 0)
        # Directories are not entries: they keep the full limit as their cap.
        count = zip_entry_count(data, left, max_directories=self.limits.max_entries)
        if count is None:
            raise damaged(noun)
        if count > left:
            raise too_many_entries(None, noun, self.limits)

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


def zip_entry_count(
    data: bytes, max_entries: int, *, max_directories: int | None = None
) -> int | None:
    """Non-directory entries in a zip's central directory, without ``ZipFile``.

    ``zipfile`` builds a ``ZipInfo`` for every entry when it opens, so a
    header bomb (a central directory of millions of tiny entries) costs
    memory before any count applies. This finds the directory the way
    ``zipfile`` does (``_EndRecData`` / ``_EndRecData64`` /
    ``_RealGetContents``) and walks its headers in place, keeping nothing.
    The walk stops as soon as more than ``max_entries`` files, or more than
    ``max_directories`` (default ``max_entries``) directories, were seen and
    then returns ``max_entries + 1``; every step consumes at least one header, so it is
    bounded by ``max_entries`` and the data length whatever the records
    claim.

    None only where ``zipfile`` refuses the archive too (no end record, a
    directory out of place or cut short).
    """
    end_record = _zip_end_record(data)
    if end_record is None:
        return None
    regions = _zip_directory_regions(data, *end_record)
    if regions is None:
        return None
    best: int | None = None
    for start, size in regions:
        count = _walk_zip_directory(
            data,
            start,
            size,
            max_entries,
            max_entries if max_directories is None else max_directories,
        )
        if count is not None and count > max_entries:
            return count
        if count is not None and (best is None or count > best):
            best = count
    return best


def _zip_end_record(data: bytes) -> tuple[int, int] | None:
    """(offset, central directory size) of the end record zipfile uses."""
    # No comment: the record is the last 22 bytes (checked first, as zipfile
    # does, so a signature planted inside the record is never picked).
    last = len(data) - _EOCD.size
    if (
        last >= 0
        and data[last : last + 4] == _EOCD_SIGNATURE
        and data[-2:] == _EOCD_NO_COMMENT
    ):
        return last, _EOCD.unpack_from(data, last)[5]
    window = max(len(data) - _EOCD_MAX_COMMENT - _EOCD.size, 0)
    found = data.rfind(_EOCD_SIGNATURE, window)
    if found < 0 or found + _EOCD.size > len(data):
        return None
    return found, _EOCD.unpack_from(data, found)[5]


def _zip_directory_regions(
    data: bytes, eocd_at: int, cd_size: int
) -> list[tuple[int, int]] | None:
    """Every (start, size) zipfile may read the central directory from.

    The disk numbers of the plain end record are ignored, like zipfile does.
    For ZIP64 both the Python 3.13 lookup (record at the locator's offset,
    else right before the locator) and the older one (right before the
    locator, else the plain record) are listed; the caller keeps the largest
    count, so the walk never undercounts what this interpreter parses.
    """
    plain = (eocd_at - cd_size, cd_size)
    offset = eocd_at - _ZIP64_LOCATOR.size
    if offset < 0 or data[offset : offset + 4] != _ZIP64_LOCATOR_SIGNATURE:
        return [plain]
    _sig, disk, record_offset, disks = _ZIP64_LOCATOR.unpack_from(data, offset)
    if disk != 0 or disks > 1:
        return None
    offset -= _ZIP64_EOCD.size
    regions: list[tuple[int, int]] = []
    if 0 <= offset and record_offset <= offset:
        record_at: int | None = None
        if data[record_offset : record_offset + 4] == _ZIP64_EOCD_SIGNATURE:
            record_at = record_offset
        elif data[offset : offset + 4] == _ZIP64_EOCD_SIGNATURE:
            record_at = offset
        if record_at is not None:
            size = _ZIP64_EOCD.unpack_from(data, record_at)[8]
            regions.append((record_at - size, size))
    if 0 <= offset and data[offset : offset + 4] == _ZIP64_EOCD_SIGNATURE:
        size = _ZIP64_EOCD.unpack_from(data, offset)[8]
        regions.append((offset - size, size))
    else:
        regions.append(plain)
    return list(dict.fromkeys(regions))


def _walk_zip_directory(
    data: bytes, start: int, size: int, max_entries: int, max_directories: int
) -> int | None:
    """Files in the directory at ``start``, as zipfile walks it; None where
    zipfile fails, ``max_entries + 1`` once past the limit."""
    if start < 0:
        return None
    # zipfile reads at most ``size`` bytes from ``start``.
    end = min(start + size, len(data))
    position = start
    files = 0
    directories = 0
    while position - start < size:
        if position + _CENTRAL_HEADER.size > end:
            return None
        header = _CENTRAL_HEADER.unpack_from(data, position)
        if header[0] != _CENTRAL_HEADER_SIGNATURE:
            return None
        name_length, extra_length, comment_length = header[12], header[13], header[14]
        name_at = position + _CENTRAL_HEADER.size
        name = data[name_at : min(name_at + name_length, end)]
        # zipfile cuts a name at its first NUL; is_dir() is a trailing "/".
        name = name.split(b"\x00", 1)[0]
        if name.endswith(b"/"):
            directories += 1
        else:
            files += 1
        if files > max_entries or directories > max_directories:
            return max_entries + 1
        position = name_at + name_length + extra_length + comment_length
    return files


def check_ooxml_container(data: bytes) -> None:
    """Run the archive policy on a .docx / .xlsx before its library opens it.

    ``zipfile`` (which python-docx and openpyxl use) never returns more than a
    member's declared size, so checking the declared sizes bounds what the
    libraries can read. Every part counts here (a document has no "readable
    file" notion), against the container's own ``OOXML_MAX_PARTS``.
    """
    noun = "document"
    limits = archive_limits()
    count = zip_entry_count(data, OOXML_MAX_PARTS)
    if count is None:
        raise damaged(noun)
    if count > OOXML_MAX_PARTS:
        raise too_many_parts(None, noun)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise damaged(noun) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > OOXML_MAX_PARTS:
            raise too_many_parts(len(infos), noun)
        if any(zip_member_is_encrypted(info) for info in infos):
            raise encrypted(noun)
        declared = 0
        for info in infos:
            if ratio_exceeded(info.file_size, info.compress_size):
                raise ratio_too_high(noun)
            declared += info.file_size
        check_declared_total(declared, len(data), noun, limits)
