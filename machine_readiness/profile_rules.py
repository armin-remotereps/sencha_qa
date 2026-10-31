"""Rules for an artifacts profile write (pure: no database access).

``validate_profile`` raises ``ArtifactProfileInvalidError`` listing every
problem; forms that want per-field messages call ``validate_base_dir`` and
``item_errors`` directly. Knowledge base ids are checked against the project by
the service (``resolve_knowledge_refs``), not here.
"""

from __future__ import annotations

import re

from django.conf import settings

from machine_readiness.exceptions import ArtifactProfileInvalidError
from machine_readiness.keys import key_error
from machine_readiness.models import (
    BASE_DIR_MAX_LENGTH,
    INSTALL_HINT_MAX_LENGTH,
    ITEM_EXPECTED_VERSION_MAX_LENGTH,
    ITEM_NAME_MAX_LENGTH,
    ITEM_PATH_PATTERN_MAX_LENGTH,
    ITEM_SOURCE_MAX_LENGTH,
    PROFILE_CHANGE_SUMMARY_MAX_LENGTH,
    PROFILE_MAX_ITEMS,
    PROFILE_NOTES_MAX_LENGTH,
    ArtifactKind,
    ArtifactOs,
)
from machine_readiness.profile_text import KB_NAME_MAX_CHARS, render_profile_body
from machine_readiness.types import ProfileData, ProfileItemData

# Runner system info "os" values (agents.os_shell.get_os_name) per profile OS.
_RUNNER_OS: dict[str, ArtifactOs] = {
    "windows": ArtifactOs.WINDOWS,
    "darwin": ArtifactOs.MACOS,
    "linux": ArtifactOs.LINUX,
}
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
# \\server\share with both parts non-empty.
_WINDOWS_UNC = re.compile(r"\\\\[^\\/]+[\\/][^\\/]+")
# The Test Runner empties this before every case (controller_client cleanup).
_DOWNLOADS = "downloads"


def artifact_os_for(runner_os_name: str) -> ArtifactOs | None:
    """The profile OS of a runner's system info name ("Windows", "Darwin", ...)."""
    return _RUNNER_OS.get(runner_os_name.strip().lower())


def validate_base_dir(os_name: ArtifactOs, path: str) -> str | None:
    """Why ``path`` cannot be ``os_name``'s base directory, or None when it can.

    Blank means "not set" and is valid. Windows takes ``X:\\…`` or
    ``\\\\server\\share\\…``; macOS and Linux take ``/…`` or ``~/…``. Refused
    everywhere: NUL or line breaks, ``..`` segments, more than 500 characters,
    and the Downloads folder or anything under it (the Test Runner empties it).
    """
    if not path:
        return None
    if len(path) > BASE_DIR_MAX_LENGTH:
        return f"Use at most {BASE_DIR_MAX_LENGTH} characters."
    if any(char in path for char in ("\x00", "\n", "\r")):
        return "The path must be on one line."
    if path != path.strip():
        return "Remove the spaces at the start or end of the path."
    if os_name == ArtifactOs.WINDOWS:
        if not (_WINDOWS_DRIVE.match(path) or _WINDOWS_UNC.match(path)):
            return r"Use an absolute Windows path, such as D:\qa or \\server\share\qa."
        segments = re.split(r"[\\/]+", path)
    else:
        if not (path.startswith("/") or path.startswith("~/")):
            return "Use an absolute path, such as /opt/qa or ~/qa."
        segments = path.split("/")
    if ".." in segments:
        return 'The path must not contain ".." segments.'
    if _is_downloads(os_name, [segment for segment in segments if segment]):
        return (
            "The Downloads folder is emptied by the Test Runner before every "
            "case; choose another folder."
        )
    return None


def item_errors(item: ProfileItemData, *, position: int) -> list[str]:
    """Problems of one item (``position`` is 1-based, for the messages)."""
    label = f"Artifact {position}" + (f" ({item.key})" if item.key else "")
    errors: list[str] = []
    problem = key_error(item.key)
    if problem:
        errors.append(f"{label}: {problem}")
    if not item.name.strip():
        errors.append(f"{label}: a name is required.")
    if item.kind not in ArtifactKind.values:
        errors.append(f"{label}: unknown kind «{item.kind}».")
    for field_label, value, limit in (
        ("name", item.name, ITEM_NAME_MAX_LENGTH),
        ("path", item.path_pattern, ITEM_PATH_PATTERN_MAX_LENGTH),
        ("expected version", item.expected_version, ITEM_EXPECTED_VERSION_MAX_LENGTH),
        ("source", item.source, ITEM_SOURCE_MAX_LENGTH),
        ("install hint", item.install_hint, INSTALL_HINT_MAX_LENGTH),
    ):
        if len(value) > limit:
            errors.append(
                f"{label}: the {field_label} has more than {limit} characters."
            )
    for field_label, value in (
        ("name", item.name),
        ("path", item.path_pattern),
        ("expected version", item.expected_version),
        ("source", item.source),
    ):
        if any(char in value for char in ("\x00", "\n", "\r")):
            errors.append(f"{label}: the {field_label} must be on one line.")
    if "\x00" in item.install_hint:
        errors.append(f"{label}: the install hint contains a NUL character.")
    pattern_problem = glob_error(item.path_pattern)
    if pattern_problem:
        errors.append(f"{label}: {pattern_problem}")
    if item.knowledge_item_id is not None and item.knowledge_item_id < 1:
        errors.append(f"{label}: the knowledge base item id must be positive.")
    return errors


def glob_error(pattern: str) -> str | None:
    """Why ``pattern`` is not a usable path glob, or None (blank is fine).

    ``fnmatch`` accepts any string, so this only refuses what can never match
    or would leave the base directory: unclosed ``[``, ``..`` segments.
    """
    if not pattern:
        return None
    if ".." in re.split(r"[\\/]+", pattern):
        return 'the path must not contain ".." segments.'
    index = 0
    while index < len(pattern):
        if pattern[index] == "[":
            close = pattern.find("]", index + 2)
            if close == -1:
                return "the path has a «[» without a closing «]»."
            index = close
        index += 1
    return None


def profile_errors(
    data: ProfileData, *, change_summary: str = "", max_chars: int | None = None
) -> list[str]:
    """Every problem of a profile write; empty when it may be stored.

    ``max_chars`` defaults to ``settings.ARTIFACT_PROFILE_MAX_CHARS`` and caps
    the rendered body, measured with every cited KB item at its longest name,
    so the planner always gets the whole profile (plan D7).
    """
    errors: list[str] = []
    for os_name, path in (
        (ArtifactOs.WINDOWS, data.base_dir_windows),
        (ArtifactOs.MACOS, data.base_dir_macos),
        (ArtifactOs.LINUX, data.base_dir_linux),
    ):
        problem = validate_base_dir(os_name, path)
        if problem:
            errors.append(f"{ArtifactOs(os_name).label} base directory: {problem}")
    if len(data.notes) > PROFILE_NOTES_MAX_LENGTH:
        errors.append(f"Notes have more than {PROFILE_NOTES_MAX_LENGTH} characters.")
    if "\x00" in data.notes:
        errors.append("Notes contain a NUL character.")
    if len(change_summary) > PROFILE_CHANGE_SUMMARY_MAX_LENGTH:
        errors.append(
            "The change summary has more than "
            f"{PROFILE_CHANGE_SUMMARY_MAX_LENGTH} characters."
        )
    if len(data.items) > PROFILE_MAX_ITEMS:
        errors.append(f"A profile holds at most {PROFILE_MAX_ITEMS} artifacts.")
    seen: set[str] = set()
    for position, item in enumerate(data.items, start=1):
        errors += item_errors(item, position=position)
        if item.key in seen:
            errors.append(f"Artifact {position}: key «{item.key}» is used twice.")
        seen.add(item.key)
    if not errors:
        limit = settings.ARTIFACT_PROFILE_MAX_CHARS if max_chars is None else max_chars
        size = len(
            render_profile_body(
                data,
                kb_names={
                    item_id: "x" * KB_NAME_MAX_CHARS
                    for item_id in data.knowledge_item_ids()
                },
            )
        )
        if size > limit:
            errors.append(
                f"The profile is {size} characters long; the limit is {limit}. "
                "Shorten the notes, hints or sources."
            )
    return errors


def validate_profile(
    data: ProfileData, *, change_summary: str = "", max_chars: int | None = None
) -> None:
    """Raise ``ArtifactProfileInvalidError`` with ``profile_errors`` if any."""
    errors = profile_errors(data, change_summary=change_summary, max_chars=max_chars)
    if errors:
        raise ArtifactProfileInvalidError(errors)


def _is_downloads(os_name: ArtifactOs, segments: list[str]) -> bool:
    """The Downloads folder of the runner's user (or any user), or below it."""
    lowered = [segment.lower() for segment in segments]
    if os_name == ArtifactOs.WINDOWS:
        # C:\Users\<name>\Downloads...
        return (
            len(lowered) >= 4
            and _WINDOWS_DRIVE.match(segments[0] + "\\") is not None
            and lowered[1] == "users"
            and lowered[3] == _DOWNLOADS
        )
    if lowered[:2] == ["~", _DOWNLOADS]:
        return True
    # /Users/<name>/Downloads (macOS), /home/<name>/Downloads, /root/Downloads.
    if (
        len(lowered) >= 3
        and lowered[0] in ("users", "home")
        and lowered[2] == _DOWNLOADS
    ):
        return True
    return lowered[:2] == ["root", _DOWNLOADS]
