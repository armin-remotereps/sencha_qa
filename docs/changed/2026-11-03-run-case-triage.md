# Run case analysis: category and severity per case

Date: 2026-11-03
Plan: docs/plan/feat-run-case-triage.md

## What changed
- New app `run_triage`: `RunTriage` / `TriageGroup` / `TriageCase` (migration `0001_initial`), fixed taxonomy (`taxonomy.py`: 8 categories, critical/major/minor/trivial), case scope (`case_scope.py`: Failed, Blocked, Needs re-test, Cancelled-with-result as "Untested"; Passed counted only).
- `run_triage/services/`: `triage_input` (case text, verdict + step breakdown, log tail via `Right`, failing step's screenshot downscaled), `classify_cases` (batched, bounded parallel AI calls, hash reuse, deadline-bounded, turn-gated writes), `group_cases` + `group_writes` (groups, one sub-group level, category note), `correct_cases` (chat corrections), read side `agent_texts` / `card` / `report` / `badges` / `triage_state`, `triage_export` (CSV), `triage_brief`; `markdown_export.py`; `turn_liveness.py`, `tool_deadline.py`.
- `agents/services/run_case_classifier*.py`, `run_case_grouper*.py`, `agents/triage_types.py`: tool-free strict-JSON AI calls.
- Machine setup: thread kind `run_triage` ("Case analysis", `SetupConversation.triage_run`, one active per run, migration `0012_run_triage_thread`), role `ThreadRole.RUN_TRIAGE` (project data, KB, evidence and triage tools; no `run_setup_task`), `open_triage_chat.py`, `triage_chat_tools.py`, `case_evidence_tools.py` (`read_case_log` / `ask_case_screenshot` moved out of `result_chat_tools.py`, result chat contract unchanged; triage role takes a `case` ref), `analysis_threads.py` + `includes/_analysis_threads.html` (Analysis section on the chat page), prompt section, send gates without the READY gate.
- Pages: run page "Case analysis" card and severity/category badges on case rows and the case page; report page with Copy as Markdown; CSV export.

## Why
Users want every run case triaged by AI into a category and severity, grouped by root cause with a note per category (like their hand-written run summaries), and to discuss and correct it in a chat.

## Behaviour and contract changes
- URLs: `POST /projects/<id>/machine-setup/triage/` (`machine_setup:triage_chat_open`, `test_run_id`, `text?`, `next?`); `GET /projects/<id>/test-runs/<run>/triage/` (`run_triage:report`) and `.../triage/export.csv` (`run_triage:export_csv`, 5/min/user).
- Migrations: `run_triage.0001_initial`, `machine_setup.0012_run_triage_thread` (additive).
- Settings (`example.env`): `RUN_TRIAGE_BATCH_SIZE`, `_PARALLEL`, `_MAX_CALLS_PER_TOOL_CALL`, `_CALL_TIMEOUT_SECONDS`, `_CASE_INPUT_MAX_CHARS`, `_LOG_TAIL_CHARS`, `_SHOTS_PER_CASE`, `_SHOT_MAX_EDGE`, `_MAX_CASES`, `_LIST_PAGE_SIZE`.
- An analysis is an ordinary setup turn: while it runs, the project's other setup chats, run Start/Resume/Re-run, help Continue, Night auto-fix and Manual Claim/Continue are refused; it cannot start while an agent-driven run is Started. No TestRail writes.

## Notes for future changes
- Corrections apply directly (marked "corrected in chat by …") and survive re-analysis unless the case changed; stale = classified status/finish time differs from the case now.
- Every triage write locks the turn row and checks RUNNING (`run_triage/services/turn_gate.py`).
- Bump `run_triage.taxonomy.TRIAGE_PROMPT_VERSION` when the classifier prompt or taxonomy changes (part of the reuse hash).
- Accepted deviations: CSV built in memory (bounded by `RUN_TRIAGE_MAX_CASES`, not streamed); manual cases send the tester's own notes (not the TestRail rewrite); `SCREENSHOT_MAX_BYTES` mirrored in `triage_input.py`.
- Follow-up: machine-free analysis turns that run beside machine turns (Q11).
- Not exercised against a database or a real model (no Postgres locally): strict mypy, `manage.py check`, `makemigrations --check`, SQL compilation and template compilation only.
