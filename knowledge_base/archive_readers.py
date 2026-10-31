"""Read the members of a zip, tar(.gz) or 7z archive in memory, under the guard.

Nothing is ever written to disk and member paths are display labels only.
Whole-archive problems raise ``ArchiveRejected`` / ``EncryptedDocument``;
members that are not read are listed in ``skipped`` with a short reason.
"""

from __future__ import annotations

import io
import lzma
import tarfile
import zipfile
import zlib
from collections.abc import Buffer, Iterator

import py7zr
from py7zr.exceptions import ArchiveError, DecompressionBombError, PasswordRequired

from knowledge_base.archive_guard import (
    READ_BLOCK_BYTES,
    ArchiveBudget,
    ArchiveLimits,
    archive_limits,
    check_declared_total,
    check_member_count,
    damaged,
    encrypted,
    normalise_label,
    ratio_exceeded,
    ratio_too_high,
    read_capped,
    skip_reason,
    too_big,
    zip_member_is_encrypted,
    zip_member_is_special,
)
from knowledge_base.exceptions import ArchiveRejected
from knowledge_base.sniffing import check_archive_content
from knowledge_base.types import ArchiveMemberRef

NOUN = "archive"
GZIP_MAGIC = b"\x1f\x8b"
# py7zr's id of the 7z AES-256 + SHA-256 coder.
_SEVEN_ZIP_AES_METHOD = b"\x06\xf1\x07\x01"
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError)


def iter_archive_members(
    fmt: str, data: bytes, skipped: list[str]
) -> Iterator[ArchiveMemberRef]:
    """Yield the accepted members of an archive of ``fmt`` ("zip", "tar", "7z")."""
    check_archive_content(fmt, data)
    limits = archive_limits()
    if fmt == "zip":
        yield from _iter_zip(data, skipped, limits)
    elif fmt == "tar":
        yield from _iter_tar(data, skipped, limits)
    else:
        yield from _iter_7z(data, skipped, limits)


# --- zip --------------------------------------------------------------------


def _iter_zip(
    data: bytes, skipped: list[str], limits: ArchiveLimits
) -> Iterator[ArchiveMemberRef]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise damaged(NOUN) from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        check_member_count(len(infos), NOUN, limits)
        if any(zip_member_is_encrypted(info) for info in infos):
            raise encrypted(NOUN)
        check_declared_total(
            sum(info.file_size for info in infos), len(data), NOUN, limits
        )
        budget = ArchiveBudget(len(data), NOUN, limits)
        for info in infos:
            label = normalise_label(info.filename)
            if not label:
                continue
            if zip_member_is_special(info):
                skipped.append(f"{label} (not a regular file)")
                continue
            reason = skip_reason(label, info.file_size, limits)
            if reason is not None:
                skipped.append(f"{label} ({reason})")
                continue
            if ratio_exceeded(info.file_size, info.compress_size):
                raise ratio_too_high(NOUN)
            try:
                with archive.open(info) as stream:
                    content = read_capped(stream, limits.max_member_bytes, budget, NOUN)
            except NotImplementedError:
                skipped.append(f"{label} (unsupported compression)")
                continue
            except _ZIP_READ_ERRORS as exc:
                raise damaged(NOUN) from exc
            if not content:
                skipped.append(f"{label} (empty)")
                continue
            yield ArchiveMemberRef(label=label, data=content)


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


def _iter_tar(
    data: bytes, skipped: list[str], limits: ArchiveLimits
) -> Iterator[ArchiveMemberRef]:
    budget = ArchiveBudget(len(data), NOUN, limits)
    source = _CappedTarSource(data, budget)
    try:
        # Stream mode: headers are read in order, nothing seeks or extracts.
        archive = tarfile.open(fileobj=source, mode="r|")
    except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
        raise damaged(NOUN) from exc
    count = 0
    with archive:
        while True:
            try:
                member = archive.next()
            except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
                raise damaged(NOUN) from exc
            if member is None:
                return
            if member.isdir():
                continue
            count += 1
            check_member_count(count, NOUN, limits)
            # The member's bytes are read (or skipped) through the budget
            # anyway; refusing on its declared size only fails sooner.
            if budget.total_bytes + member.size > limits.max_total_bytes:
                raise too_big(NOUN, limits)
            label = normalise_label(member.name)
            if not label:
                continue
            if not member.isreg():
                skipped.append(f"{label} (not a regular file)")
                continue
            reason = skip_reason(label, member.size, limits)
            if reason is not None:
                skipped.append(f"{label} ({reason})")
                continue
            stream = archive.extractfile(member)
            if stream is None:
                skipped.append(f"{label} (not a regular file)")
                continue
            try:
                # Already counted by the source; only the member cap applies.
                content = read_capped(stream, limits.max_member_bytes, None, NOUN)
            except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
                raise damaged(NOUN) from exc
            if not content:
                skipped.append(f"{label} (empty)")
                continue
            yield ArchiveMemberRef(label=label, data=content)


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


def _iter_7z(
    data: bytes, skipped: list[str], limits: ArchiveLimits
) -> Iterator[ArchiveMemberRef]:
    try:
        # A file object (not a path) keeps py7zr's extraction single-threaded,
        # so a writer exception stops it at once. max_extract_size also counts
        # the members py7zr decompresses only to reach or check others.
        archive = py7zr.SevenZipFile(
            io.BytesIO(data), mode="r", max_extract_size=limits.max_total_bytes
        )
    except PasswordRequired as exc:
        raise encrypted(NOUN) from exc
    except _SEVEN_ZIP_ERRORS as exc:
        raise damaged(NOUN) from exc
    with archive:
        if _seven_zip_is_encrypted(archive):
            raise encrypted(NOUN)
        files = [entry for entry in archive.files if not entry.is_directory]
        check_member_count(len(files), NOUN, limits)
        check_declared_total(
            sum(entry.uncompressed for entry in files), len(data), NOUN, limits
        )
        targets: list[str] = []
        for entry in files:
            label = normalise_label(entry.filename)
            if not label:
                continue
            if (
                not entry.is_file
                or entry.is_symlink
                or entry.is_junction
                or entry.is_socket
            ):
                skipped.append(f"{label} (not a regular file)")
                continue
            reason = skip_reason(label, entry.uncompressed, limits)
            if reason is not None:
                skipped.append(f"{label} ({reason})")
                continue
            if entry.emptystream or entry.uncompressed == 0:
                skipped.append(f"{label} (empty)")
                continue
            compressed = entry.compressed
            if compressed and ratio_exceeded(entry.uncompressed, compressed):
                raise ratio_too_high(NOUN)
            targets.append(entry.filename)
        if not targets:
            return
        budget = ArchiveBudget(len(data), NOUN, limits)
        factory = _CappedWriterFactory(limits.max_member_bytes, budget)
        try:
            archive.extract(targets=targets, factory=factory)
        except DecompressionBombError as exc:
            raise too_big(NOUN, limits) from exc
        except PasswordRequired as exc:
            raise encrypted(NOUN) from exc
        except _SEVEN_ZIP_ERRORS as exc:
            raise damaged(NOUN) from exc
    for filename, writer in factory.products.items():
        label = normalise_label(filename)
        content = writer.take()
        if label and content:
            yield ArchiveMemberRef(label=label, data=content)


def _seven_zip_is_encrypted(archive: py7zr.SevenZipFile) -> bool:
    main_streams = getattr(archive.header, "main_streams", None)
    if main_streams is None:
        return False
    for folder in main_streams.unpackinfo.folders:
        for coder in folder.coders:
            if coder.get("method") == _SEVEN_ZIP_AES_METHOD:
                return True
    return False
