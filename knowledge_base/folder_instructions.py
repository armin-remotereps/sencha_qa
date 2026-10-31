"""Which archive file is a folder's instructions (pure, no ORM).

A file directly in a folder is a candidate when its name, case-insensitively,
is ``instructions`` or ``readme`` with ``.md``, ``.markdown``, ``.txt`` or no
extension (``INSTRUCTIONS.md``, ``Readme.txt``, ``README``; not
``README.de.md``). One file wins per folder: ``instructions`` before
``readme``, then ``.md``/``.markdown`` before ``.txt`` before none, then the
shorter name, then the name.

The winner applies to its folder and every subfolder of the same archive
(``knowledge_base.services.folder_instructions`` resolves that at read time).
"""

from __future__ import annotations

from collections.abc import Sequence

_STEM_RANK = {"instructions": 0, "readme": 1}
_EXTENSION_RANK = {".md": 0, ".markdown": 0, ".txt": 1, "": 2}


def instruction_rank(file_name: str) -> tuple[int, int, int, str] | None:
    """A sort key (lower wins) when ``file_name`` is a candidate, else None."""
    lowered = file_name.casefold()
    stem, dot, extension = lowered.partition(".")
    suffix = f"{dot}{extension}"
    if stem not in _STEM_RANK or suffix not in _EXTENSION_RANK:
        return None
    return (_STEM_RANK[stem], _EXTENSION_RANK[suffix], len(file_name), file_name)


def pick_instruction_files(members: Sequence[tuple[str, str]]) -> set[int]:
    """Indexes of the one winning instruction file per folder.

    ``members`` holds each file's ``(folder, file_name)``; only files directly
    in a folder compete for it.
    """
    best: dict[str, tuple[tuple[int, int, int, str], int]] = {}
    for index, (folder, file_name) in enumerate(members):
        rank = instruction_rank(file_name)
        if rank is None:
            continue
        current = best.get(folder)
        if current is None or rank < current[0]:
            best[folder] = (rank, index)
    return {index for _, index in best.values()}
