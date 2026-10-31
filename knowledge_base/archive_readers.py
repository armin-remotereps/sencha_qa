"""Read the members of a zip, tar(.gz) or 7z archive in memory, under the guard.

Nothing is ever written to disk and member paths are display and filter
values only. Whole-archive problems (limits, a damaged or encrypted outer
archive) raise ``ArchiveRejected`` / ``EncryptedDocument``; members that are
not read are listed in ``skipped`` with a short reason.

Nested archives are expanded one level deep: their files are yielded with
the inner archive's path as a prefix (``logs/build-1.zip/agent.log``) and
count toward the same limits as the outer archive's files. A nested archive
that is damaged, encrypted, not an archive at all or a disabled 7z is
skipped; one nested two deep is skipped as "nested archive". The nested
archive's own bytes are never yielded.
"""

from __future__ import annotations

import io
import lzma
import posixpath
import tarfile
import zipfile
import zlib
from collections.abc import Buffer, Iterator

import py7zr
from django.conf import settings
from py7zr.exceptions import ArchiveError, DecompressionBombError, PasswordRequired

from knowledge_base.archive_guard import (
    ENCRYPTED_MEMBER_REASON,
    READ_BLOCK_BYTES,
    ArchiveAllowance,
    ArchiveBudget,
    ArchiveLimits,
    SkippedEntries,
    archive_limits,
    damaged,
    display_label,
    encrypted,
    normalise_member_path,
    ratio_exceeded,
    ratio_too_high,
    read_capped,
    skip_reason,
    too_big,
    too_many_entries,
    zip_member_is_encrypted,
    zip_member_is_special,
)
from knowledge_base.exceptions import (
    ArchiveDamaged,
    ArchiveRejected,
    ContentMismatch,
    EncryptedDocument,
)
from knowledge_base.formats import (
    SEVEN_ZIP_DISABLED_MESSAGE,
    archive_format,
    kind_for_name,
)
from knowledge_base.models import KnowledgeKind
from knowledge_base.sniffing import check_archive_content
from knowledge_base.types import ArchiveMemberRef

NOUN = "archive"
GZIP_MAGIC = b"\x1f\x8b"
# py7zr's id of the 7z AES-256 + SHA-256 coder.
_SEVEN_ZIP_AES_METHOD = b"\x06\xf1\x07\x01"
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError)


class _Level:
    """One archive being read: the outer one, or a nested one inside it."""

    def __init__(
        self,
        limits: ArchiveLimits,
        allowance: ArchiveAllowance,
        skipped: SkippedEntries,
        *,
        prefix: str,
    ) -> None:
        self.limits = limits
        self.allowance = allowance
        self.skipped = skipped
        self.prefix = prefix
        # Only the outer archive may hold archives (one level, D4).
        self.allow_archives = not prefix

    def full_path(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def skip(self, path: str, reason: str) -> None:
        self.skipped.add(f"{display_label(self.full_path(path))} ({reason})")

    def reason_not_read(self, path: str, size_bytes: int) -> str | None:
        return skip_reason(
            path, size_bytes, self.limits, allow_archives=self.allow_archives
        )

    def admit(self, files: int, declared_bytes: int, archive_bytes: int) -> None:
        """Count the accepted members of this level before any is read."""
        self.allowance.add_files(files, NOUN, complete=True)
        self.allowance.add_declared(declared_bytes, NOUN)
        if ratio_exceeded(declared_bytes, archive_bytes):
            raise ratio_too_high(NOUN)

    def member(self, path: str, content: bytes, is_archive: bool) -> ArchiveMemberRef:
        return ArchiveMemberRef(
            path=self.full_path(path), data=content, is_archive=is_archive
        )


def iter_archive_members(
    fmt: str, data: bytes, skipped: SkippedEntries
) -> Iterator[ArchiveMemberRef]:
    """Yield the readable files of an archive of ``fmt`` ("zip", "tar", "7z").

    Files of archives nested one level deep are included; the limits (raw
    entries too) apply to both levels together.
    """
    check_archive_content(fmt, data)
    limits = archive_limits()
    outer = _Level(limits, ArchiveAllowance(limits), skipped, prefix="")
    for member in _iter_level(fmt, data, outer):
        if member.is_archive:
            yield from _iter_nested(member, outer)
        else:
            yield member


def _iter_level(fmt: str, data: bytes, level: _Level) -> Iterator[ArchiveMemberRef]:
    if fmt == "zip":
        return _iter_zip(data, level)
    if fmt == "tar":
        return _iter_tar(data, level)
    return _iter_7z(data, level)


def _is_archive(path: str) -> bool:
    return kind_for_name(posixpath.basename(path)) == KnowledgeKind.ARCHIVE


def _iter_nested(nested: ArchiveMemberRef, outer: _Level) -> Iterator[ArchiveMemberRef]:
    """The files of a nested archive, or a skip entry when it cannot be read.

    Damage or encryption skips only the nested archive (files already read
    from it stay); any limit still refuses the whole outer archive.
    """
    label = display_label(nested.path)
    fmt = archive_format(posixpath.basename(nested.path))
    if fmt is None:
        outer.skipped.add(f"{label} (unsupported type)")
        return
    if fmt == "7z" and not settings.KNOWLEDGE_BASE_ACCEPT_7Z:
        outer.skipped.add(f"{label} ({SEVEN_ZIP_DISABLED_MESSAGE})")
        return
    try:
        check_archive_content(fmt, nested.data)
    except ContentMismatch:
        outer.skipped.add(f"{label} (not a readable archive)")
        return
    inner = _Level(outer.limits, outer.allowance, outer.skipped, prefix=nested.path)
    read = 0
    try:
        for member in _iter_level(fmt, nested.data, inner):
            read += 1
            yield member
    except EncryptedDocument:
        outer.skipped.add(f"{label} ({ENCRYPTED_MEMBER_REASON})")
    except ArchiveDamaged:
        partly = ", read partly" if read else ""
        outer.skipped.add(f"{label} (damaged archive{partly})")


# --- zip --------------------------------------------------------------------


def _iter_zip(data: bytes, level: _Level) -> Iterator[ArchiveMemberRef]:
    limits = level.limits
    level.allowance.check_zip_entries(data, NOUN)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise damaged(NOUN) from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        level.allowance.add_entries(len(infos), NOUN, complete=True)
        accepted: list[tuple[str, zipfile.ZipInfo, bool]] = []
        for info in infos:
            path = normalise_member_path(info.filename)
            if not path:
                continue
            if zip_member_is_special(info):
                level.skip(path, "not a regular file")
                continue
            reason = level.reason_not_read(path, info.file_size)
            if reason is None and zip_member_is_encrypted(info):
                reason = ENCRYPTED_MEMBER_REASON
            if reason is None and info.file_size == 0:
                reason = "empty"
            if reason is not None:
                level.skip(path, reason)
                continue
            if ratio_exceeded(info.file_size, info.compress_size):
                raise ratio_too_high(NOUN)
            accepted.append((path, info, _is_archive(path)))
        # Only accepted members count; zipfile never decompresses the others.
        level.admit(
            files=sum(1 for _, _, is_archive in accepted if not is_archive),
            declared_bytes=sum(info.file_size for _, info, _ in accepted),
            archive_bytes=len(data),
        )
        budget = ArchiveBudget(len(data), NOUN, limits, level.allowance)
        for path, info, is_archive in accepted:
            try:
                with archive.open(info) as stream:
                    content = read_capped(stream, limits.max_member_bytes, budget, NOUN)
            except NotImplementedError:
                level.skip(path, "unsupported compression")
                continue
            except _ZIP_READ_ERRORS as exc:
                raise damaged(NOUN) from exc
            if not content:
                level.skip(path, "empty")
                continue
            yield level.member(path, content, is_archive)


# --- tar --------------------------------------------------------------------


class _CappedTarSource(io.RawIOBase):
    """The (gunzipped) tar byte stream, counted against the archive budget.

    Every byte ``tarfile`` reads, headers and skipped members included, passes
    through here, so the total and the ratio (bytes produced against
    compressed bytes consumed so far) are enforced on the running total.
    """

    def __init__(self, data: bytes, budget: ArchiveBudget) -> None:
        super().__init__()
        self._data = memoryview(data)
        self._budget = budget
        self._position = 0
        self._gzip = data.startswith(GZIP_MAGIC)
        self._inflater = zlib.decompressobj(zlib.MAX_WBITS | 16)
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        produced = self._produce(len(view))
        view[: len(produced)] = produced
        if produced:
            self._budget.add(
                len(produced), compressed_bytes=self._position if self._gzip else None
            )
        return len(produced)

    def _produce(self, size: int) -> bytes:
        if not self._gzip:
            block = bytes(self._data[self._position : self._position + size])
            self._position += len(block)
            return block
        while True:
            if self._inflater.eof:
                # A concatenated (multi-member) gzip continues after the end.
                rest = self._inflater.unused_data + self._pending
                self._pending = b""
                if not rest.strip(b"\x00"):
                    rest = self._next_input()
                    if not rest.strip(b"\x00"):
                        return b""
                self._inflater = zlib.decompressobj(zlib.MAX_WBITS | 16)
                self._pending = rest
            source = self._inflater.unconsumed_tail
            if not source:
                source = self._pending or self._next_input()
                self._pending = b""
            if not source:
                return b""
            output = self._inflater.decompress(source, size)
            if output:
                return output

    def _next_input(self) -> bytes:
        block = bytes(self._data[self._position : self._position + READ_BLOCK_BYTES])
        self._position += len(block)
        return block


def _iter_tar(data: bytes, level: _Level) -> Iterator[ArchiveMemberRef]:
    """Stream a tar: the limits are checked as entries arrive.

    The whole stream (headers and skipped members too) counts toward the
    expanded total, so a tar is refused on its total stream.
    """
    limits = level.limits
    allowance = level.allowance
    budget = ArchiveBudget(len(data), NOUN, limits, allowance)
    source = _CappedTarSource(data, budget)
    try:
        # Stream mode: headers are read in order, nothing seeks or extracts.
        archive = tarfile.open(fileobj=source, mode="r|")
    except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
        raise damaged(NOUN) from exc
    directories = 0
    with archive:
        while True:
            try:
                member = archive.next()
            except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
                raise damaged(NOUN) from exc
            if member is None:
                return
            if member.isdir():
                # Stream mode still keeps every TarInfo: directories are not
                # entries, but are capped like zip directories.
                directories += 1
                if directories > limits.max_entries:
                    raise too_many_entries(None, NOUN, limits)
                continue
            allowance.add_entries(1, NOUN)
            # The member's bytes are read (or skipped) through the budget
            # anyway; refusing on its declared size only fails sooner.
            if allowance.read_bytes + member.size > limits.max_total_bytes:
                raise too_big(NOUN, limits)
            path = normalise_member_path(member.name)
            if not path:
                continue
            if not member.isreg():
                level.skip(path, "not a regular file")
                continue
            reason = level.reason_not_read(path, member.size)
            if reason is None and member.size == 0:
                reason = "empty"
            if reason is not None:
                level.skip(path, reason)
                continue
            is_archive = _is_archive(path)
            allowance.add_files(0 if is_archive else 1, NOUN)
            allowance.add_declared(member.size, NOUN)
            stream = archive.extractfile(member)
            if stream is None:
                level.skip(path, "not a regular file")
                continue
            try:
                # Already counted by the source; only the member cap applies.
                content = read_capped(stream, limits.max_member_bytes, None, NOUN)
            except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
                raise damaged(NOUN) from exc
            if not content:
                level.skip(path, "empty")
                continue
            yield level.member(path, content, is_archive)


# --- 7z ---------------------------------------------------------------------


class _CappedWriter(py7zr.Py7zIO):
    """An in-memory py7zr writer that raises once a cap is exceeded."""

    def __init__(self, max_bytes: int, budget: ArchiveBudget) -> None:
        self._max_bytes = max_bytes
        self._budget = budget
        self._buffer = bytearray()

    def write(self, s: bytes | bytearray) -> int:
        if len(self._buffer) + len(s) > self._max_bytes:
            raise ArchiveRejected(
                "A file in this archive is larger than its declared size allows."
            )
        self._budget.add(len(s))
        self._buffer += s
        return len(s)

    def read(self, size: int | None = None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def flush(self) -> None:
        return None

    def size(self) -> int:
        return len(self._buffer)

    def take(self) -> bytes:
        """The written bytes; the writer is emptied to release memory."""
        content = bytes(self._buffer)
        self._buffer = bytearray()
        return content


class _CappedWriterFactory(py7zr.WriterFactory):
    def __init__(self, max_bytes: int, budget: ArchiveBudget) -> None:
        self._max_bytes = max_bytes
        self._budget = budget
        # Insertion order is extraction order.
        self.products: dict[str, _CappedWriter] = {}

    def create(self, filename: str) -> py7zr.Py7zIO:
        writer = _CappedWriter(self._max_bytes, self._budget)
        self.products[filename] = writer
        return writer


_SEVEN_ZIP_ERRORS = (ArchiveError, lzma.LZMAError, EOFError, OSError, ValueError)


def _iter_7z(data: bytes, level: _Level) -> Iterator[ArchiveMemberRef]:
    limits = level.limits
    try:
        # A file object (not a path) keeps py7zr's extraction single-threaded,
        # so a writer exception stops it at once. max_extract_size also counts
        # the members py7zr decompresses only to reach or check others; it is
        # what is left of the total shared with the outer (or nested) archive.
        archive = py7zr.SevenZipFile(
            io.BytesIO(data),
            mode="r",
            max_extract_size=max(level.allowance.read_bytes_left, 1),
        )
    except PasswordRequired as exc:
        raise encrypted(NOUN) from exc
    except _SEVEN_ZIP_ERRORS as exc:
        raise damaged(NOUN) from exc
    with archive:
        if _seven_zip_is_encrypted(archive):
            raise encrypted(NOUN)
        files = [entry for entry in archive.files if not entry.is_directory]
        level.allowance.add_entries(len(files), NOUN, complete=True)
        # py7zr's file name -> (normalised path, is a nested archive).
        targets: dict[str, tuple[str, bool]] = {}
        declared = 0
        for entry in files:
            path = normalise_member_path(entry.filename)
            if not path:
                continue
            if (
                not entry.is_file
                or entry.is_symlink
                or entry.is_junction
                or entry.is_socket
            ):
                level.skip(path, "not a regular file")
                continue
            reason = level.reason_not_read(path, entry.uncompressed)
            if reason is None and (entry.emptystream or entry.uncompressed == 0):
                reason = "empty"
            if reason is not None:
                level.skip(path, reason)
                continue
            compressed = entry.compressed
            if compressed and ratio_exceeded(entry.uncompressed, compressed):
                raise ratio_too_high(NOUN)
            targets[entry.filename] = (path, _is_archive(path))
            declared += entry.uncompressed
        level.admit(
            files=sum(1 for _, is_archive in targets.values() if not is_archive),
            declared_bytes=declared,
            archive_bytes=len(data),
        )
        if not targets:
            return
        budget = ArchiveBudget(len(data), NOUN, limits, level.allowance)
        factory = _CappedWriterFactory(limits.max_member_bytes, budget)
        try:
            archive.extract(targets=list(targets), factory=factory)
        except DecompressionBombError as exc:
            raise too_big(NOUN, limits) from exc
        except PasswordRequired as exc:
            raise encrypted(NOUN) from exc
        except _SEVEN_ZIP_ERRORS as exc:
            raise damaged(NOUN) from exc
    for filename, writer in factory.products.items():
        target = targets.get(filename)
        content = writer.take()
        if target is None or not content:
            continue
        path, is_archive = target
        yield level.member(path, content, is_archive)


def _seven_zip_is_encrypted(archive: py7zr.SevenZipFile) -> bool:
    main_streams = getattr(archive.header, "main_streams", None)
    if main_streams is None:
        return False
    for folder in main_streams.unpackinfo.folders:
        for coder in folder.coders:
            if coder.get("method") == _SEVEN_ZIP_AES_METHOD:
                return True
    return False
