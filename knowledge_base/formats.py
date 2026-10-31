"""Accepted file extensions and their kinds (pure, no ORM queries)."""

from __future__ import annotations

from knowledge_base.models import KnowledgeKind

# Compound suffixes are matched before single ones.
_COMPOUND_EXTENSIONS: dict[str, KnowledgeKind] = {
    ".tar.gz": KnowledgeKind.ARCHIVE,
}

_EXTENSIONS: dict[str, KnowledgeKind] = {
    **{ext: KnowledgeKind.TEXT for ext in (".txt", ".text", ".log", ".out")},
    **{ext: KnowledgeKind.MARKDOWN for ext in (".md", ".markdown")},
    **{ext: KnowledgeKind.CSV for ext in (".csv", ".tsv")},
    **{
        ext: KnowledgeKind.XML
        for ext in (
            ".xml",
            ".xsd",
            ".xsl",
            ".xslt",
            ".config",
            ".csproj",
            ".props",
            ".targets",
        )
    },
    **{ext: KnowledgeKind.JSON for ext in (".json", ".jsonl", ".ndjson")},
    **{ext: KnowledgeKind.YAML for ext in (".yaml", ".yml")},
    **{
        ext: KnowledgeKind.CONFIG
        for ext in (".ini", ".cfg", ".conf", ".properties", ".toml")
    },
    **{
        ext: KnowledgeKind.SCRIPT
        for ext in (".sh", ".bash", ".ps1", ".psm1", ".bat", ".cmd", ".sql")
    },
    **{
        ext: KnowledgeKind.SOURCE
        for ext in (
            ".py",
            ".js",
            ".mjs",
            ".cjs",
            ".jsx",
            ".ts",
            ".tsx",
            ".cs",
            ".vb",
            ".java",
            ".kt",
            ".kts",
            ".groovy",
            ".gradle",
            ".scala",
            ".go",
            ".rb",
            ".php",
            ".c",
            ".h",
            ".cpp",
            ".cc",
            ".cxx",
            ".hpp",
            ".hh",
            ".m",
            ".swift",
            ".rs",
            ".lua",
            ".pl",
            ".r",
        )
    },
    **{ext: KnowledgeKind.HTML for ext in (".html", ".htm")},
    ".pdf": KnowledgeKind.PDF,
    ".docx": KnowledgeKind.DOCX,
    ".doc": KnowledgeKind.DOC,
    ".xlsx": KnowledgeKind.XLSX,
    ".xlsm": KnowledgeKind.XLSX,
    ".xls": KnowledgeKind.XLS,
    ".zip": KnowledgeKind.ARCHIVE,
    ".tar": KnowledgeKind.ARCHIVE,
    ".tgz": KnowledgeKind.ARCHIVE,
    ".7z": KnowledgeKind.ARCHIVE,
}

# Kinds whose content is decoded as text and cut by line ranges.
TEXT_KINDS: frozenset[str] = frozenset(
    {
        KnowledgeKind.TEXT,
        KnowledgeKind.MARKDOWN,
        KnowledgeKind.CSV,
        KnowledgeKind.XML,
        KnowledgeKind.JSON,
        KnowledgeKind.YAML,
        KnowledgeKind.CONFIG,
        KnowledgeKind.SCRIPT,
        KnowledgeKind.SOURCE,
        KnowledgeKind.HTML,
    }
)

SEVEN_ZIP_EXTENSION = ".7z"
SEVEN_ZIP_DISABLED_MESSAGE = "7z archives are disabled on this server"


def matched_extension(name: str) -> str | None:
    """The accepted extension of ``name`` (lower case), or None."""
    lower = name.lower()
    for extension in _COMPOUND_EXTENSIONS:
        if lower.endswith(extension):
            return extension
    dot = lower.rfind(".")
    if dot < 0:
        return None
    extension = lower[dot:]
    return extension if extension in _EXTENSIONS else None


def kind_for_name(name: str) -> KnowledgeKind | None:
    """The kind of a file by its name, or None when it is not accepted."""
    extension = matched_extension(name)
    if extension is None:
        return None
    return _COMPOUND_EXTENSIONS.get(extension) or _EXTENSIONS[extension]


def archive_format(name: str) -> str | None:
    """ "zip", "tar" or "7z" for an archive name; None otherwise."""
    extension = matched_extension(name)
    if extension == ".zip":
        return "zip"
    if extension in (".tar", ".tar.gz", ".tgz"):
        return "tar"
    if extension == SEVEN_ZIP_EXTENSION:
        return "7z"
    return None


def accepted_extensions(*, accept_7z: bool) -> tuple[str, ...]:
    extensions = [*_COMPOUND_EXTENSIONS, *_EXTENSIONS]
    if not accept_7z:
        extensions.remove(SEVEN_ZIP_EXTENSION)
    return tuple(sorted(extensions))


def accept_attribute(*, accept_7z: bool) -> str:
    """The ``accept=`` value of the upload input."""
    return ",".join(accepted_extensions(accept_7z=accept_7z))
