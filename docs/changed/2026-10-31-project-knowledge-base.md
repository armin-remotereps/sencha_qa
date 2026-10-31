# Project knowledge base

Date: 2026-10-31
Plan: docs/plan/feat-project-knowledge-base.md

## What changed
- New app `knowledge_base`:
  - **Models:** `KnowledgeBlob` (content-addressed file, shared by duplicated projects), `KnowledgeItem` (one file or archive member) and `KnowledgeChunk` (about 1500 characters, with a page, a location and a weighted `search_vector`).
  - **Pure modules:** formats (`formats.py`), content sniffing, text decoding, chunking, the archive guard and readers, and one extractor per format family (`extractors/`). The `.doc` reader is our own MS-DOC piece-table parser over `olefile`.
  - **Services:** one use case per module under `knowledge_base/services/`. They cover upload, extract, expand archive, edit, delete, retry, copy, sweep and download, plus the read contract.
- Project tab **Knowledge base** (`knowledge_base:list`, under `projects/<id>/knowledge-base/`):
  - multi-file upload;
  - full-text and exact search with highlighted snippets;
  - an item page with an extracted-text preview, rename/describe, Retry, Delete and Download;
  - a live status socket at `ws/projects/<id>/knowledge-base/`.
- Agents:
  - `knowledge_base/services/agent_tools.py` provides `list_knowledge`, `search_knowledge` and `read_knowledge`.
  - **Setup lead:** gets all three tools in every role, plus a KNOWLEDGE BASE prompt section with the index.
  - **Setup and tester sub-agents:** get search and read (rule: "how, not what"). Allowlisted sub-agents do not.
  - **Orchestrator planner:** gets the index and a bounded tool loop when the project has ready items.
  - **Ask AI advisor:** gets `related_knowledge` excerpts.
- Setup chat composer: a "Knowledge base" picker references up to 5 items in any thread (`SetupMessage.knowledge_items`), shown as chips and added to the turn as notes.
- Duplicate project: a "Copy the knowledge base" checkbox, on by default. It copies ready items and chunks through a `knowledge` task and shares the files.
- Infrastructure:
  - `knowledge-celery` worker (queue `knowledge`, `--max-memory-per-child`, `mem_limit: 3g`) and the `KNOWLEDGE_BASE` volume (api and worker only, never nginx);
  - an hourly storage sweep (beat);
  - new dependencies `pypdf`, `python-docx`, `openpyxl`, `defusedxml`, `xlrd`, `olefile` and `py7zr`, plus stubs.
- `sanitize_file_name` moved to `auto_tester/file_names.py`. `projects.decorators` now accepts views returning any `HttpResponseBase` (needed for `FileResponse`).

## Why
Machine setup and test execution struggled because the agents did not know the project's own material: install guides, TeamCity exports, configs, logs. Members can now upload it once, and every agent can search and read it.

## Behaviour and contract changes
- **Migrations:** `knowledge_base.0001_initial` and `machine_setup.0007_message_knowledge_items`. Both are additive, with no data loss.
- **Settings** (all in `example.env`):
  - `KNOWLEDGE_BASE_*`: root, file, files-per-upload and project limits, hourly upload budget, item characters, archive members and uncompressed size, PDF pages, sheet cells, extraction time limit, and the 7z switch;
  - `ORCHESTRATOR_PLAN_MAX_TOOL_ROUNDS` (4) and `ORCHESTRATOR_PLAN_TIMEOUT_SECONDS` (180);
  - `DATA_UPLOAD_MAX_NUMBER_FILES` is now set explicitly to 100.
- **7z:** `KNOWLEDGE_BASE_ACCEPT_7Z=True`. Every 7z bomb test was refused before extraction within the limits (Q11).
- **Contract for other plans:** exactly as in the plan › Contract for other plans. That covers the services `search_knowledge`, `read_knowledge_item`, `list_knowledge_items`, `find_knowledge_items`, `related_knowledge`, `knowledge_index_text` and `resolve_knowledge_refs`; `agent_tools.py`; and `run_orchestrator(knowledge_index=)` → `build_plan_system_prompt(knowledge_index=, knowledge_tools=)`.
- **Deploy:**
  1. `pip install -r requirements.txt` and `migrate`.
  2. Start `knowledge-celery`.
  3. Restart web, every worker and beat, so the hourly sweep is installed.

## Notes for future changes
- **Import rule:** `knowledge_base` never imports `projects.services`, `machine_setup.*` or `agents.services.*`. Prompt builders in `agents.services` take plain strings. The planner loop uses `tool_registry.allowlisted_dispatcher`.
- **Storage invariants:**
  - Blobs are written before their items exist and outside the project lock. `KnowledgeBlob.stored_at` plus `BLOB_GRACE` (1 h) protect them.
  - Delete and the sweep unlink files while the rows are locked.
  - Every byte written goes through `upload_budget.reserve_upload_bytes`, for uploads and archive members alike.
- **Claim token:** `extract_started_at` is the claim. Every worker write checks it, and a soft time limit fails the item under its own claim. Items stuck Waiting or Reading past the time limit can be retried.
- **Accepted deviations** (plan Changelog):
  - planner `max_iterations = ROUNDS` plus one tool-free call;
  - a fully scanned PDF fails;
  - caps truncate with a warning;
  - cells over 4000 characters are cut;
  - the upload field is named `uploads`;
  - the tab lists the newest 500 items.
- **Rejected review findings** (plan Changelog): a pg_trgm index, streaming 7z, a copy into a non-empty target, and a per-user disk ceiling.
- **Local development on macOS:** the knowledge worker needs `--pool=solo` (the prefork pool breaks under spawn). Docker on Linux is unaffected.
- **Verified:**
  - pre-commit (black, isort, strict mypy; the controller_client and vision_worker hooks were skipped: their venvs are absent and those folders are untouched), `manage.py check`, `makemigrations --check` and `sqlmigrate`;
  - smoke scripts for every format, bomb, sniffing, limit, query count, upload/delete/sweep/budget path and planner path (with a faked LLM);
  - a browser check on a local stack with a real `knowledge` worker: upload, zip expansion, search highlight and escaping, item preview, live status, the composer picker, and the mobile layout;
  - five reviewer/verifier rounds.
- **Not verified:**
  - live agent runs (setup chat, planner or Ask AI with a real LLM, or a real Test Runner);
  - `.xls` files saved by Excel (the fixtures were hand-patched) and encrypted `.docx`/`.xlsx`;
  - Celery prefork on Linux, time limits under prefork, and a Docker image build.
