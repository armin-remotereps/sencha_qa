"""Folder paths of an archive's members as a tree (pure, no ORM)."""

from __future__ import annotations

from collections.abc import Mapping

from knowledge_base.types import KnowledgeFolderEntry


def subfolders(
    counts: Mapping[str, int], folder: str
) -> tuple[KnowledgeFolderEntry, ...]:
    """The immediate subfolders of ``folder``, each with its recursive file count.

    ``counts`` maps every stored folder path to the number of files directly
    in it; ``folder`` is "" for the archive root.
    """
    prefix = f"{folder}/" if folder else ""
    totals: dict[str, int] = {}
    for path, count in counts.items():
        if not path or not path.startswith(prefix) or path == folder:
            continue
        child = path[len(prefix) :].split("/", 1)[0]
        totals[child] = totals.get(child, 0) + count
    return tuple(
        KnowledgeFolderEntry(path=f"{prefix}{name}", name=name, file_count=total)
        for name, total in sorted(totals.items(), key=lambda pair: pair[0].casefold())
    )


def breadcrumbs(folder: str) -> tuple[KnowledgeFolderEntry, ...]:
    """One entry per segment of ``folder``, root first (file counts are 0)."""
    if not folder:
        return ()
    parts = folder.split("/")
    return tuple(
        KnowledgeFolderEntry(path="/".join(parts[: index + 1]), name=part, file_count=0)
        for index, part in enumerate(parts)
    )


def ancestor_folders(folder: str) -> tuple[str, ...]:
    """``folder`` and every ancestor, nearest first, ending with the root "".

    ``"a/b/c"`` gives ``("a/b/c", "a/b", "a", "")``.
    """
    if not folder:
        return ("",)
    parts = folder.split("/")
    return (
        *("/".join(parts[:length]) for length in range(len(parts), 0, -1)),
        "",
    )


def folder_exists(counts: Mapping[str, int], folder: str) -> bool:
    """True for the root, a stored folder, or an ancestor of a stored folder."""
    if not folder:
        return True
    prefix = f"{folder}/"
    return any(path == folder or path.startswith(prefix) for path in counts)
