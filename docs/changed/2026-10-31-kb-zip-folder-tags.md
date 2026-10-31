# Knowledge base archives keep their folders

Date: 2026-10-31
Plan: docs/plan/feat-kb-zip-folder-tags.md

## What changed
- `KnowledgeItem` gains three fields:
  - `folder`: the member's directory inside its archive. It is the item's **folder tag**: one tag with the full path, case kept, matched case-insensitively with subfolders.
  - `tags`: manual tags, lower-case, at most 20. They are given at upload, inherited by an archive's files, and editable on the item.
  - `is_folder_instructions`.
- Pure helpers:
  - `knowledge_base/tags.py`: tags, member paths and search words.
  - `folder_tree.py`: subfolders, breadcrumbs and ancestors.
  - `folder_instructions.py`: the README / INSTRUCTIONS rule.
- **Ingestion** (`archive_guard.py`, `archive_readers.py`, `services/expand_knowledge_archive.py`, `services/upload_knowledge_files.py`):
  - Every readable file in every folder becomes an item. Nested archives are expanded one level deep, and their file name becomes a folder segment.
  - One `ArchiveAllowance` is shared across levels and counts readable files, raw entries and bytes. Over any limit, the whole archive is refused with a "holds N … ; the limit is M" message.
  - Metadata, hidden files and unsupported types do not count. Encrypted zip members are skipped.
  - A zip header-bomb pre-check reads the central directory in place, as `zipfile` would, before `ZipFile` opens.
  - Well-known extensionless names (README, Dockerfile, Makefile, …) are read.
  - When an archive fails, the hourly budget reserved for its unused members is given back.
- **Search:** chunk vectors weigh the folder words and manual tags like headings (B). `services/tag_filter.item_tag_q` is the one tag filter, used by the list, search and agent tools.
- **Read services:**
  - `services/folder_instructions.py`: the instructions that apply to an item, in one query.
  - `services/list_knowledge_tags.py`: the tag and folder facet.
  - `services/browse_knowledge_archive.py`: one archive folder, capped.
  - `tag=` on the list and search services, and a `limit=` on the list.
- **Agents** (`tool_text.py`, `services/agent_tools.py`):
  - Items show as `folder/name`, with their tags.
  - `list_knowledge` / `search_knowledge` take `tag`, a tag or a folder path.
  - Read and search results point to the nearest three folder instructions. Read page 1 adds a fenced excerpt.
  - A long archive listing fetches 101 rows and adds a `Folders:` summary.
- **Pages:**
  - The list has a tag filter (folders and tags, with counts).
  - `?archive=&folder=` opens a folder browser with breadcrumbs, an instructions panel and subfolders.
  - Items show folder and tag chips and their folder instructions.
  - Upload and edit forms take tags.
- `machine_setup/payloads.py` and the composer picker show `in <folder>`.

## Why
Members upload zips with deep folder trees whose structure carries meaning, and folder READMEs explain the files in that folder. Before this change, the path was lost to agents and pages, and big or macOS-made zips were refused because metadata entries counted toward the limits.

## Behaviour and contract changes
- **Migrations:**
  - `knowledge_base.0002_item_folder_tags` is additive: fields, a GIN index on `tags`, and a partial index on (parent, folder) for instruction files.
  - `0003_backfill_folders` fills `folder` and instruction flags for existing members from `original_name` and rewrites their vectors. Reversing it is a no-op.
- **Settings** (in `example.env`):
  - new: `KNOWLEDGE_BASE_MAX_ARCHIVE_BYTES` (100 MiB upload per archive), `KNOWLEDGE_BASE_ARCHIVE_MAX_ENTRIES` (20000 raw entries, shared across nesting) and `KNOWLEDGE_BASE_MAX_UPLOAD_REQUEST_BYTES` (200 MiB; it replaces a constant in the views and must stay ≤ nginx's 200M);
  - `KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS` goes from 500 to 2000 and now counts readable files only.
  - `.docx` / `.xlsx` keep a 500-part cap (`OOXML_MAX_PARTS`).
- **URLs:** the list takes `?tag=`; `?archive=<id>&folder=<path>` replaces the flat member list (404 for an unknown folder). The item edit and upload forms take a `tags` field.
- **Contract:** additive fields with defaults on `KnowledgeItemSummary`, `KnowledgeHit`, `KnowledgePage`, `KnowledgeExcerpt` and `KnowledgeListEntry`; `tag=` on `list_knowledge_items` / `search_knowledge`; new agent tool parameter `tag`. `KnowledgeItemDetail.members` (KB-internal) is removed.

## Notes for future changes
- **The folder tag is the `folder` column, not an entry in `tags`.** Filter through `item_tag_q` only. Postgres reads `docs/install/windows` as one token, so `search_label_text` splits paths into words for the vectors.
- **Folder instructions are resolved at read time** (same archive, ancestor folder, flag set), never copied into other items. Deleting the file simply removes it from the results. A rename does not unset the flag.
- **Accepted:**
  - the 0003 backfill runs two UPDATEs per member (one-off; the KB is a day old);
  - extensionless names are accepted as top-level uploads too;
  - an archive's tag edit does not reach its files;
  - archive listings are ordered by the database's folder/name collation.
- **Verified:**
  - pre-commit (black, isort, strict mypy; the controller_client / vision_worker hooks were skipped as untouched), `manage.py check` and `makemigrations --check`;
  - `ts_debug` tokenisation;
  - on a throwaway Postgres and Redis: an end-to-end smoke with a deep zip, a nested zip and macOS junk (instructions picked, 127 items ready, agent read/search/list text, query counts read 4 / list 4 / search 2 / facet 2 / browse 5), copying a project, refusing a zip of 2001 files, and accepting a Finder-style zip of 1990 files plus 1990 `__MACOSX` entries;
  - pages (filters, browser, 404s, edit with 21 tags refused);
  - archive guard harnesses, including EOCD bypass attempts;
  - three reviewer/verifier rounds.
- **Not verified:** a browser check of the new pages, live agent runs with a real LLM, and a real `knowledge` worker under Celery.
