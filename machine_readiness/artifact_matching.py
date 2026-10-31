"""Match profile items against a base directory inventory (pure, no LLM).

Versions come from one fixed semver-like heuristic (plan Q10): no regex is
ever built from user or model input, so there is no ReDoS surface.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from fnmatch import fnmatchcase

from machine_readiness.models import LATEST_VERSION
from machine_readiness.types import (
    ArtifactMatch,
    InventoryEntry,
    MatchStatus,
    ProfileItemData,
    Version,
)

# 1.2 / 1.2.3 / 1.2.3.4, optionally followed by a build suffix such as
# -b45, +build7, _r3 or .12. Components are capped at 9 digits so int()
# stays cheap, and a candidate never starts or ends inside a digit run.
_VERSION_RE = re.compile(
    r"(?<![\d.])(\d{1,9}(?:\.\d{1,9}){1,3})"
    r"(?:[-+._]?(?:build|b|r)?(\d{1,9}))?(?![\d])",
    re.IGNORECASE,
)
# An expected version may also be a bare major ("17").
_EXPECTED_RE = re.compile(r"(?<!\d)(\d{1,9}(?:\.\d{1,9}){0,4})(?!\d)")
# File names longer than this are cut before the version scan.
_NAME_SCAN_MAX_CHARS = 512
# Paths kept per match, for display.
MATCH_MAX_PATHS = 10


def extract_versions(file_name: str) -> tuple[Version, ...]:
    """Version candidates in ``file_name``, left to right.

    "app-1.2.3-b45.msi" -> ((1, 2, 3, 45),); "jdk-17.0.2_x64.exe" ->
    ((17, 0, 2),). A dotted run is required, so "x64" yields nothing.
    """
    found: list[Version] = []
    for match in _VERSION_RE.finditer(file_name[:_NAME_SCAN_MAX_CHARS]):
        parts = [int(part) for part in match.group(1).split(".")]
        if match.group(2) is not None:
            parts.append(int(match.group(2)))
        found.append(tuple(parts))
    return tuple(found)


def parse_expected_version(text: str) -> Version | None:
    """The first version in an expected-version text ("17", "1.3", "v2.0.1")."""
    match = _EXPECTED_RE.search(text[:_NAME_SCAN_MAX_CHARS])
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_satisfies(candidate: Version, expected: Version) -> bool:
    """True when ``expected`` is a component-wise prefix of ``candidate``.

    Missing components of ``candidate`` count as 0: "1.3" accepts 1.3, 1.3.0
    and 1.3.7; "1.3.0" accepts 1.3; "17" accepts 17.0.2; "1.3" refuses 1.2.3
    and "1.0" refuses 1.5.
    """
    padded = candidate + (0,) * max(len(expected) - len(candidate), 0)
    return padded[: len(expected)] == expected


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def match_inventory(
    items: Sequence[ProfileItemData],
    entries: Sequence[InventoryEntry],
    *,
    base_dir: str = "",
    truncated: bool = False,
    case_sensitive: bool = False,
) -> list[ArtifactMatch]:
    """One ``ArtifactMatch`` per item, in item order.

    Patterns are ``fnmatch`` globs on the entries' relative "/" paths (a "\\"
    in a pattern counts as "/"). An absolute pattern is matched only when it
    lies under ``base_dir``. Statuses:

    - no match: MISSING, or UNCHECKED when the scan was ``truncated``;
    - expected "latest": FOUND with the highest candidate, never a mismatch;
    - an expected version: FOUND when any matching file name has a candidate
      that satisfies it (``version_satisfies``), else VERSION_MISMATCH;
      AMBIGUOUS when the expected text holds no version and several match;
    - no expected version: FOUND for one match, AMBIGUOUS for several.

    Case-insensitive by default (Windows and macOS file systems).
    """
    paths = [
        (entry.p if case_sensitive else entry.p.lower(), entry) for entry in entries
    ]
    base = _normalise(base_dir, case_sensitive).rstrip("/")
    return [_match_item(item, paths, base, truncated, case_sensitive) for item in items]


def _match_item(
    item: ProfileItemData,
    paths: list[tuple[str, InventoryEntry]],
    base: str,
    truncated: bool,
    case_sensitive: bool,
) -> ArtifactMatch:
    expected_text = item.expected_version.strip()
    pattern = _relative_pattern(item.path_pattern, base, case_sensitive)
    if pattern is None:
        return ArtifactMatch(
            item_key=item.key,
            status=MatchStatus.UNCHECKED,
            expected_version=expected_text,
            detail=(
                "No path pattern."
                if not item.path_pattern.strip()
                else "The path is outside the base directory."
            ),
        )
    matched = sorted(entry.p for path, entry in paths if fnmatchcase(path, pattern))
    if not matched:
        return ArtifactMatch(
            item_key=item.key,
            status=MatchStatus.UNCHECKED if truncated else MatchStatus.MISSING,
            expected_version=expected_text,
            detail=(
                "The scan was cut short; not found in the part scanned."
                if truncated
                else ""
            ),
        )
    per_path = {path: extract_versions(posixpath.basename(path)) for path in matched}
    candidates = tuple(sorted({v for versions in per_path.values() for v in versions}))
    shown = tuple(matched[:MATCH_MAX_PATHS])
    if expected_text.lower() == LATEST_VERSION:
        return ArtifactMatch(
            item_key=item.key,
            status=MatchStatus.FOUND,
            paths=shown,
            candidates=candidates,
            found_version=candidates[-1] if candidates else None,
            expected_version=expected_text,
        )
    expected = parse_expected_version(expected_text) if expected_text else None
    if expected is None:
        several = len(matched) > 1
        return ArtifactMatch(
            item_key=item.key,
            status=MatchStatus.AMBIGUOUS if several else MatchStatus.FOUND,
            paths=shown,
            candidates=candidates,
            found_version=candidates[-1] if len(candidates) == 1 else None,
            expected_version=expected_text,
            detail=f"{len(matched)} paths match." if several else "",
        )
    satisfying = [
        path
        for path, versions in per_path.items()
        if any(version_satisfies(version, expected) for version in versions)
    ]
    if not satisfying:
        return ArtifactMatch(
            item_key=item.key,
            status=MatchStatus.VERSION_MISMATCH,
            paths=shown,
            candidates=candidates,
            expected_version=expected_text,
            detail=(
                "Versions found: "
                + (", ".join(format_version(v) for v in candidates) or "none")
            ),
        )
    found = max(
        version
        for path in satisfying
        for version in per_path[path]
        if version_satisfies(version, expected)
    )
    return ArtifactMatch(
        item_key=item.key,
        status=MatchStatus.FOUND,
        paths=tuple(satisfying[:MATCH_MAX_PATHS]),
        candidates=candidates,
        found_version=found,
        expected_version=expected_text,
    )


def _relative_pattern(pattern: str, base: str, case_sensitive: bool) -> str | None:
    """The pattern relative to the base directory; None when not matchable."""
    normalised = _normalise(pattern.strip(), case_sensitive)
    if not normalised:
        return None
    if _is_absolute(normalised):
        if not base or not normalised.startswith(base + "/"):
            return None
        normalised = normalised[len(base) + 1 :]
    return normalised.lstrip("/") or None


def _normalise(path: str, case_sensitive: bool) -> str:
    path = path.replace("\\", "/")
    while "//" in path[1:]:
        path = path[0] + path[1:].replace("//", "/")
    return path if case_sensitive else path.lower()


def _is_absolute(path: str) -> bool:
    return (
        path.startswith("/")
        or path.startswith("~/")
        or (len(path) >= 3 and path[1] == ":" and path[2] == "/")
    )
