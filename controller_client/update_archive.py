"""Verify the update bundle and extract it safely into a fresh folder.

Everything is checked in memory (size, sha256, every entry, the bundled
version) before a single file is written; extraction then goes entry by entry
with exclusive creates, never ``ZipFile.extractall``.
"""

import base64
import binascii
import hashlib
import hmac
import io
import os
import re
import shutil
import stat
import sys
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from controller_client.exceptions import UpdateError
from controller_client.protocol import UpdateRunnerPayload
from controller_client.update_layout import PACKAGE_DIR_NAME, write_managed_marker

MAX_ENTRIES: Final[int] = 1000
MAX_TOTAL_UNCOMPRESSED_BYTES: Final[int] = 32 * 1024 * 1024
MAX_FILE_BYTES: Final[int] = 8 * 1024 * 1024
# Never shipped in a bundle; the runner carries .env over itself.
_FORBIDDEN_PARTS: Final[frozenset[str]] = frozenset({".env", ".venv", "logs"})
_REQUIRED_FILES: Final[tuple[str, ...]] = (
    f"{PACKAGE_DIR_NAME}/protocol.py",
    f"{PACKAGE_DIR_NAME}/main.py",
    f"{PACKAGE_DIR_NAME}/requirements.txt",
)
_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"""^CLIENT_VERSION(?:\s*:\s*[^=\n]+)?\s*=\s*["']([^"'\n]+)["']""",
    re.MULTILINE,
)
_COPY_CHUNK: Final[int] = 64 * 1024
# Windows device names: "CON", "nul.txt" and so on open the device instead of
# a file, whatever the extension and case.
_WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


@dataclass(frozen=True)
class ExtractedBundle:
    # The new install root (holds controller_client/).
    root: Path
    package_dir: Path
    version: str
    file_count: int


@dataclass(frozen=True)
class _Entry:
    info: zipfile.ZipInfo
    parts: tuple[str, ...]
    executable: bool


def verify_and_extract(
    payload: UpdateRunnerPayload, dest_root: Path
) -> ExtractedBundle:
    """Check the bundle and extract it into ``dest_root``, which must not exist.

    Raises UpdateError on any problem. If extraction fails part way, the folder
    it created is removed again, so on error nothing of it is left behind.
    """
    raw = _decode(payload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise UpdateError(f"The update bundle is not a valid ZIP file: {e}") from e
    try:
        with archive:
            return _verify_and_write(archive, payload, dest_root)
    except (OSError, zipfile.BadZipFile, zlib.error, EOFError) as e:
        raise UpdateError(f"Could not extract the update bundle: {e}") from e


def _verify_and_write(
    archive: zipfile.ZipFile, payload: UpdateRunnerPayload, dest_root: Path
) -> ExtractedBundle:
    entries = _checked_entries(archive)
    version = _bundled_version(archive)
    if version != payload.target_version:
        raise UpdateError(
            f"The bundle contains version {version}, expected "
            f"{payload.target_version}"
        )
    try:
        dest_root.mkdir(exist_ok=False)
    except OSError as e:
        raise UpdateError(f"Could not create {dest_root}: {e}") from e
    try:
        count = _extract(archive, entries, dest_root)
        write_managed_marker(dest_root, version)
    except BaseException:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise
    return ExtractedBundle(
        root=dest_root,
        package_dir=dest_root / PACKAGE_DIR_NAME,
        version=version,
        file_count=count,
    )


def _decode(payload: UpdateRunnerPayload) -> bytes:
    try:
        raw = base64.b64decode(payload.archive_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise UpdateError(f"The update bundle is not valid base64: {e}") from e
    if len(raw) != payload.archive_size:
        raise UpdateError(
            f"The update bundle has {len(raw)} bytes, expected {payload.archive_size}"
        )
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, payload.archive_sha256):
        raise UpdateError("The update bundle's sha256 does not match")
    return raw


def _checked_entries(archive: zipfile.ZipFile) -> list[_Entry]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise UpdateError(f"The update bundle has more than {MAX_ENTRIES} entries")
    entries: list[_Entry] = []
    names: set[str] = set()
    total = 0
    for info in infos:
        parts = _safe_parts(info)
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode):
            raise UpdateError(f"The update bundle contains a link: {info.filename!r}")
        if info.is_dir():
            if file_type not in (0, stat.S_IFDIR):
                raise UpdateError(f"Unexpected entry type: {info.filename!r}")
            continue
        if file_type not in (0, stat.S_IFREG):
            raise UpdateError(f"Unexpected entry type: {info.filename!r}")
        if info.file_size > MAX_FILE_BYTES:
            raise UpdateError(f"File too large in the bundle: {info.filename!r}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise UpdateError("The update bundle expands to more than 32 MiB")
        # Case-insensitive file systems would merge these.
        key = "/".join(parts).casefold()
        if key in names:
            raise UpdateError(f"Duplicate entry in the bundle: {info.filename!r}")
        names.add(key)
        entries.append(
            _Entry(info=info, parts=parts, executable=bool(mode & stat.S_IXUSR))
        )
    for required in _REQUIRED_FILES:
        if required.casefold() not in names:
            raise UpdateError(f"The update bundle has no {required}")
    return entries


def _safe_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """The entry's path parts; raises UpdateError for anything that could escape."""
    for name in (info.orig_filename, info.filename):
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or ":" in name
            or name.startswith("/")
        ):
            raise UpdateError(f"Unsafe path in the update bundle: {name!r}")
    path = PurePosixPath(info.filename)
    raw_parts = info.filename.rstrip("/").split("/")
    if path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts):
        raise UpdateError(f"Unsafe path in the update bundle: {info.filename!r}")
    if raw_parts[0] != PACKAGE_DIR_NAME:
        raise UpdateError(
            f"Entry outside {PACKAGE_DIR_NAME}/ in the bundle: {info.filename!r}"
        )
    if any(part in _FORBIDDEN_PARTS for part in raw_parts):
        raise UpdateError(f"Forbidden entry in the update bundle: {info.filename!r}")
    if any(_is_windows_unsafe(part) for part in raw_parts):
        raise UpdateError(
            f"Path not allowed on Windows in the update bundle: {info.filename!r}"
        )
    return tuple(raw_parts)


def _is_windows_unsafe(part: str) -> bool:
    """A reserved device name, or a name Windows silently trims (dot / space)."""
    if part.endswith((".", " ")):
        return True
    stem = part.split(".", 1)[0].rstrip(" ")
    return stem.upper() in _WINDOWS_RESERVED_NAMES


def _bundled_version(archive: zipfile.ZipFile) -> str:
    source = archive.read(f"{PACKAGE_DIR_NAME}/protocol.py")
    match = _VERSION_RE.search(source.decode("utf-8", errors="replace"))
    if match is None:
        raise UpdateError("The bundled protocol.py has no CLIENT_VERSION")
    return match.group(1)


def _extract(archive: zipfile.ZipFile, entries: list[_Entry], dest_root: Path) -> int:
    root = dest_root.resolve()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if sys.platform == "win32":
        flags |= os.O_BINARY
    for entry in entries:
        target = root.joinpath(*entry.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Defence in depth: the parts were validated, but a directory created
        # above must still resolve inside the new root.
        if not target.parent.resolve().is_relative_to(root):
            raise UpdateError(f"Entry escapes the new folder: {entry.info.filename!r}")
        mode = 0o755 if entry.executable else 0o644
        fd = os.open(target, flags, mode)
        written = 0
        with os.fdopen(fd, "wb") as out, archive.open(entry.info) as source:
            while chunk := source.read(_COPY_CHUNK):
                written += len(chunk)
                if written > entry.info.file_size:
                    raise UpdateError(
                        f"Entry larger than declared: {entry.info.filename!r}"
                    )
                out.write(chunk)
    return len(entries)
