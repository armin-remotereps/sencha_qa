"""Plain data shared by the machine readiness services and pure modules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from machine_readiness.models import ArtifactOs

# A version as integer components, e.g. (17, 0, 2); compared as tuples.
Version = tuple[int, ...]


@dataclass(frozen=True)
class ProfileItemData:
    """One artifact of a profile, as written by a form or the agent."""

    key: str
    name: str
    # machine_readiness.models.ArtifactKind value.
    kind: str
    path_pattern: str = ""
    expected_version: str = ""
    source: str = ""
    install_hint: str = ""
    knowledge_item_id: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "path_pattern": self.path_pattern,
            "expected_version": self.expected_version,
            "source": self.source,
            "install_hint": self.install_hint,
            "knowledge_item_id": self.knowledge_item_id,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object]) -> ProfileItemData:
        """Lenient: missing or mistyped fields become blank (snapshots only)."""
        kb_id = raw.get("knowledge_item_id")
        return cls(
            key=_text(raw.get("key")),
            name=_text(raw.get("name")),
            kind=_text(raw.get("kind")),
            path_pattern=_text(raw.get("path_pattern")),
            expected_version=_text(raw.get("expected_version")),
            source=_text(raw.get("source")),
            install_hint=_text(raw.get("install_hint")),
            knowledge_item_id=(
                kb_id if isinstance(kb_id, int) and not isinstance(kb_id, bool) else None
            ),
        )


@dataclass(frozen=True)
class ProfileData:
    """The writable content of an artifacts profile (one base dir per OS)."""

    base_dir_windows: str = ""
    base_dir_macos: str = ""
    base_dir_linux: str = ""
    notes: str = ""
    items: tuple[ProfileItemData, ...] = ()

    def base_dir_for(self, os_name: ArtifactOs) -> str:
        if os_name == ArtifactOs.WINDOWS:
            return self.base_dir_windows
        if os_name == ArtifactOs.MACOS:
            return self.base_dir_macos
        return self.base_dir_linux

    @property
    def is_empty(self) -> bool:
        return not (
            self.base_dir_windows.strip()
            or self.base_dir_macos.strip()
            or self.base_dir_linux.strip()
            or self.notes.strip()
            or self.items
        )

    def knowledge_item_ids(self) -> list[int]:
        """Cited knowledge base item ids, in item order, without duplicates."""
        return list(
            dict.fromkeys(
                item.knowledge_item_id
                for item in self.items
                if item.knowledge_item_id is not None
            )
        )

    def to_json(self) -> dict[str, object]:
        """The ``ArtifactProfileVersion.snapshot`` shape."""
        return {
            "base_dir_windows": self.base_dir_windows,
            "base_dir_macos": self.base_dir_macos,
            "base_dir_linux": self.base_dir_linux,
            "notes": self.notes,
            "items": [item.to_json() for item in self.items],
        }

    @classmethod
    def from_json(cls, raw: object) -> ProfileData:
        """Lenient reader of ``to_json`` output; re-validate before writing."""
        if not isinstance(raw, Mapping):
            return cls()
        items = raw.get("items")
        return cls(
            base_dir_windows=_text(raw.get("base_dir_windows")),
            base_dir_macos=_text(raw.get("base_dir_macos")),
            base_dir_linux=_text(raw.get("base_dir_linux")),
            notes=_text(raw.get("notes")),
            items=tuple(
                ProfileItemData.from_json(item)
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class ProfileSnapshot:
    """The current profile with provenance and intake state.

    ``exists`` False means intake is pending (no profile row); then
    ``data`` is empty and ``revision`` 0.
    """

    data: ProfileData
    exists: bool
    revision: int = 0
    # machine_readiness.models.ProfileWriteSource value; "" before any write.
    source: str = ""
    written_by_id: int | None = None
    written_by_name: str = ""
    written_at: datetime | None = None
    change_summary: str = ""
    intake_skipped_at: datetime | None = None
    intake_skipped_by_name: str = ""

    @property
    def intake_pending(self) -> bool:
        return not self.exists


@dataclass(frozen=True)
class InventoryEntry:
    """One scanned path, as stored in ``ArtifactInventory.entries``.

    ``p`` relative path with "/" separators, ``d`` is a directory, ``s`` size
    in bytes (0 for directories), ``m`` mtime as epoch seconds.
    """

    p: str
    d: bool
    s: int
    m: float

    @classmethod
    def from_json(cls, raw: object) -> InventoryEntry | None:
        """None for a malformed row (stored JSON is runner-written data)."""
        if not isinstance(raw, Mapping):
            return None
        path, size, mtime = raw.get("p"), raw.get("s", 0), raw.get("m", 0)
        if not isinstance(path, str) or not path:
            return None
        return cls(
            p=path.replace("\\", "/"),
            d=bool(raw.get("d", False)),
            s=size if isinstance(size, int) and not isinstance(size, bool) else 0,
            m=float(mtime) if isinstance(mtime, int | float) else 0.0,
        )

    def to_json(self) -> dict[str, object]:
        return {"p": self.p, "d": self.d, "s": self.s, "m": self.m}


class MatchStatus(StrEnum):
    """How one profile item compares with an inventory (not stored)."""

    FOUND = "found"
    MISSING = "missing"
    VERSION_MISMATCH = "version_mismatch"
    AMBIGUOUS = "ambiguous"
    # Not decidable from the inventory: no path pattern, an absolute pattern
    # outside the base directory, or nothing matched a truncated scan.
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class ArtifactMatch:
    """One profile item matched against an inventory."""

    item_key: str
    status: MatchStatus
    # Matching inventory paths (relative), sorted, at most MATCH_MAX_PATHS.
    paths: tuple[str, ...] = ()
    # Every version candidate found in the matching file names, sorted.
    candidates: tuple[Version, ...] = ()
    # The version reported as found: the one equal to the expected, or the
    # highest candidate for "latest"; None when there is none.
    found_version: Version | None = None
    expected_version: str = ""
    # Why the status is what it is, for UNCHECKED and AMBIGUOUS.
    detail: str = ""


@dataclass(frozen=True)
class Stage0RequirementData:
    """One validated requirement from a ``record_stage0`` call."""

    key: str
    title: str
    # machine_readiness.models.RequirementKind value.
    kind: str
    check_target: str = ""
    install_hint: str = ""
    conflicts_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stage0TextResult:
    """The analysis of one precondition text of a batch."""

    # The ref the prompt gave the text (e.g. "T3").
    ref: str
    requirement_keys: tuple[str, ...] = ()
    none_needed: bool = False
    unclear: str = ""


@dataclass(frozen=True)
class Stage0BatchResult:
    """A validated ``record_stage0`` payload.

    ``requirements`` holds new or updated definitions (catalog keys the
    model only cited are not repeated); ``texts`` only the refs it covered,
    so the rest stay unanalysed; ``problems`` the entries dropped and why.
    """

    requirements: tuple[Stage0RequirementData, ...] = ()
    texts: tuple[Stage0TextResult, ...] = ()
    problems: tuple[str, ...] = ()


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
