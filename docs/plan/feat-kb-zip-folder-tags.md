# feat: Knowledge base archives keep their folders (folder tags, folder instructions, folder browser, fuller expansion)

Status: Done
Created: 2026-10-31
Request: (verbatim) "On knowledge base, user may upload a .zip with lot of directories in it, system should go through each of them and process alll of the files, the directory instruction is important as well and it's better to be a tag on the kb as well".

Restated: a member uploads one archive holding a deep tree of folders. Every readable file in every folder becomes a knowledge base item. The archive is refused only when it is over a (higher) limit, and the message says why. The folder structure carries meaning, so each file keeps its folder path, and that path is the file's **folder tag**. Instruction files inside a folder (`README`, `INSTRUCTIONS`) are **context for that folder's files**: the agents are pointed to them whenever they read or find a file there, and the pages link to them. Type: `feat`.

## Decisions (answers of 2026-10-31)
- **D1 (Q1): both meanings.**
  - The folder structure is kept as a tag.
  - Instruction files inside folders are context for the files of that folder and its subfolders. Design › Folder instructions has the details.
- **D2 (Q2): the full path is the only folder tag.**
  - `docs/install/windows/setup.md` gets one folder tag, `docs/install/windows`. Nothing is split per segment.
  - Filtering by a folder tag includes its subfolders (a prefix match): this is the planner's call.
  - Search still finds the folder words, because the path is split into words for the search vector.
- **D3 (Q3): an archive over a limit is refused as a whole, with a clear message.** The limits are higher:
  - 2000 readable files;
  - 100 MiB per archive upload;
  - 256 MiB expanded;
  - 20 000 raw entries.

  Only files that will be read count toward the file and expanded-size limits. Metadata (`__MACOSX`, `.DS_Store`), hidden files, unsupported types and directories do not.
- **D4 (Q4):** nested archives are expanded one level deep.
- **D5 (Q5): tags can be edited by hand.**
  - The edit form takes at most 20 manual tags.
  - The upload form takes optional tags, applied to every uploaded file and inherited by the files inside an archive.
  - Later edits of an archive's tags do not propagate to its files.
- **D6 (Q6):** well-known extensionless names are read (a fixed allowlist).
- **D7 (Q7): case.**
  - Manual tags are stored lower-case.
  - The folder tag shows the folder's **original case** and matches **case-insensitively**. It is rendered from the `folder` column (Design › Data model), so no second, lower-cased copy exists.
- **D8 (Q8):** the archive's own name is not a tag.
- This plan supersedes decision D3 of the KB plan ("Description only, no tags").

## Context
What exists today (`docs/changed/2026-10-31-project-knowledge-base.md`, `docs/plan/feat-project-knowledge-base.md`):
- **Expansion already walks every folder.**
  - `knowledge_base/services/expand_knowledge_archive.py` claims the archive and runs `archive_readers.iter_archive_members` (zip, tar(.gz), 7z) under `archive_guard.py`.
  - It stores each member's blob (quota + hourly budget) and creates pending child items (`parent` FK) in one transaction.
  - Members are one level deep.
- **The folder structure is lost.**
  - `name` is the sanitised basename.
  - The path survives only in `original_name`. `normalise_label` caps it at 255 characters by keeping the end, so deep paths lose their root folders.
  - Members are a flat table, and `get_knowledge_item_detail` loads **all** of them.
  - Agents see only the id, basename, kind and pages (`tool_text`, `knowledge_index`).
  - Search vectors (`services/search_vectors.py`) are A: name + description, B: heading, C: text, without the path.
- **Limits that reject the whole archive** and that a big multi-folder zip hits:
  1. `KNOWLEDGE_BASE_MAX_FILE_BYTES` (25 MiB) also caps the archive upload (`upload_knowledge_files._refusal_reason`).
  2. `check_member_count` counts every non-directory entry, including `__MACOSX/._*`, `.DS_Store`, unsupported types and nested archives. A macOS-compressed zip of 300 documents (about 600 entries) is rejected today.
  3. `check_declared_total` (zip, 7z) sums the declared sizes of skipped entries too.
  4. One encrypted zip member rejects the whole zip.
- **Skipped today:** nested archives, hidden parts, `__MACOSX`, unsupported or extensionless names, empty files and members over 25 MiB.
- **Copy:** `copy_project_knowledge.py` copies `_COPIED_FIELDS` and the chunk vectors, one member level deep.
- **Contract consumers outside `knowledge_base`:**
  - `machine_setup/payloads.py` (`knowledge_choice_payload`);
  - `machine_setup/services/{queue_turn,run_turn,chat_page,setup_prompt}.py`;
  - `projects/services.py` (`knowledge_index_text`);
  - `projects/person_step_question_services.py` (`related_knowledge`).

  `KnowledgeItemSummary(...)` is constructed only in `list_knowledge_items._summary`, so new fields with defaults are backwards compatible.
- **Versions:** Django ~5.2.11, psycopg ~3.3.2, Postgres FTS config `english`. `django.contrib.postgres` (`ArrayAgg`, `SearchVector`, `GinIndex`) is already used.

## Goals
- Every readable file in every folder becomes an item, including one level of nested archives. An archive over a limit is refused with a message naming the limit and the count found.
- Each member keeps its full `folder` path, shown as its folder tag. Filtering by a folder tag includes its subfolders. Folder words are searchable.
- Manual tags: given at upload (inherited by archive files) and editable on the item.
- Folder instructions:
  - one instruction file per folder, chosen by an explicit rule;
  - it applies to that folder and all subfolders;
  - agents get the nearest instructions (at most 3) named in every read and search result, plus a short fenced excerpt when they read page 1;
  - the item page and the folder browser link to them.
- Agents see each file's path, folder tag and manual tags, and can filter list and search by tag.
- Archive files are browsed folder by folder.
- Duplicating a project copies folders, tags and instruction flags.
- Existing archive members are backfilled.

## Non-goals
- Uploading a browser folder (`webkitdirectory`): zip it instead.
- Project-wide tag management (rename or merge), tag colours.
- Editing `folder`, or marking or unmarking an instruction file by hand. The rule decides; see Alternatives.
- Partial expansion. An archive over a limit is refused (D3).
- Nesting deeper than one level.
- Sniffing unknown extensions as text.
- Putting instruction text into other files' search vectors.
- Changing the project quota, the hourly budget, the time limit or the WebSocket payload.

## Design

### Data model (`knowledge_base/models.py`)
Migration `knowledge_base.0002_item_folder_tags` (schema, additive, constant defaults, so metadata-only on Postgres ≥ 11).

**`KnowledgeItem.folder`**
- Type: `CharField(max_length=KNOWLEDGE_FOLDER_MAX_LENGTH=1024, blank=True, default="")`.
- Contents: the member's directory inside its archive, with `/` separators and no leading or trailing slash. The guard has already normalised away `.`, `..` and drive parts.
- `""` for top-level uploads and for root members.
- For a nested archive's members, the inner archive's file name is one segment: `logs/build-12.zip/agent`.
- It is **the folder tag**:
  - shown in its original case;
  - matched case-insensitively, with subfolders included;
  - never edited;
  - never stored a second time in `tags`.
- A folder longer than 1024 characters is cut by whole trailing segments.

**`KnowledgeItem.tags`**
- Type: `ArrayField(CharField(max_length=KNOWLEDGE_TAG_MAX_LENGTH=64), blank=True, default=list)`.
- Contents: **manual tags only**, given at upload, inherited from the archive, or edited later.
- Each tag is normalised lower-case. At most `KNOWLEDGE_MAX_TAGS_PER_ITEM = 20`.
- Index: `GinIndex(fields=["tags"], name="kb_item_tags")`.

**`KnowledgeItem.is_folder_instructions`**
- Type: `BooleanField(default=False)`.
- Set at expansion by the rule in Folder instructions.
- Index: partial `models.Index(fields=["parent", "folder"], condition=Q(is_folder_instructions=True), name="kb_item_folder_instr")`.

**Indexes:** both are plain `CREATE INDEX`. The KB went live today and its tables are small, so `AddIndexConcurrently` is not worth a non-atomic migration.

Migration `knowledge_base.0003_backfill_folders` (`RunPython` on historical models; reverse is a no-op). It inlines its rules, since migrations do not import app code that will change. For members (`parent` not null):
- `folder` is the directory part of `original_name`. When `original_name` starts with `…` (it was truncated), the first, partial segment is dropped.
- `is_folder_instructions` follows the rule, computed per (parent, folder) group.
- The members' `KnowledgeChunk.search_vector` is rewritten with the new weighting: one UPDATE per member, with `'english'` hard-coded.

Top-level items keep empty values, and their vectors already equal the new formula.

### Pure modules (no ORM)

**New `knowledge_base/tags.py`**
- `normalise_tag(raw) -> str`:
  - keeps printable characters only, collapses whitespace and strips;
  - drops `,`;
  - `casefold()`s;
  - cuts to 64 characters;
  - returns `""` when nothing is left.
- `parse_tag_input(text) -> tuple[str, ...]`: splits on commas and newlines, normalises and de-duplicates in order. The form checks the count.
- `merge_tags(*groups) -> tuple[str, ...]`: de-duplicates in order and caps at 20.
- `normalise_folder_query(raw) -> str`: a tag query read as a folder path. It strips surrounding slashes and whitespace, keeps the case and returns `""` when empty.
- `split_member_path(path) -> tuple[str, str]`: returns `(folder, file_name)`, with the folder capped at 1024 characters by whole segments.
- `item_path(folder, name) -> str`.
- `search_label_text(folder, tags) -> str`: the folder split on `/`, `\`, `_`, `-` and `.` into space-separated words, plus the tags. Splitting is needed because Postgres's default parser turns `docs/install/windows` into one `file` token, which `english` maps to `simple` as a single lexeme. The implementer confirms this with `ts_debug('english', 'docs/install/windows')`.

**New `knowledge_base/folder_tree.py`**
- `subfolders(counts: Mapping[str, int], folder: str) -> tuple[KnowledgeFolderEntry, ...]`: the immediate subfolders, each with a recursive count.
- `breadcrumbs(folder) -> tuple[KnowledgeFolderEntry, ...]`.
- `ancestor_folders(folder) -> tuple[str, ...]`: nearest first, including the folder itself and the root `""`. For example `a/b/c` gives `("a/b/c", "a/b", "a", "")`.

**New `knowledge_base/folder_instructions.py`** (the rule, pure)
- `instruction_rank(file_name) -> int | None`. A file is a folder instruction candidate when its name, matched case-insensitively, is a stem plus an allowed extension:
  - the stem is `instructions` or `readme`;
  - the extension is `.md`, `.markdown`, `.txt` or none.

  The rank (lower wins) is:
  - `instructions` before `readme`;
  - then `.md`/`.markdown` before `.txt` before no extension;
  - then the shorter name, then the name.

  Examples: `INSTRUCTIONS.md`, `Readme.txt`, `README`. Not `README.de.md`, not `install.md`, and not "an `.md` named like its folder" (too many false positives).
- `pick_instruction_files(members: Sequence[tuple[str, str]]) -> set[int]`:
  - takes the members' (folder, file_name) pairs;
  - returns the indexes of the **one** winner per folder;
  - only files directly in that folder count.

**`knowledge_base/archive_guard.py`**
- `normalise_label` becomes `normalise_member_path(name) -> str`: the same rules, uncapped. `display_label(path) -> str` keeps the end within 255 characters, for `original_name` and skip messages.
- `ArchiveLimits` gains `max_entries`.
  - `check_entry_count(count, noun, limits)` checks the raw entry count (directories excluded). Over it → `ArchiveRejected`.
  - `check_member_count(count, noun, limits)` now receives the **readable** member count.
- New message (D3): "This archive holds {found} readable files; the limit is {max}. Split it into smaller archives." The same shape is used for entries ("…{found} entries…") and for size ("…expands to {size} of readable files; the limit is {max}…").
- `check_declared_total` sums the **accepted** members only (zip, 7z).
  - The per-member ratio, `read_capped`, the running `ArchiveBudget` and py7zr's `max_extract_size` are unchanged and still reject the archive.
  - Tar keeps counting every streamed byte, as today, so a tar is refused on its total stream.
- `skip_reason(path, size, limits, *, allow_archives: bool)`:
  - a nested archive is accepted when `allow_archives` is true;
  - an encrypted zip member is **skipped** ("password protected") instead of rejecting the zip. Encryption is not a limit.

**`knowledge_base/archive_readers.py`**
- `ArchiveMemberRef` changes:
  - `label` is renamed `path` (uncapped);
  - new field `is_archive: bool`.
- **zip:**
  - raw entry cap;
  - accepted members chosen by `skip_reason`;
  - `check_member_count(accepted)` and `check_declared_total(accepted)` run before any read;
  - encrypted members are skipped.
- **tar:** the raw entry cap and the accepted count are checked while streaming. Over either, the whole archive is rejected, as today; no children are created.
- **7z:** the same as zip over `archive.files`. Header encryption still rejects.
- **Nested (D4):** `iter_archive_members(fmt, data, skipped)` holds an `ArchiveAllowance` (remaining readable files and remaining bytes), shared across levels. When the outer reader yields `is_archive=True`:
  1. The inner format comes from the name. 7z disabled → skipped.
  2. The content sniff runs. A failure → skipped, "not a readable archive".
  3. The inner archive is iterated with `allow_archives=False`. Its limits are the outer allowance left; the per-member and entry caps are unchanged.
  4. Every inner path gets the inner archive's path as a prefix.
- **Errors in a nested archive:**
  - A damaged or encrypted inner archive is skipped, and the outer archive goes on.
  - Running past the **shared allowance** rejects the whole outer archive with the D3 message (counting inner files).
  - The inner archive's own bytes are never stored as an item.

**`knowledge_base/formats.py` (D6):** `kind_for_name` falls back to an allowlist of extensionless names, matched case-insensitively:
- `TEXT`: `README`, `INSTRUCTIONS`, `LICENSE`, `CHANGELOG`, `NOTICE`, `AUTHORS`;
- `SCRIPT`: `Makefile`, `Dockerfile`, `Jenkinsfile`, `Vagrantfile`, `Procfile`, `Gemfile`, `Rakefile`.

Hidden files stay skipped. `accept=` is unchanged.

### Folder instructions (D1)
- **Rule:** see `folder_instructions.py` above. At most one instruction file per folder of an archive.
- **Where it is stored:** a flag on the instruction item itself, `is_folder_instructions`, set once at expansion from the original path. A later rename does not unset it.
  - No text is copied into other items, and no link rows exist.
  - Which instructions apply is resolved **at read time** from the archive's members: same `parent_id`, `folder` in the item's ancestor folders, flag set.
  - Deleting the instruction item simply removes it from the results.
  - It stays a normal item: listed, extracted, searchable, readable.
- **Scope:** an instruction applies to its folder and every subfolder. **All ancestors apply**, nearest first, capped at `FOLDER_INSTRUCTIONS_PER_ITEM = 3` (the nearest three).
  - A root `README` usually describes the whole tree, so it should still reach deep files.
  - The nearest one is the most specific, so it comes first.
  - An instruction file does not list itself, but does list its ancestors' instructions.
  - In a nested archive, the inner archive's folders and the outer ancestors chain naturally, because the folder path includes the inner archive segment.
- **Agents:**
  - `read_knowledge(item_id, page)`: after the page header, inside the fence, a block such as `Folder instructions (read these first): id=12 "docs/install/README.md" (Markdown, 1 page); id=3 "README.md" (Markdown, 2 pages)`.
    - **On page 1 only**, it adds an excerpt of the nearest **ready** instruction: the start of its first chunk, cut to `FOLDER_INSTRUCTION_EXCERPT_CHARS = 600` and neutralised.
    - After the fence: "Read them with read_knowledge(item_id=12)".
    - Instructions that are not ready say "not readable yet".
  - `search_knowledge`: below the hits, inside the fence, one `Folder instructions for these hits:` block. It is de-duplicated across hits, capped at `FOLDER_INSTRUCTIONS_PER_RESULT = 5`, and gives names and ids only, no excerpt.
  - `list_knowledge(archive_id=)`: instruction items are marked `[folder instructions for docs/install]`.
  - The index, `related_knowledge` (advisor) and reference notes are unchanged. The advisor has no tools to follow a pointer; instruction files can still appear among its excerpts as ordinary matches.
- **Pages:**
  - The item page's Details card shows "Folder instructions: <links>", nearest first.
  - The folder browser shows the instructions that apply to the current folder above the listing, with the excerpt of the nearest one. Instruction items carry a "Folder instructions" chip in tables.
- **Queries:**
  - **Resolving** instructions takes one query per call, whatever the number of items: `parent_id IN (...) AND is_folder_instructions AND folder IN (union of ancestor folders)`, served by the partial index. It is filtered per item in Python.
  - **The excerpt** takes one more query: the first chunk (`ordinal=0`) of at most one item. It runs only on read page 1 and in the browser.

### Services (`knowledge_base/services/`)

**Write side**
- `search_vectors.write_search_vectors(item_id, *, name, description, folder, tags) -> None`:
  - A: name + description;
  - **B: `search_label_text(folder, tags)` plus the heading**;
  - C: text.

  Folder words weigh like a heading. A large generic folder ("docs") must not push every chunk to the rank of an item *named* "docs".
  - Callers: `extract_knowledge_item._save_chunks` (folder and tags read under the lock) and `edit_knowledge_item`.
- `expand_knowledge_archive.py`. For each child:
  - `name = sanitize_file_name(file_name)`;
  - `original_name = display_label(path)`;
  - `folder` from `split_member_path`;
  - `tags = merge_tags(archive.tags)` (inherited manual tags, read under the claim lock);
  - `is_folder_instructions` from `pick_instruction_files` over the children being created. Quota-skipped children are excluded before picking.

  The `skipped_warning` entries use `display_label`. Limit failures are `ArchiveRejected`, so the existing `fail_claimed_item` path shows the D3 message.
- `upload_knowledge_files(..., tags: Sequence[str] = ())`:
  - stores the tags on every created item;
  - refuses archive kinds over `KNOWLEDGE_BASE_MAX_ARCHIVE_BYTES` and the other kinds over `KNOWLEDGE_BASE_MAX_FILE_BYTES`.
- `edit_knowledge_item(*, project_id, item_id, name, description, tags: Sequence[str]) -> None`: saves the normalised, capped tags, then rewrites the vectors. An archive's tag edit is not propagated (D5).
- `copy_project_knowledge.py`: `"folder"`, `"tags"` and `"is_folder_instructions"` are added to `_COPIED_FIELDS`.

**Read side**
- New `tag_filter.py`: `item_tag_q(tag: str, *, prefix: str = "") -> Q`. A tag query matches when **either**:
  - the manual tags contain `normalise_tag(tag)` (GIN), **or**
  - the folder equals the query or starts with it plus `/`, compared case-insensitively (`folder__iexact` / `folder__istartswith`; Django escapes `%`/`_`).

  `prefix="item__"` is for chunk querysets. This is the one definition of "filter by tag" for the list page, search and the agent tools.
- `list_knowledge_items.py`:
  - `_SUMMARY_FIELDS` gains `folder`, `tags` and `is_folder_instructions`;
  - `_ENTRY_FIELDS` gains `parent__name` (an FK join in the same query);
  - members are ordered by `folder, name, id`;
  - `list_knowledge_items(project_id, *, parent_id=None, tag=None)` and `list_knowledge_entries(..., tag=None)`: with `tag` and no `parent_id`, every level is searched (top-level items and members); with `parent_id`, only that archive;
  - `get_knowledge_item_detail` no longer loads members (`KnowledgeItemDetail.members` is removed; it is KB-internal). For a member it fills `folder_instructions` with one extra query.
- New `list_knowledge_tags.py`: `list_knowledge_tags(project_id, *, limit) -> KnowledgeTagFacet`, in two queries:
  - manual tag counts, as raw parameterised SQL with `CROSS JOIN LATERAL unnest(i.tags)` and the table name from `quote_name` (as in `copy_project_knowledge`). The ORM cannot group by a set-returning function;
  - folder counts: `values("folder").annotate(Count("id"))`, excluding `""`.

  Each is capped at `limit` (300) and ordered by name.
- New `browse_knowledge_archive.py`:
  - `archive_folder_counts(project_id, archive_id) -> dict[str, int]`: one query.
  - `browse_knowledge_archive(project_id, archive_id, *, folder) -> KnowledgeArchiveBrowse`:
    - queries: the archive detail, folder counts, the files whose `folder == folder`, the instruction resolution and at most one excerpt;
    - raises `KnowledgeFolderNotFoundError` when `folder` is neither `""`, a stored folder nor a prefix of one.
- New `folder_instructions.py` (service):
  - `instructions_for_items(project_id, refs: Sequence[KnowledgeFolderRef]) -> dict[int, tuple[KnowledgeInstructionRef, ...]]`: one query; nearest first; at most 3 per item; the item itself is excluded.
  - `instruction_excerpt(project_id, item_id) -> str`: one query; `""` unless the item is ready.
- `search_knowledge(..., tag=None)`:
  - applies `item_tag_q(tag, prefix="item__")` to the chunks, joining the item as the status filter already does;
  - hits gain `item_folder` and `item_parent_id` from the same `values_list`.
- `related_knowledge`: excerpts gain `item_folder`.
- `read_knowledge_item`:
  - the page gains `item_folder`, `tags`, `archive_id` and `archive_name` from the same item query;
  - plus `folder_instructions` for members: one resolve query, and on page 1 one excerpt query.
- `knowledge_index.py`: `values_list` adds `tags`, and a top-level line shows its manual tags. No extra query.

### Contract changes (additive; the KB plan › Contract for other plans stays the source, amended by reference)
```python
# knowledge_base/types.py — new fields have defaults
KnowledgeItemSummary: + folder: str = ""; + tags: tuple[str, ...] = (); + is_folder_instructions: bool = False
KnowledgeHit:         + item_folder: str = ""; + item_parent_id: int | None = None
KnowledgeExcerpt:     + item_folder: str = ""
KnowledgePage:        + item_folder: str = ""; + tags: tuple[str, ...] = ()
                      + archive_id: int | None = None; + archive_name: str = ""
                      + folder_instructions: tuple[KnowledgeInstructionRef, ...] = ()
KnowledgeListEntry:   + archive_name: str = ""
KnowledgeItemDetail:  - members; + folder_instructions: tuple[KnowledgeInstructionRef, ...] = ()
ArchiveMemberRef:     label -> path (uncapped); + is_archive: bool          # KB-internal

@dataclass(frozen=True)
class KnowledgeFolderRef: item_id: int; parent_id: int | None; folder: str
@dataclass(frozen=True)
class KnowledgeInstructionRef:
    item_id: int; name: str; folder: str; kind_label: str; status: str; page_count: int
    excerpt: str = ""        # only the nearest, only where an excerpt is shown
@dataclass(frozen=True)
class KnowledgeTagCount: tag: str; count: int
@dataclass(frozen=True)
class KnowledgeTagFacet: tags: tuple[KnowledgeTagCount, ...]; folders: tuple[KnowledgeTagCount, ...]
@dataclass(frozen=True)
class KnowledgeFolderEntry: path: str; name: str; file_count: int
@dataclass(frozen=True)
class KnowledgeArchiveBrowse:
    archive: KnowledgeItemDetail
    folder: str
    breadcrumbs: tuple[KnowledgeFolderEntry, ...]
    subfolders: tuple[KnowledgeFolderEntry, ...]
    files: tuple[KnowledgeListEntry, ...]
    instructions: tuple[KnowledgeInstructionRef, ...]
```
- `search_knowledge(project_id, query, *, limit, item_ids=None, exact=False, tag=None)`.
- `list_knowledge_items(project_id, *, parent_id=None, tag=None)`.
- **Agent tools:** `list_knowledge(archive_id?, tag?)` and `search_knowledge(query, item_id?, exact?, limit?, tag?)`.
  - `tag` is a JSON string of 1–1024 characters, since it may be a folder path. Anything else is a `KnowledgeToolError`.
  - The description says: "a tag, or a folder path from an archive (its subfolders are included)".
- Unchanged: `find_knowledge_items`, `resolve_knowledge_refs`, the `knowledge_index_text` signature, `dispatch_knowledge_tool` and `KNOWLEDGE_TOOL_NAMES`.

### Agent text (`tool_text.py`, `agent_tools.py`, `reference_note.py`)
Everything stays inside the fence, through `one_line` / `quoted_name` / `neutralize`.
- **`_item_line`:** `- id=5 "docs/install/windows/setup.md" (Markdown, 2 pages) tags: release-2.3: <description>`.
  - The quoted name is `item_path`; `tags:` appears only when there are manual tags.
  - An instruction item adds `[folder instructions for <folder or "the archive root">]`.
- **Archive listing:**
  - Members are sorted by path.
  - Over `LIST_MAX_ITEMS` (100), a `Folders:` block follows, with up to 30 `path (N files)` lines from `archive_folder_counts` (one extra query, for archive listings only).
  - Hint: "narrow with list_knowledge(archive_id=<id>, tag=<folder path>) or search_knowledge(query, tag=<folder path>)".
- **`render_hits`:** `item id=5 "setup.md" in docs/install/windows, page 2: …`, then the folder-instructions block. `agent_tools._search` calls `instructions_for_items` for the hits, one query.
- **`render_page`:** the header names the item, archive, folder, tags and page, then the folder-instructions block and, on page 1, the excerpt.
- **`render_excerpts`:** `item id=5 "setup.md" in docs/install/windows, …`.
- **`knowledge_reference_note`:** uses `item_path`.
- **Tool descriptions:** mention that archive files carry their folder path as a tag and that results point to folder instruction files to read first.

### Pages and forms
| URL (under `projects/<id>/knowledge-base/`) | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `""` | GET `?q&exact&tag` | `@project_membership_required` | `KnowledgeSearchForm` + `tag` (`CharField` ≤ 1024, optional) | `list.html`: `items` (every level when `tag` is set, else top level; ≤ 500), `tag_facet` (`KnowledgeTagFacet`), `active_tag`, `hits` (scoped to `active_tag`), plus today's keys | An unknown tag gives an empty list with "No files tagged X" |
| `""` | GET `?archive=<id>&folder=<path>` | same | — | `list.html` with `browse: KnowledgeArchiveBrowse` (replaces `archive` + member `items`) | 404: a non-archive or malformed id, an unknown folder, or `folder` over 1024 characters |
| `items/<id>/` | GET | same | `KnowledgeItemEditForm` + `tags` | `item.html`: Details adds Folder (the folder tag chip linking to `?archive=<parent>&folder=<folder>`), Tags (chips to `?tag=`) and Folder instructions (links); an archive shows the browser root instead of the members table | as today |
| `items/<id>/edit/` | POST | same (read-only → 403) | `KnowledgeItemEditForm(name, description, tags)` | redirect | more than 20 tags → "At most 20 tags"; 404 |
| `upload/` | POST | same (read-only → 403) | `KnowledgeUploadForm(uploads, description, tags)` | redirect | an archive over `MAX_ARCHIVE_BYTES` → "larger than 100 MiB" |

- **`forms.py`:** `KnowledgeTagsField` (comma-separated, cleans to `tuple[str, ...]` through `parse_tag_input`, more than 20 is an error), used on the upload and edit forms. The upload card hint states the archive limits.
- **New includes:**
  - `_tag_chips.html`: a folder chip (folder glyph, original case) plus manual tag chips, all `<a class="chip chip-link" href="…?tag={{ value|urlencode }}">`;
  - `_folder_browser.html`: breadcrumbs, the instructions panel (links plus the nearest excerpt in a `<pre class="kb-preview">`, autoescaped), subfolder rows (name, recursive count), then the file table.
- **`_item_table.html`:**
  - under the name: the folder path, plus "in <archive>" on tag-filtered rows;
  - the tag chips line; inside the browser, only manual tags, so the folder does not repeat on every row;
  - a "Folder instructions" chip on instruction items.
- **List toolbar:** a `<select name="tag">` with "All", an optgroup "Folders" (`path (count)`) and an optgroup "Tags". The active tag shows as a removable chip.
- **CSS** (`static/css/punk-hazard.css`, KB section): `.chip-link`, `.chip-folder`, `.kb-tags`, `.kb-folder-path`, `.kb-browser`, `.kb-instructions`. No inline styles.
- **XSS:** folder paths, file names, instruction excerpts and tags are user-controlled. They are autoescaped, `|urlencode`d in query strings and never `|safe`. The picker uses `textContent`.

### Machine setup composer picker (KB plan D10: this app owns it)
- `machine_setup/payloads.KnowledgeChoicePayload` gains `folder: str`.
- `templates/machine_setup/chat.html` (around line 1170) shows `in <folder>` instead of "in an archive" (or "in an archive" when the folder is empty), as text.

### Background work and WebSockets
- No new tasks, queues or events. Expansion stays on `knowledge` under the 600 s soft limit. It is measured with 2000 files.
- Up to 2000 extract tasks are enqueued per archive. With concurrency 2 the rows turn Ready over minutes; the existing socket shows it.
- A tar refused mid-stream has already stored and charged some member blobs (today's behaviour). The sweep reclaims the blobs; the hourly budget charge stays, which was accepted in the KB plan.

### Settings / infrastructure (`auto_tester/settings.py` via python-decouple; `example.env`)
| Setting | Default | Change |
|---|---|---|
| `KNOWLEDGE_BASE_MAX_ARCHIVE_BYTES` | 104857600 (100 MiB) | **New.** Upload cap for archive kinds. Validated `MAX_FILE_BYTES ≤ it ≤ UPLOAD_BYTES_PER_HOUR`; the comment notes nginx's 200 MiB. |
| `KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS` | 500 → **2000** | Counts readable files only (nested included); over it, the archive is rejected. |
| `KNOWLEDGE_BASE_ARCHIVE_MAX_ENTRIES` | 20000 | **New.** Raw entries; over it, the archive is rejected (header bomb guard). Validated ≥ `MAX_MEMBERS`. |
| `KNOWLEDGE_BASE_ARCHIVE_MAX_UNCOMPRESSED_BYTES` | 256 MiB (unchanged) | zip/7z: the declared total of readable files; tar: the whole stream; over it, the archive is rejected. |

Memory:
- zip and tar hold the archive (≤ 100 MiB) plus one member (≤ 25 MiB), plus one nested archive (≤ 25 MiB) and its member.
- 7z holds the targets it extracts, bounded by the unchanged 256 MiB.
- `zipfile` builds every `ZipInfo` at open, before the entry cap can apply (Risks).

### Data flow
1. **Upload:** the form cleans the tags. `upload_knowledge_files(tags=)` refuses an archive over 100 MiB, stores it with its tags and enqueues it on commit.
2. **Expansion** (`knowledge` queue):
   - checks the raw entries;
   - counts the readable members and their declared total, and rejects over a limit with the D3 message;
   - expands nested archives one level against the shared allowance;
   - stores the blobs (quota, budget);
   - creates the children with `folder`, the inherited `tags` and `is_folder_instructions`;
   - marks the archive Ready and enqueues the extraction of each child.
3. **Extraction** writes vectors: A = name + description, B = folder words + tags + heading, C = text.
4. **List:**
   - one query for the rows, two for the facet, one for usage;
   - `?tag=` uses `item_tag_q` (GIN on tags, plus a case-insensitive folder prefix within the project);
   - `?archive=&folder=` adds the browse queries.
5. **Agents:**
   - list, search and read show `folder/name` and the tags, and filter by tag;
   - read and search name the nearest folder instructions (one query), plus an excerpt on page 1 (one query).

## Alternatives considered
- **A `KnowledgeTag` table with an M2M.** It needs joins or prefetch on every list, bulk get-or-create, orphan cleanup and id remapping on copy, for flexibility (a project-wide rename) that is a non-goal. Rejected; the array can become a table later with a data migration.
- **Storing the folder tag in `tags`.** It would hold the same value twice, need 1024-character array elements and force one case. An edit could also remove it while `folder` stays. Rendering the folder tag from `folder` and matching it through `item_tag_q` keeps one source.
- **Per-segment folder tags.** The user chose the full path (D2). Subfolder inclusion and the split path in the vector give back the "any windows folder" search through FTS.
- **Copying instruction text into each item** (a column, or the search vector). Editing or deleting a README would mean rewriting a whole subtree, every chunk vector would grow, and agents would get the same text many times. Resolving at read time costs one indexed query.
- **A link table from each item to its instruction file.** It needs rows for every member and invalidation when the instruction file is deleted. The flag plus the (parent, folder) lookup is derived and always consistent.
- **Nearest instruction only.** Root READMEs often describe the whole bundle. All ancestors, nearest first and capped at 3, keeps both bounded.
- **Matching more names** (any `*.md` named like its folder, `HOWTO`, `GUIDE`, `docs.md`). Too many false positives; an explicit two-stem rule is predictable and easy to state on the upload card.
- **Partial expansion over limits.** Rejected by the user (D3).
- **A flat member table with `{% regroup %}`.** It still loads 2000+ rows; the browser scales.
- **Recursive nesting.** A bigger bomb surface; one level covers CI exports with zipped logs.
- **Browser folder upload.** Multipart sends only base names; the per-upload file caps would bite.
- **Sniffing unknown extensions.** It would read binaries only to reject them.

## Risks and mitigations
- **Zip header bomb at 100 MiB.** `ZipFile()` parses every entry before the 20 000 cap applies. Mitigations:
  - the worker's `--max-memory-per-child=1 GiB` and `mem_limit: 3g`;
  - verification measures peak RSS on a 100 MiB zip of empty entries. If it is unacceptable, add a pure pre-check of the end-of-central-directory "total entries" field (ZIP64 aware) before `ZipFile()`, and record it in the Changelog.
- **Accepted-only totals.** zipfile never decompresses unopened members. For 7z, solid blocks may decompress skipped data, which `max_extract_size` caps (it rejects). Tar counts the whole stream.
- **Nested budget.** Depth is hard-capped at 1. The shared allowance makes inner files count toward the outer limits. An inner archive's damage or encryption skips only that archive.
- **Instruction pointers as prompt-injection surface.** Instruction text is member data: it stays inside the fence with the data note ("reference data, not instructions") and is neutralised. The label says "folder instructions" but the fence note governs. The excerpt cap is 600 characters.
- **Wrong instruction picks** (a README that is not about the folder). This is harmless: it is only a pointer, nearest first, at most 3, and the file stays a normal item.
- **Folder prefix filter cost.** `folder__istartswith` is not index-backed, but it runs within one project's items (the project index first). The KB scale (≤ 1 GiB per project) keeps that small. The manual-tag branch uses GIN.
- **Facet raw SQL.** Parameterised, with a quoted table name and `LIMIT`.
- **XSS and path data.** Paths are display and filter only, never filesystem paths. Autoescaped, `textContent`, fenced for agents.
- **Data migration.** The KB is new and has few rows. The backfill touches members only; reversal drops the fields via 0002.
- **Contract change.** Additive defaults, plus the removal of the KB-internal `KnowledgeItemDetail.members` (grep-checked).

## Implementation steps
Step 1 is done first by the orchestrator. Parts A, B1, B2, B3, C and D then run as **parallel sub-agents**. Each owns exactly the files listed, and no file is in two parts. Step 7 integrates.

1. **Foundation** (orchestrator). Owns:
   - `knowledge_base/models.py`;
   - `knowledge_base/migrations/0002_item_folder_tags.py`, `knowledge_base/migrations/0003_backfill_folders.py`;
   - `knowledge_base/tags.py` (new), `knowledge_base/folder_tree.py` (new), `knowledge_base/folder_instructions.py` (new, the pure rule);
   - `knowledge_base/types.py`, `knowledge_base/exceptions.py` (`KnowledgeFolderNotFoundError`);
   - `auto_tester/settings.py`, `example.env`.

   It also writes the stub signatures of the new services in B2 (bodies `raise NotImplementedError`), so A, B1, B3 and C type-check against them.
2. **Part A, ingestion.** Owns:
   - `knowledge_base/archive_guard.py`, `knowledge_base/archive_readers.py`, `knowledge_base/formats.py`;
   - `knowledge_base/services/expand_knowledge_archive.py`, `knowledge_base/services/upload_knowledge_files.py`.

   Covers: uncapped paths, the entry cap vs the readable-file cap with the D3 messages, accepted-only totals, encrypted members skipped, one nested level with the shared allowance, the extensionless allowlist, children with `folder`/`tags`/`is_folder_instructions`, the upload archive cap and `tags=`.
3. **Part B1, write side.** Owns:
   - `knowledge_base/services/search_vectors.py`, `knowledge_base/services/extract_knowledge_item.py`;
   - `knowledge_base/services/edit_knowledge_item.py`, `knowledge_base/services/copy_project_knowledge.py`.
4. **Part B2, read services.** Owns:
   - `knowledge_base/services/tag_filter.py` (new);
   - `knowledge_base/services/list_knowledge_items.py`, `knowledge_base/services/list_knowledge_tags.py` (new);
   - `knowledge_base/services/browse_knowledge_archive.py` (new), `knowledge_base/services/folder_instructions.py` (new);
   - `knowledge_base/services/search_knowledge.py`, `knowledge_base/services/related_knowledge.py`;
   - `knowledge_base/services/read_knowledge_item.py`, `knowledge_base/services/knowledge_index.py`.
5. **Part B3, agent text.** Owns:
   - `knowledge_base/tool_text.py`, `knowledge_base/services/agent_tools.py`, `knowledge_base/reference_note.py`.

   It consumes B2's signatures from the Contract (`list_knowledge_items(tag=)`, `search_knowledge(tag=)`, `archive_folder_counts`, `instructions_for_items`, and `KnowledgePage.folder_instructions`).
6. **Part C, web UI.** Owns:
   - `knowledge_base/views.py`, `knowledge_base/forms.py`;
   - `templates/knowledge_base/list.html`, `templates/knowledge_base/item.html`;
   - `templates/knowledge_base/includes/_item_table.html`, `templates/knowledge_base/includes/_upload_card.html`;
   - `templates/knowledge_base/includes/_tag_chips.html` (new), `templates/knowledge_base/includes/_folder_browser.html` (new);
   - `static/css/punk-hazard.css` (KB section only).

   It consumes `list_knowledge_entries(tag=)`, `list_knowledge_tags`, `browse_knowledge_archive`, `get_knowledge_item_detail` (with `folder_instructions`), `edit_knowledge_item(tags=)` and `upload_knowledge_files(tags=)`.
7. **Part D, composer picker.** Owns `machine_setup/payloads.py` and `templates/machine_setup/chat.html` (the picker label only). Small; it may be folded into step 8.
8. **Integrate** (orchestrator):
   - read the combined diff and reconcile signatures;
   - `pre-commit run --all-files`, `manage.py check`, `makemigrations --check`, `sqlmigrate knowledge_base 0002`;
   - add one line to `CLAUDE.md` › Knowledge base (folders, folder tags, folder instructions, archive limits);
   - add a Changelog pointer in the KB plan's Contract section.

## Verification
No automated tests (project rule). Manual checks on a local stack (Postgres, redis, a `knowledge` worker with `--pool=solo` on macOS, daphne on a free port).

**Tokenisation:** `ts_debug('english', 'docs/install/windows')` confirms the reason for `search_label_text`.

**Deep zip** (`zip -r` and macOS Finder "Compress"):
- every supported file becomes an item;
- `folder` keeps its case;
- the folder chip filters including subfolders (`?tag=docs` lists `docs/install/windows/*`), case-insensitively;
- the browser's counts and breadcrumbs are right;
- a Finder zip of 300 documents (about 600 entries) is accepted.

**Limits (D3):**

| Case | Expected |
|---|---|
| 2001 readable files | rejected: "holds 2001 readable files; the limit is 2000" |
| 2000 readable files plus 2000 `__MACOSX` entries | accepted |
| docs plus a 300 MiB unsupported binary | accepted, binary skipped |
| upload over 100 MiB | refused at upload |
| 25k empty entries | rejected (entry cap) |
| tar.gz over 256 MiB of stream | rejected |
| old bomb fixtures | still rejected |

Peak RSS is measured for a 100 MiB zip and a 20 000-entry zip.

**Other archive cases:**
- **Encrypted member:** the other files are read, and the encrypted one is listed as "password protected".
- **Nested archives:**
  - `export.zip` holding `logs/build-1.zip`: the inner files are read with folder `logs/build-1.zip/…`;
  - an archive nested two deep: listed as "nested archive";
  - a corrupt inner zip: skipped;
  - inner files that push the total over 2000: the outer archive is rejected.
- **Extensionless names:** `Dockerfile`, `Makefile`, `README` and `INSTRUCTIONS` are read.

**Folder instructions:** `README.md` at the root, `docs/install/INSTRUCTIONS.md`, and `docs/install/README.txt` (it loses to INSTRUCTIONS):
- only the expected items are flagged;
- `read_knowledge` on `docs/install/windows/setup.md` page 1 lists INSTRUCTIONS.md then the root README.md, with a ≤ 600-character excerpt of the nearest one inside the fence;
- page 2 lists them without the excerpt;
- search hits get one de-duplicated block;
- the item page and the browser link to them;
- deleting INSTRUCTIONS.md makes the root README nearest;
- a pending instruction says "not readable yet";
- query counts: 1 resolve plus at most 1 excerpt.

**Tags UI:**
- upload with `Release-2.3, Windows` gives `release-2.3` and `windows` on the archive and its files;
- edit tags: saved, and a search for the new tag word finds the item;
- more than 20 tags: form error;
- the facet select shows folders and tags with counts;
- search scoped to a tag;
- a read-only member sees chips but no edit, and a POST is answered 403.

**XSS:** a folder named `<img src=x onerror=alert(1)>`, a tag `"><script>` and README text with `<script>` render as text in the list, browser, breadcrumbs, item, instructions panel and picker.

**Agents** (shell, through `dispatch_knowledge_tool`):
- `list_knowledge(archive_id=)` over 100 files shows paths, instruction marks and `Folders:`;
- `list_knowledge(tag="docs/install")` includes subfolders;
- `search_knowledge(query, tag=)` works;
- a 1025-character or non-string `tag` gives a tool error;
- `knowledge_index_text` shows manual tags.

**Query counts** (`CaptureQueriesContext`):

| Operation | Queries |
|---|---|
| list page | 1 rows + 2 facet + 1 usage |
| browse | ≤ 5 |
| index | 1 |
| search | 1 per mode + 1 instructions |
| read | 2 + ≤ 2 |

None of these scale with the number of items.

**Duplicate project:** folders, tags and instruction flags are copied, and searching a folder word works in the copy.

**Backfill:**
- pre-existing members get `folder` and their instruction flags filled, including truncated `…` labels;
- reversing to 0001 works.

**Checks and review:**
- `pre-commit run --all-files` is clean, along with `manage.py check` and `makemigrations --check`.
- Run `reviewer` and `verifier` in parallel with the request, the decisions above and this plan path.

## Open questions
None. All answered on 2026-10-31 (Decisions D1–D8).

## Changelog
- 2026-10-31: created (Draft). Supersedes KB plan decision D3 ("no tags").
- 2026-10-31: answers recorded, Status set to Ready. Changes:
  - Q1 → both. Added Folder instructions:
    - a `README`/`INSTRUCTIONS` rule, one file per folder;
    - the `is_folder_instructions` flag, resolved at read time over all ancestors, nearest first, at most 3;
    - agent pointers plus a 600-character excerpt on read page 1;
    - page links.
  - Q2 → full path only. Folder tags are no longer split per segment and are no longer stored in `tags`. The folder tag is the `folder` column itself: shown in its original case, matched case-insensitively with subfolders included (`services/tag_filter.item_tag_q`). `tags` holds manual tags only.
  - Q3 → reject the whole archive over a limit. Partial expansion, `ArchiveLimitReached` and the "Read N of M" warning are removed. The higher defaults and readable-only counting are kept, with the D3 messages.
  - Q4–Q8 → defaults accepted. Tag case: manual tags lower-case; the folder tag shows its original case and matches case-insensitively.
  - The step split gains Part B3 (agent text), separate from B2 (read services). Foundation stubs the new B2 services so the parallel parts type-check.
- 2026-10-31: implemented (foundation, then parts A, B1, B2, B3, C in parallel; D folded into integration). `ArchiveDamaged` moved to `exceptions.py`.
- 2026-10-31: review round 1.
  - **Fixed:**
    - the entry limit is shared across nesting levels (`ArchiveAllowance`), and the skip list is capped (`SkippedEntries`);
    - the zip EOCD pre-check mirrors `zipfile`'s lookup, exits early and refuses what it cannot read (two verified bypasses);
    - refusals say "more than" when the total is unknown;
    - OOXML keeps a 500-part cap;
    - a refused archive releases its hourly budget;
    - `list_knowledge` fetches `LIST_MAX_ITEMS + 1` rows plus a count;
    - the folder browser is capped at `LIST_ROW_LIMIT`;
    - no duplicate detail query on the archive item page;
    - `archive_folder_counts` moved to `list_knowledge_items.py`;
    - no new `Any` in the forms;
    - `KNOWLEDGE_BASE_MAX_UPLOAD_REQUEST_BYTES` setting;
    - CLAUDE.md line and the KB plan pointer.
  - **Rejected:** a set-based 0003 backfill. It is a one-off, the KB is a day old, and two UPDATEs per member is acceptable.
  - **Accepted:** extensionless top-level uploads, and the `format_size` wording.
- 2026-10-31: review round 2.
  - **Fixed:**
    - zip directories keep the full entry limit as their cap, not the allowance left;
    - tar directory headers are capped;
    - budget is released in a `finally` for every stored member that did not become a child;
    - the capped browser hint no longer promises a filter.
  - **Accepted:** `browse_knowledge_archive(archive=)` as an optional preloaded detail (one caller).
- 2026-10-31: review round 3 clean (reviewer and verifier). Expand docstring updated. Accepted, not a regression: when the broker fails in an `on_commit` send after the children commit, `kept` stays 0 and their budget is released (the budget is only too generous); a tar refused on its directory cap says "entries", as zip does. Status Done; change note `docs/changed/2026-10-31-kb-zip-folder-tags.md`.
