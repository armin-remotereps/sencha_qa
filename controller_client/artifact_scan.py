"""Read-only file system inventory for ``scan_directory`` and ``inspect_paths``.

Stdlib only. Nothing here reads file contents, follows a link while walking or
runs a shell: ``scan_directory`` walks one absolute directory breadth first
with ``os.scandir`` (``follow_symlinks=False``), never entering symlinks,
Windows junctions or other reparse points, within a depth, entry and time
budget; ``inspect_paths`` only stats the paths it is given.
"""

from __future__ import annotations

import os
import stat
import time
from collections import deque
from collections.abc import Callable
from typing import Final

from controller_client.protocol import (
    SCAN_DIRECTORY_TIME_LIMIT_SECONDS,
    SCAN_ENTRY_PATH_MAX_CHARS,
    InspectPathsPayload,
    PathInfoPayload,
    ScanDirectoryPayload,
    ScanDirectoryResultPayload,
    ScanEntryPayload,
)

# Windows FILE_ATTRIBUTE_REPARSE_POINT; typeshed declares ``stat``'s copy only
# for win32, and the value is fixed by the Win32 API.
_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x400


def scan_directory(
    payload: ScanDirectoryPayload,
    *,
    time_limit: float = SCAN_DIRECTORY_TIME_LIMIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ScanDirectoryResultPayload:
    """List ``payload.path`` down to ``max_depth`` levels, at most ``max_entries``.

    A refused or unreadable root comes back with ``error`` set and no entries.
    """
    root = os.path.expanduser(payload.path)
    if not os.path.isabs(root):
        return _refused(payload.path, "The path must be absolute.")
    root = os.path.normpath(root)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return _refused(root, "The path does not exist.")
    except PermissionError:
        return _refused(root, "Permission denied.")
    except OSError as e:
        return _refused(root, f"Cannot read the path: {e.strerror or e}.")
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        return _refused(
            root,
            "The path is a symbolic link or junction; scan its target "
            f"instead: {os.path.realpath(root)}",
        )
    if not stat.S_ISDIR(info.st_mode):
        return _refused(root, "The path is not a directory.")
    return _walk(root, payload.max_depth, payload.max_entries, time_limit, clock)


def inspect_paths(payload: InspectPathsPayload) -> tuple[PathInfoPayload, ...]:
    """Stat each path (``~`` expanded); missing, relative or unreadable = absent."""
    return tuple(_inspect(path) for path in payload.paths)


def _walk(
    root: str,
    max_depth: int,
    max_entries: int,
    time_limit: float,
    clock: Callable[[], float],
) -> ScanDirectoryResultPayload:
    deadline = clock() + time_limit
    entries: list[ScanEntryPayload] = []
    permission_errors = 0
    truncated = False
    # (absolute directory, its path relative to root ("" for root), its depth)
    pending: deque[tuple[str, str, int]] = deque([(root, "", 0)])
    while pending and not truncated:
        if clock() > deadline:
            truncated = True
            break
        directory, relative, depth = pending.popleft()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if clock() > deadline or len(entries) >= max_entries:
                        truncated = True
                        break
                    scanned = _scan_entry(entry, relative)
                    if scanned is None:
                        continue
                    if isinstance(scanned, PermissionError):
                        permission_errors += 1
                        continue
                    row, descend = scanned
                    entries.append(row)
                    if descend and depth + 1 < max_depth:
                        pending.append((entry.path, row.p, depth + 1))
        except PermissionError:
            if not relative:
                return _refused(root, "Permission denied.")
            permission_errors += 1
        except OSError:
            # Vanished or unreadable mid-scan; the root itself was checked.
            if not relative:
                return _refused(root, "Cannot read the directory.")
    return ScanDirectoryResultPayload(
        root=root,
        entries=tuple(entries),
        truncated=truncated,
        permission_errors=permission_errors,
        error="",
    )


def _scan_entry(
    entry: os.DirEntry[str], parent: str
) -> tuple[ScanEntryPayload, bool] | PermissionError | None:
    """The row for one entry and whether to descend into it.

    None skips the entry: a symlink or junction (never listed nor entered), a
    name that cannot be reported as a "/" relative path, or one that vanished.
    """
    name = entry.name
    if "/" in name or (os.sep != "\\" and "\\" in name):
        return None
    relative = f"{parent}/{name}" if parent else name
    if len(relative) > SCAN_ENTRY_PATH_MAX_CHARS:
        return None
    try:
        if entry.is_symlink() or entry.is_junction():
            return None
        info = entry.stat(follow_symlinks=False)
    except PermissionError as e:
        return e
    except OSError:
        return None
    is_dir = stat.S_ISDIR(info.st_mode)
    row = ScanEntryPayload(
        p=relative,
        d=is_dir,
        s=0 if is_dir else max(int(info.st_size), 0),
        m=round(info.st_mtime, 3),
    )
    # Other reparse points (cloud placeholders, mount points) are listed but
    # never entered.
    return row, is_dir and not _is_reparse_point(info)


def _is_reparse_point(info: os.stat_result) -> bool:
    # st_file_attributes exists only on Windows.
    attributes: int = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _inspect(path: str) -> PathInfoPayload:
    absent = PathInfoPayload(path=path, exists=False, is_dir=False, size=0, mtime=0.0)
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return absent
    try:
        info = os.stat(expanded)
    except (OSError, ValueError):
        return absent
    is_dir = stat.S_ISDIR(info.st_mode)
    return PathInfoPayload(
        path=path,
        exists=True,
        is_dir=is_dir,
        size=0 if is_dir else max(int(info.st_size), 0),
        mtime=round(info.st_mtime, 3),
    )


def _refused(root: str, error: str) -> ScanDirectoryResultPayload:
    return ScanDirectoryResultPayload(
        root=root, entries=(), truncated=False, permission_errors=0, error=error
    )
