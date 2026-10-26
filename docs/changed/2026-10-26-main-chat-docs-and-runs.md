# Main chat: attach Markdown documents, and the agent proposes test runs

Date: 2026-10-26
Plan: docs/plan/feat-main-chat-docs-and-runs.md

## What changed
- `machine_setup/models.py`, migration `0005_documents_run_proposals`:
  - new `SetupDocument` (text stored in the DB, not MEDIA);
  - new `SetupRunProposal` (one pending per conversation);
  - new `SetupMessage.run_proposal`, set on the NOTICE row that shows the card.
- `machine_setup/document_text.py`, `run_proposal_types.py` (new, pure): upload decoding, file name sanitising, document paging, the user-turn note, `RunSpec` and the limits.
- Documents:
  - `forms.SetupMessageForm` gains an optional `document` file, and `text` is now optional;
  - `send_message` and `queue_turn.create_user_turn` store the document with the USER row;
  - `services/read_document.py` returns one page per query;
  - `services/document_download.py` and the view `setup_document` serve a members-only `text/plain` download;
  - `run_turn` appends a note with each document's id to the user message.
- Runs:
  - `services/run_case_selection.py` resolves a run's selection criteria to case ids;
  - `services/propose_test_runs.py` validates, supersedes the pending proposal and stores the new one;
  - `services/run_proposal_decision.py` handles Create runs (loops `projects.services.create_test_run_with_cases`) and Discard;
  - `services/run_proposal_events.py` re-broadcasts the card;
  - `services/run_catalog.py` lists runs, and `case_catalog.list_case_groups_text` counts cases per group.
- Agent:
  - new `services/main_chat_tools.py`: `read_document`, `list_test_runs`, `list_test_case_groups` and `propose_test_runs`, for the main role only;
  - `setup_prompt` gains ATTACHED DOCUMENTS and TEST RUNS rules for the main role;
  - `run_turn._on_runs_proposed` posts the card row.
- UI:
  - the composer gets "Attach .md" and a chip;
  - user rows show attachment chips;
  - new `includes/_run_proposal.html` card with Create runs / Discard;
  - `TOOL_LABELS` and the CSS in `punk-hazard.css` are updated.
- `projects/services.create_test_run_with_cases` loads only `id` and `testrail_source_run_id` of each case.

## Why
People want to hand the setup agent long documents, which don't fit the 8000-character field. For big projects, people want to describe the runs they need and have the agent set them up.

## Behaviour and contract changes
- URLs, all `@project_membership_required`:
  - `machine_setup:send` now accepts multipart and the field `document`;
  - new `machine_setup:document` (GET `documents/<id>/`);
  - new `machine_setup:run_proposal_create` (POST `run-proposals/<id>/create/`);
  - new `machine_setup:run_proposal_discard` (POST `run-proposals/<id>/discard/`).
- Message payload: new `attachments` (USER rows) and `run_proposal` (card rows). There is no new socket event; cards update through `machine_setup.message` upserts.
- Main-agent tools: the four above. The agent only proposes runs; a person's click creates them, as Drafts, and nothing is started.
- Limits:
  - one document per message, `.md` / `.markdown` / `.txt`, strict UTF-8, at most `MACHINE_SETUP_DOCUMENT_MAX_BYTES` (new setting, 262144, in `example.env`);
  - 12k characters per read page;
  - 50 runs per proposal and 1000 cases per run.
- Documents and proposals are main chat only: the form, the send service and `propose_test_runs` each refuse other threads. Reset takes both with the conversation.
- Deploy: `migrate` (additive), then restart Daphne/web and the execution worker.

## Notes for future changes
- Selection: filters are ANDed, explicit ids are unioned, excludes are removed last. Cases store no TestRail section, so "one run per section" needs a section import first.
- `message_payload` reads `documents` for USER rows and `run_proposal` when its FK is set. Callers must prefetch or pass `attachments=` and defer `run_proposal__case_ids` to avoid hidden queries.
- Accepted residuals:
  - a worker dying between storing a proposal and posting its card leaves an orphan pending proposal, which the next proposal supersedes;
  - the 256 KiB cap is checked after Django receives the body (nginx allows 200M, as for every upload). A tighter nginx limit is an infrastructure follow-up.
- Verified with:
  - pre-commit (strict mypy), `manage.py check` and `makemigrations --check`;
  - a throwaway-DB smoke script, 41 checks: validation, paging, scoping/404s, propose / supersede / create / double create / discard, Drafts only, payloads; 7 queries for the chat page and 24 to create 3 runs;
  - two reviewer and verifier passes.

  Not yet seen in a browser, with a live runner or with the real model.
