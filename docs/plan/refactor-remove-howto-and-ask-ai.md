# refactor: Remove "How to do it" guides and "Ask AI" (HITL and Manual)

Status: Done
Created: 2026-11-05
Request: Remove two AI features from both run modes, server and Test Runner. (1) The "How to do it" guides: the HITL `how_to` list on person-step hand-offs (with its once-per-case knowledge base research and KB/profile grounding) and the Manual per-step guides (`ManualStepGuide`, the `prepare_manual_case_guide` task on `assist`). (2) "Ask AI" per step: `PersonStepQuestion`, the advisor, its context builders, the `answer_person_step_question` task, the `person_step_questions` socket event, the web card UI and the runner window's question UI. Keep Discuss / Pause and discuss, the help chat, the result chat, the manual Discuss thread (`manual_case`), the Machine setup chat, the strict case-stated `command` on person steps, and the rest of HITL: the agent still prepares each step's instruction, the confirm sub-agent still runs, and evidence is still recorded. Reason: about 40% of a ~$200 daily AI bill went to the `assist` queue (981 guide tasks for 349 guides, plus Ask AI), and Discuss covers the need. Old Test Runners that are still connected must keep working, and old checkpoints must keep parsing.

## Context

Read first: `docs/changed/2026-10-29-hitl-ask-ai-and-howto.md`, `2026-11-01-hitl-howto-from-kb.md`, `2026-11-01-ask-ai-context.md`, `2026-11-01-manual-step-howto.md`, `2026-11-01-manual-run-mode.md`. Those changes are what this plan undoes. Pieces that came in with them and stay are listed under "Kept" below.

**HITL how-to (agents)**
- `agents/types.py`: `PersonHowToItem`, `PersonHowToSourceKind`, `PersonGuideStatus`, `PersonGuideNotes`, `RelatedKnowledgeText`, `PersonGuideSources`, `PersonGuideContext`, `PersonStepHandOff.how_to`, `OrchestratorCheckpoint.person_guide`.
- `agents/checkpoint.py`: the optional `person` → `how_to` key (`_how_to_item_to_json`, `_parse_how_to`, `_with_source`) and the root `person_guide` key (`person_guide_to_json` / `person_guide_from_json`, `_PERSON_GUIDE_KEY`, `_PERSON_GUIDE_STATUSES`).
- `agents/how_to_source.py`: source labels (used only by checkpoint, `person_steps`, `projects/person_step_rows`).
- `agents/services/person_guide_context.py`, `person_guide_research.py`, `person_guide_prompts.py`: the once-per-case KB research.
- `agents/services/person_step_prompts.py`: `HOW_TO_*`, the profile/notes/excerpts fences, `GUIDE_*` caps, `HowToAudience`, `_AudienceWords`, `_how_to_rules`, `profile_os_label`, `_how_to_example`, `_grounded_how_to_example`, `build_case_step_how_to_*`, `_reference_blocks`, `format_how_to_text`, the `guide=` parameters, and the confirm prompt's handling of suggested commands (`_has_commands`, `_fenced_step_and_report`).
- `agents/services/person_steps.py`: `prepare_case_step_how_to`, `_HowToContext`, `_parse_how_to`, `_how_to_item`, `_how_to_source`, `_restates`, `_comparable`, plus the `guide` / `allowed_items` / `profile_given` plumbing.
- `agents/services/orchestrator.py`: `run_orchestrator(person_guide_sources=)`, `_person_guide_context`, `_step_related_text`, `_Progress.person_guide` / `person_guide_considered`, and `progress.person_guide = resume.person_guide`.
- `agents/os_shell.py`: `open_shell_hint` and `shell_syntax_hint`. Their only users are the how-to and advice prompts.

**Ask AI (agents)**: `agents/services/person_step_advisor.py`, `person_step_advice_prompts.py`, and the types `AdviceStepSource`, `AdviceStepKind`, `AdviceCaseStep`, `AdviceStep`, `AdviceEarlierStep`, `PersonStepAdviceContext`.

**projects (HITL side)**
- `person_guide_sources.py`. It is used by `projects/services.py` (around lines 3590–3625), which also reads the planner's fresh `artifacts_profile` / `knowledge_index` through the same memo.
- `person_step_rows.py`: `how_to_rows`, `stored_how_to`, and `how_to=` in the row writer.
- `person_step_services.py`: panel fields `how_to`, `how_to_has_sources`, `has_commands` (it also counts suggested commands), `questions`, `can_ask`, `ask_blocker`, `has_pending`, the `include_questions` parameter, and `how_to=stored_how_to(...)` in the checkpoint rebuild (around line 708).
- `how_to_view.py`.
- `person_step_question_services.py`, `_view.py`, `_views.py`, `_push.py`, `advice_context.py`, `person_step_advice_mapping.py`.
- `runner_person_step_services.py`: the `assist` / `guide` flags, the questions query, `_how_to_payloads` with its fold to 8 items, the inlined how-to for old runners (`_HOW_TO_HEADING`, `format_how_to_text`), `"how_to"` in `_STEP_FIELDS`, and `ask_from_runner`.
- `runner_window_state.py`: `RunnerWindowFeatures.assist` / `guide`.
- `controller_consumer.py`: `_assist_enabled`, `_guide_enabled`, the `PERSON_STEP_QUESTION` route, `_handle_person_step_question`, `_ask_manual_step`, `_is_manual_question`, `_question_refused`, the `_PERSON_STEP_QUESTION_*` texts, and `questions_enabled=` in the snapshot push.
- `consumers.py`: `test_run_case_person_step_questions`.
- `views.py`: `include_questions=True`, `person_step_question_form`.
- `urls.py`: `test_run_case_person_ask`.
- `forms.py`: `PersonStepQuestionForm`, `RunnerPersonStepQuestionForm`, `RunnerManualStepQuestionForm`.
- `exceptions.py`: `PersonStepQuestionError`.

**projects (Manual side)**
- `manual_case_claim_services.py`: the `request_manual_case_guide` hook (line ~127).
- `manual_step_guide_services.py`, `manual_step_guide_read.py`, `manual_step_guide_worker.py`, `manual_step_guide_fingerprint.py`, `manual_advice_mapping.py`, `manual_question_push.py`.
- `manual_case_display_services.py`: step `how_to` / `how_to_state`, `how_to_shell_label`, `guide_preparing`, `questions`, `questions_payload`.
- `runner_manual_state_services.py`: `assist` / `guided`, `get_manual_ask_state`, `load_manual_guide`, `_fits`, `_step_payload`, `_how_to_payloads`, and the extra `test_case__steps` / `test_case__expected` columns loaded only for the fingerprint.
- `runner_manual_services.py`: `ask_manual_from_runner`.
- `tasks.py`: `answer_person_step_question`, `prepare_manual_case_guide`, `_MANUAL_GUIDE_CHUNK_SECONDS`.

**machine_setup (manual Discuss)**
- `services/manual_case_context.py` reads the guide (`_guide_block`), the Ask AI Q&A (`_questions_block`), and the claim OS. The claim OS is stored only on `ManualStepGuide.os_name` (`load_manual_case` annotates `claim_os_name=F("manual_step_guide__os_name")`; the brief shows "TEST MACHINE (at claim)").
- `services/manual_chat_tools.py` and `services/setup_prompt.py` (around lines 555–605) name "the How to do it guide and the Ask AI questions" in the tool description and the role prompt.

**Models** (`projects/models.py`)
- `CasePersonStep.how_to` (JSONField).
- `PersonStepQuestion` and `PersonStepQuestionStatus`, with two partial unique constraints, a check constraint and two indexes.
- `ManualGuideState` and `ManualStepGuide` (one-to-one to `TestRunTestCase`).
- Constants `PERSON_STEP_HOW_TO_*`, `PERSON_STEP_QUESTION_MAX_LENGTH`, `PERSON_STEP_ANSWER_MAX_LENGTH`, `PERSON_STEP_QUESTIONS_PER_STEP`.
- The last migration is `projects/0055_manual_pause_discuss.py`.

**Settings** (`auto_tester/settings.py`, `example.env`)
- `PERSON_STEP_QUESTION_TIMEOUT_SECONDS`, `PERSON_STEP_QUESTION_MAX_TOOL_ROUNDS` and its two validations.
- `MANUAL_GUIDE_MAX_STEPS`, `MANUAL_GUIDE_PARALLEL`, `MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS`.
- `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS`, `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS`.
- The `MANUAL_GUIDE_MAX_STEPS_CAP` import from `controller_client.protocol`.
- Two `CELERY_TASK_ROUTES` entries on `assist`.
- The `KNOWLEDGE_SEARCH_TIMEOUT_MS` comment ("related excerpts for guides and Ask AI").

**Celery `assist` queue**: besides the two tasks being removed, it routes `projects.tasks.scan_artifacts_task` and `quick_check_task` (readiness Scan / Check machine; short runner round trips, no LLM). The compose service is `assist-celery` with `--concurrency=8`. `.claude/skills/deploy/SKILL.md` tails its logs.

**knowledge_base**: `services/related_knowledge.py`, `tool_text.render_excerpts` and `types.KnowledgeExcerpt` are used only by `person_guide_sources.py` and `advice_context.py` (checked with `git grep`). They become dead.

**Web UI**
- `templates/projects/includes/_person_step_card.html`: the How to do it list, the "Ask the AI about this step" section, and the output field gated on `has_commands`.
- `_how_to_list.html`.
- `_manual_case_card.html`: per-step `<details class="manual-step-howto">`, the `guide_preparing` hint, and the "Questions to the AI" list.
- `test_run_case_detail.html`: the Alpine components `personStepAsk` and `personStepQuestionDraft`, the socket branch for `person_step_questions`, and header comments.
- `static/css/punk-hazard.css`: `.person-step-howto*`, `.step-list .manual-step-howto`, `.person-step-ask`, `.person-step-qa*`, `.manual-questions*`.

**Test Runner** (`controller_client/`, `CLIENT_VERSION` 0.16.0)
- `protocol.py`:
  - Capabilities `PERSON_STEP_ASSIST_V1`, `PERSON_STEP_GUIDE_V1`, `MANUAL_STEP_GUIDE_V1`.
  - Message types `PERSON_STEP_QUESTION`, `PERSON_STEP_QUESTION_RESULT`.
  - Constants `PERSON_STEP_HOW_TO_*`, `PERSON_STEP_QUESTION_MAX_CHARS`, `PERSON_STEP_STATE_MAX_QUESTIONS`, `PERSON_STEP_QUESTION_MAX_BYTES`, `MANUAL_GUIDE_MAX_STEPS_CAP`, `MANUAL_GUIDE_SNAPSHOT_MAX_BYTES` and its asserts.
  - Types `PersonStepQuestionWireStatus`, `PersonHowToPayload`, `PersonStepQuestionPayload`, `PersonStepQuestionAskPayload`, `PersonStepQuestionResultPayload`, `ManualStepHowToState`.
  - Payload fields `PersonStepStatePayload.how_to` / `questions`, `PersonStepStateMessage.questions_enabled`, `ManualStepPayload.how_to` / `how_to_state`, `ManualActiveCasePayload.questions` / `can_ask` / `shell_label`.
  - Their parsers and serializers.
- Window code: `client.py` (question forwarding, slots, timeouts), `person_window.py`, `person_window_ipc.py` (`QUESTION` / `QUESTION_RESULT`, `questions_enabled`), `person_window_app.py` (How to do it block, Ask section, `_qa_text`, has-command check at line ~382 counting how-to commands), `person_window_manual.py` (how-to block, Ask section), `person_window_how_to.py`, `person_window_diagnostics.py` (`--window` samples with how-to and questions).

**Old runner compatibility, checked in git.** Every released parser from 0.10.0 to 0.16.0 reads these keys with defaults: `item.get("how_to", [])`, `item.get("questions", [])`, `_extract_bool(data, "questions_enabled", default=False)`, `raw.get("questions", [])`, `_extract_bool(raw, "can_ask", default=False)`, `item.get("how_to_state", "")`, and `_extract_text(raw, "shell_label", ...)` (absent reads ""). A window hides How to do it and Ask AI when the keys are absent: `questions_enabled` False hides the HITL Ask box, `can_ask` False plus an empty question list hide the manual one, and `how_to_state` "" hides the manual how-to. A runner's `client.py` drops window questions while `_questions_enabled` is False and resets it on every disconnect. The server stores handshake capabilities as plain strings (`parse_handshake_capabilities`) and never converts them to the enum, so old runners may keep advertising the retired names. `ControllerConsumer._route_authenticated_message` ignores unknown message types.

**Kept** (came in with the removed changes but are still used):
- `agents/secret_redaction.py`: redacts instruction and expected result in `parse_hand_off`.
- `agents/json_values.py`: used by the checkpoint.
- `agents/services/knowledge_tool_loop.py`: the planner's KB loop.
- `agents/os_shell.shell_label` / `get_os_name`.
- `_fence`'s repeat-strip fix.
- `Project.default_help_mode` and the Run mode card.
- `projects/case_task_text.py`.
- `machine_readiness/profile_text.py`: `artifacts_profile_with_items` stays as the body of `artifacts_profile_text`.
- `CasePersonStep.shell_label` and `PersonStepStatePayload.shell_label`, which belong to the case-stated command.

## Goals
- No LLM call for How to do it or Ask AI anywhere. HITL prepare is one call per step again, without KB research. A Manual claim queues no task.
- Remove the code, models, settings, routes, socket event, URL, templates, CSS, JS and runner UI of both features.
- Runners 0.10.0 to 0.16.0 keep working unchanged. They get no how-to, see no Ask AI box, and nothing else changes.
- Checkpoints that contain `how_to` or `person_guide` keep parsing. The keys are ignored and dropped at the next save.
- New runner 0.17.0 without the features, offered through Update Test Runner.

## Non-goals
- No change to Discuss, Pause and discuss, the help, result, manual Discuss or Machine setup chats, the confirm sub-agent, evidence, the case-stated `command` rule, the `case_steps` refs, or the run-mode settings.
- No change to the readiness Scan / Check machine tasks, which stay on `assist`.
- No rework of `machine_readiness.profile_text` or the planner's KB loop.
- No data export tooling. A `dumpdata` before migrating is offered as an optional deploy step.
- No edits to the superseded plans and change notes. They stay as history.

## Design

### Data model
New migration `projects/0056_remove_howto_and_ask_ai.py`, generated by `makemigrations` after the model edits:
- `RemoveField(CasePersonStep, "how_to")`.
- `DeleteModel(PersonStepQuestion)`. Its constraints and indexes go with the table.
- `DeleteModel(ManualStepGuide)`.
- `PersonStepQuestionStatus`, `ManualGuideState` and the constants listed in Context go from `models.py`. Choices classes need no migration op.

**Data loss**:
- Every Ask AI question and answer, HITL and Manual.
- Every manual step guide, including its research notes and the claim-time OS.
- The stored how-to list of every person step.

None of this reaches TestRail, test sessions, the checkpoint verdict or evidence, so no result or report changes. Checkpoints stay in their JSON (see below). Reverse migration recreates empty tables and an empty `how_to` column (`default=list`), so a code rollback works without the data.

### Checkpoint compatibility (`agents/checkpoint.py`)
- Remove the writers and readers of `person.how_to` and the root `person_guide`. `_parse_person` and `checkpoint_from_json` read only named keys, so old keys are ignored with no warning.
- The next save rewrites the checkpoint without them, because `checkpoint_to_json` builds a fresh dict.
- Add a one-line comment naming the two retired keys as "ignored if present; never reuse the names".
- `projects` code that rebuilds a `CheckpointPersonStep` from a row (`person_step_services`, around line 708) drops `how_to=`.

### Services
**agents**
- Delete `person_guide_context.py`, `person_guide_research.py`, `person_guide_prompts.py`, `person_step_advisor.py`, `person_step_advice_prompts.py` and `agents/how_to_source.py`.
- In `agents/os_shell.py`, delete `open_shell_hint` and `shell_syntax_hint`.
- `person_step_prompts.py`:
  - `build_prepare_system_prompt(*, os_name, restore)` loses `guide`. The output JSON goes back to `{"kind", "instruction", "expected_result", "command"}`.
  - The "instruction says WHAT, how_to says HOW" rule goes. In its place: the instruction must be self-sufficient. Name the application or window (rule already there), and give in words the navigation the test case or plan step states (menu path, URL, file path), copied exactly and never invented. This is the d73b5f5 rule set plus the OS/shell sentence and the later evidence and marker changes.
  - `build_prepare_user_prompt` / `build_restore_user_prompt` lose `guide` and the reference blocks.
  - The confirm and mark-evidence sub-tasks judge by the case-stated `command` only. `_has_commands(hand_off)` becomes `bool(hand_off.command)`, and the suggested-command listing in `_fenced_step_and_report` goes.
  - Remove the guide-only markers from `_MARKERS` and the related constants.
- `person_steps.py`:
  - `prepare_person_step` loses `guide`.
  - `parse_hand_off` loses `allowed_items` / `profile_given` and no longer parses `how_to`. A `how_to` key the model still writes is ignored.
  - Delete `prepare_case_step_how_to` and the how-to helpers. `_redacted` stays for instruction and expected result.
- `orchestrator.py`:
  - `run_orchestrator` loses `person_guide_sources`. Remove `_person_guide_context`, `_step_related_text`, the `_Progress` guide fields and the resume carry.
  - Drop `cancellation_check` from the resume and hand-off helpers if the guide was its only use there.
  - The planner's `knowledge_index` / `artifacts_profile` parameters stay.
- `agents/types.py`: delete the types listed in Context.

**projects**
- `services.py`, case worker: replace `build_person_guide_sources(project.id)` with direct reads, only when `resume is None`: `artifacts_profile_text(project.id)` from `machine_readiness.profile_text` and `knowledge_index_text(project.id)` from `knowledge_base.services.knowledge_index`. Same queries as before for a fresh plan, none on resume. Both imports are already allowed for `projects`.
- `person_step_rows.py`: drop `how_to_rows`, `stored_how_to`, `how_to=` on create, and the `PERSON_STEP_HOW_TO_*` imports.
- `person_step_services.get_person_step_panel(test_run, project, *, pivot=None, blockers=None)`:
  - Drop `include_questions`.
  - Drop the panel fields `how_to`, `how_to_has_sources`, `questions`, `can_ask`, `ask_blocker`, `has_pending`.
  - `has_commands` becomes `bool(step.command)`. Keep the field because the template uses it.
  - Query count drops by one on the case page.
- `runner_person_step_services.get_runner_person_steps(project_id, *, case_steps)`: drop `assist` / `guide`, the questions query, the how-to payloads and the inlined how-to. Delete `ask_from_runner` and its texts. Remove `"how_to"` from `_STEP_FIELDS`, because `values()` would raise once the column is gone.
- `runner_window_state.RunnerWindowFeatures`: drop `assist` and `guide`.
- `runner_manual_state_services.build_manual_run_state`:
  - Drop the `assist` / `guided` branches, the questions, `load_manual_guide`, `_fits`, `_step_payload` and `_how_to_payloads`.
  - Drop the fingerprint-only columns, and `other_chars` / `_text_chars` if nothing else uses them.
  - The steps go out as plain `ManualStepPayload(num, text, expected)`.
- `manual_case_claim_services.claim_manual_case`: remove the guide hook and its docstring paragraph. Nothing else in the claim transaction changes.
- `manual_case_display_services`: drop the guide load, the questions query and the fields listed in Context. The panel loses up to two queries.
- `runner_manual_services`: delete `ask_manual_from_runner` and its throttle note.
- Delete these modules:
  - `person_guide_sources.py`, `how_to_view.py`
  - `person_step_question_services.py`, `person_step_question_view.py`, `person_step_question_views.py`, `person_step_question_push.py`
  - `advice_context.py`, `person_step_advice_mapping.py`, `manual_advice_mapping.py`, `manual_question_push.py`
  - `manual_step_guide_services.py`, `manual_step_guide_read.py`, `manual_step_guide_worker.py`, `manual_step_guide_fingerprint.py`
- `exceptions.py`: delete `PersonStepQuestionError`. Fix the `ManualRunError` docstring, which mentions Ask AI.
- `forms.py`: delete the three question forms. `_WireTextField` stays if other runner forms use it.
- Docstring touch-ups:
  - `case_task_text.py`: "and the Ask AI advisor".
  - `manual_case_lock.py`: "or Ask AI question".
  - `case_step_refs.py`: "the Ask AI context".

**machine_setup (manual Discuss)**
- `manual_case_context.py`:
  - Delete `_guide_block`, `_questions_block`, the `PersonStepQuestion` imports and `format_how_to_text`.
  - `load_manual_case` drops the `claim_os_name` annotation, because the field lives on the dropped table.
  - `build_manual_brief` drops the "TEST MACHINE (at claim)" block. The system prompt's TEST MACHINE section (`setup_prompt.py` line ~753, the live runner info) is the only machine source, as the existing fallback text already assumes.
  - The opening paragraph and the module docstring stop mentioning the guide and the Q&A.
  - `manual_case_state_text` loses two queries.
- `manual_chat_tools.py` (the `get_manual_case` description) and `setup_prompt.py` (the manual role text, lines ~555–605): drop "the How to do it guide" and "the Ask AI questions / answers". No other prompt change.

**knowledge_base**
- Delete `services/related_knowledge.py`, `tool_text.render_excerpts` and `types.KnowledgeExcerpt` once `git grep` confirms no user is left. This is the only knowledge_base change. The KB import rule is unaffected.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_person_ask` (`…/cases/<pivot>/person-step/ask/`) | POST | removed | `PersonStepQuestionForm` removed | — | 404 after removal (a stale tab's post) |
| `projects:test_run_case_detail` (unchanged URL) | GET | `@project_membership_required` (unchanged) | `person_step_question_form` removed from context | `_person_step_card.html`: How to do it list, Ask section and their Alpine hooks removed; the output field shows only for a case-stated command. `_manual_case_card.html`: per-step how-to, the "being prepared" hint and the questions list removed. `_how_to_list.html` deleted. `test_run_case_detail.html`: `personStepAsk`, `personStepQuestionDraft`, the `person_step_questions` socket branch and header comment lines removed | — |

Remove the matching classes from `static/css/punk-hazard.css`. The `.person-step-*` classes that the card still uses stay.

### Background work and WebSockets
- Delete the Celery tasks `projects.tasks.answer_person_step_question` and `projects.tasks.prepare_manual_case_guide`, and their routes in `CELERY_TASK_ROUTES`.
- **The `assist` queue stays.** It still carries `scan_artifacts_task` and `quick_check_task`. A person waits on those, and they must not sit behind long `upload` or `execution` work, which was the reason the queue exists.
  - `docker-compose.yml` `assist-celery`: lower `--concurrency` from 8 to 2. Only two short, project-locked, non-LLM tasks remain, so 8 idle prefork children is wasted memory.
  - Update the route comment to "Short runner round trips, no LLM; a person waits".
- Case page socket: remove the `person_step_questions` event (`consumers.test_run_case_person_step_questions`) and its JS branch.
- Controller protocol: see the next section.
- In-flight messages at deploy: tasks still queued on `assist` under the removed names are rejected by the new worker as unregistered, logged once each and discarded. That is harmless; do not purge the queue, because purging would also drop scan/check messages. Stop the old `assist-celery` together with the web containers (`docker compose up -d` recreates both). That way an old answer task cannot `group_send` `test_run_case.person_step_questions` to a new case-page consumer that no longer has the handler; the missing handler would close that page's socket. The page reconnects on reload, so even that case is cosmetic.

### Controller protocol (`controller_client/protocol.py`)
- `CLIENT_VERSION` "0.16.0" → "0.17.0". The runner bundle changes (UI removed, three capabilities no longer advertised). The bump makes `runner_is_older` offer Update Test Runner. It is not needed for compatibility.
- Remove the three `ClientCapability` members. Add a comment listing the retired strings `person_step_assist_v1`, `person_step_guide_v1`, `manual_step_guide_v1` as "may still arrive from runners < 0.17.0; ignored; never reuse".
  - Update `_PERSON_WINDOW_CAPABILITIES`, the `advertised_capabilities` docstring and the optional-capability comment above `REQUIRED_CLIENT_CAPABILITIES`. `REQUIRED_CLIENT_CAPABILITIES` itself does not change.
- Remove the `PERSON_STEP_QUESTION` / `PERSON_STEP_QUESTION_RESULT` message types and every question and how-to type, constant, parser and serializer listed in Context.
  - `serialize_person_step_state(steps, *, cases=None)` loses `questions_enabled`.
  - The manual budget asserts that mention the guide go. `MANUAL_RUN_STATE_SAFE_BYTES` and the discussion asserts stay.
- **What the server sends old runners:** the snapshot keys `how_to`, `questions`, `questions_enabled`, `how_to_state`, `can_ask` and the manual `active.shell_label` are no longer sent. Every parser since 0.10.0 defaults them (Context). So a 0.10–0.16 window shows no how-to and no Ask box, and everything else renders as today.
- **What the server accepts from old runners:** a `person_step_question` from a pre-0.17 runner falls through the consumer's router silently. Its window only sends one while the snapshot says questions are enabled, which never happens again. Its own client also refuses while `_questions_enabled` is False, and that flag resets on reconnect. So no refusal handler is kept.
- New runners against an old server: not a supported pairing, because runners update from their server. The new parsers read only named keys, so extra old-server keys are ignored anyway.
- `projects/runner_update_*` needs no change.

### Settings / infrastructure
- `settings.py`:
  - Remove the seven settings and their validations listed in Context.
  - Remove the `MANUAL_GUIDE_MAX_STEPS_CAP` import, and the two task routes.
  - Reword the `KNOWLEDGE_SEARCH_TIMEOUT_MS` comment to "agent tool, tab search".
- `example.env`: remove the same seven entries and the "shared with Ask AI" comment. Keep the `-Q assist` worker line.
- A leftover variable in a deployed `.env` is harmless, because decouple only reads what `settings.py` asks for.
- `docker-compose.yml`: `assist-celery` concurrency 2 (above). `flower`'s `depends_on` is unchanged.
- `.claude/skills/deploy/SKILL.md`: no change, since `assist-celery` still exists.

### Data flow (after)
- **HITL step:** the orchestrator reaches a person step and calls `prepare_person_step` (one LLM call, no KB research, no references). The hand-off has kind, instruction, expected result and command. The `person` checkpoint key has no `how_to`. The `CasePersonStep` row has no `how_to`. The case card and runner snapshot show the instruction, case steps and command. The person reports, and the confirm sub-agent judges by the stated command and the screen.
- **Manual claim:** `claim_manual_case` writes the claim and the verbatim checkpoint, and no task is queued. The runner snapshot carries the plain verbatim steps. Discuss (`manual_case` thread) reads the case, the marks and the pause.

## Alternatives considered
- **Keep the features behind a setting (off by default).** This was rejected. The user decided the features are not needed. Dormant code, models and a runner UI still cost upkeep in every protocol and prompt change.
- **Keep sending empty `how_to` / `questions` / `questions_enabled=false` as literal legacy keys.** This is unnecessary, because every released parser defaults the keys (checked in git for 0.10.0, 0.12.0, 0.13.0, 0.14.0 and 0.15.0, and in the current 0.16.0 file). Omitting them keeps the protocol file clean.
- **Keep a refusal handler for `person_step_question`.** This was rejected, because an old window cannot send one without `questions_enabled`, which the server never sets again. The router already ignores unknown types.
- **Drop the `assist` queue and move scan/check to `notify` or `upload`.** This was rejected. `notify` is meant to stay instant, and `upload` can be blocked for an hour by a TestRail import while a person waits on Check machine. A two-process worker costs little.
- **Keep the claim-time OS by adding `TestRunTestCase.manual_claim_os_name` with a data migration.** This was rejected. It was a guide artifact, and the Discuss role prompt already gets the live TEST MACHINE info, which the brief's fallback already relied on.
- **Keep `knowledge_base.related_knowledge` for future use.** This was rejected, because it has no consumer left. Restoring it from git is trivial.

## Risks and mitigations
- **Data loss** (questions and answers, guides, `CasePersonStep.how_to`): this was decided by the user and is not used by results or TestRail.
  - Before `migrate`, record the counts read-only: `manage.py shell -c "from projects.models import PersonStepQuestion, ManualStepGuide, CasePersonStep; print(PersonStepQuestion.objects.count(), ManualStepGuide.objects.count(), CasePersonStep.objects.exclude(how_to=[]).count())"`.
  - Optionally keep `manage.py dumpdata projects.PersonStepQuestion projects.ManualStepGuide > pre-0056.json`.
- **Rollback:** code rolled back after `migrate` needs `migrate projects 0055` first. That recreates empty tables and the column. Note it in the change note.
- **Old runners:** low risk, verified against the parsers. Smoke test with a 0.16.0 runner before and after Update (Verification).
- **Prompt regression in HITL prepare:** without how-to, instructions could become vaguer for shell or navigation steps. The restored instruction rule asks for the stated navigation in the instruction. A person who is stuck uses Discuss / Pause and discuss, which is the user's intended path.
- **Confirm sub-agent:** previously it also judged by pasted output of suggested commands. Now only a case-stated command triggers "pasted output first". This matches the pre-how-to behaviour.
- **Hidden ORM breakage:** `values(..."how_to"...)` in `runner_person_step_services`, and `F("manual_step_guide__os_name")` in `manual_case_context`, fail only at runtime. `mypy` will not catch them. Grep for `how_to`, `manual_step_guide` and `questions` after the change. The smoke checks hit both paths.
- **Deploy ordering:** web and workers restart together (compose). The in-flight `group_send` edge case is described above.
- **XSS:** only template deletions. No new user data rendering.
- **N+1 / queries:** the case page, manual panel, runner snapshots and Discuss state each lose one or two queries. Nothing is added.

## Implementation steps

### Step 0: shared-first (orchestrator, before the sub-agents)
These are the contracts every part builds on. After this step the tree does not import until parts A to D land, which is expected.

1. **Wire protocol:** `controller_client/protocol.py`. Bump the version; remove the capabilities, message types, how-to and question types, constants, fields, parsers and serializers listed under Controller protocol; add the retired-names comment.
2. **Agent types and checkpoint:** `agents/types.py` and `agents/checkpoint.py`. Delete the how-to, guide and advice types, `PersonStepHandOff.how_to` and `OrchestratorCheckpoint.person_guide`, and the `how_to` and `person_guide` read and write paths. Add the retired-keys comment.
3. **Models and migration:** `projects/models.py` and the new `projects/migrations/0056_remove_howto_and_ask_ai.py` (`makemigrations projects`; review it: one `RemoveField` and two `DeleteModel`).
4. **Settings:** `auto_tester/settings.py` and `example.env`. Remove the settings, the import and the two routes; reword the route and KB timeout comments.

### Parallel parts (one sub-agent each; no file shared between parts)

5. **Part A, agents.** Files:
   - `agents/services/person_step_prompts.py`, `agents/services/person_steps.py`, `agents/services/orchestrator.py`, `agents/os_shell.py`, `agents/CLAUDE.md`.
   - Delete `agents/how_to_source.py`, `agents/services/person_guide_context.py`, `person_guide_research.py`, `person_guide_prompts.py`, `person_step_advisor.py`, `person_step_advice_prompts.py`.

   Interfaces exposed:
   - `prepare_person_step(llm, sub_task, *, step_num, total_steps, task_description, system_info, restore=False)`.
   - `parse_hand_off(raw, *, step_num, task_description, restore)`.
   - `run_orchestrator(...)` without `person_guide_sources`.
   - `format_how_to_text` deleted.

   `agents/CLAUDE.md`: remove the "How to do it" and "Ask AI" sentences from the Person steps paragraph. Keep the `_is_explicit_command` text and the confirm text, with the suggested-commands sentence removed.

6. **Part B, projects HITL, the web case page and the consumer.** Files:
   - `projects/services.py` (direct profile and index reads), `person_step_rows.py`, `person_step_services.py`, `runner_person_step_services.py`, `runner_window_state.py`, `controller_consumer.py`, `consumers.py`, `views.py`, `urls.py`, `forms.py`, `exceptions.py`, `case_task_text.py`, `case_step_refs.py` (docstrings).
   - Templates `_person_step_card.html` and `test_run_case_detail.html`; delete `_how_to_list.html`.
   - `static/css/punk-hazard.css`: all removed classes, including the manual ones.
   - Delete `projects/person_guide_sources.py`, `how_to_view.py`, `person_step_question_services.py`, `person_step_question_view.py`, `person_step_question_views.py`, `person_step_question_push.py`, `advice_context.py`, `person_step_advice_mapping.py`.

   Interfaces:
   - `get_person_step_panel(test_run, project, *, pivot=None, blockers=None)`. `PersonStepPanel.has_commands` is kept.
   - `get_runner_person_steps(project_id, *, case_steps)`.
   - `RunnerWindowFeatures(case_steps, case_context, discussion, pause)`.
   - The consumer no longer imports `ask_manual_from_runner` or `RunnerManualStepQuestionForm`.

7. **Part C, projects Manual, manual Discuss, Celery tasks and knowledge_base cleanup.** Files:
   - `projects/manual_case_claim_services.py`, `manual_case_display_services.py`, `runner_manual_state_services.py`, `runner_manual_services.py`, `manual_case_lock.py` (docstring), `projects/tasks.py` (both tasks, imports, `_MANUAL_GUIDE_CHUNK_SECONDS`).
   - Template `_manual_case_card.html`.
   - `machine_setup/services/manual_case_context.py`, `manual_chat_tools.py`, `setup_prompt.py`.
   - `knowledge_base/services/related_knowledge.py` (delete), `knowledge_base/tool_text.py` (`render_excerpts`), `knowledge_base/types.py` (`KnowledgeExcerpt`).
   - Delete `projects/manual_step_guide_services.py`, `manual_step_guide_read.py`, `manual_step_guide_worker.py`, `manual_step_guide_fingerprint.py`, `manual_advice_mapping.py`, `manual_question_push.py`.

   Interfaces: `build_manual_run_state(*, project, runner_capabilities)` (signature unchanged). The manual panel no longer has `questions`, `questions_payload`, `how_to_shell_label` or `guide_preparing`, and its steps have no `how_to` / `how_to_state`.

8. **Part D, Test Runner.** Files:
   - `controller_client/client.py`, `person_window.py`, `person_window_ipc.py`, `person_window_app.py`, `person_window_manual.py`, `person_window_diagnostics.py`; delete `person_window_how_to.py`.

   Changes:
   - Remove the question forwarding, the IPC `QUESTION` / `QUESTION_RESULT` and `questions_enabled`.
   - Remove the How to do it and Ask sections from the HITL and Manual tabs, and their layout rows. Re-grid the following rows so no gap is left.
   - The has-command check uses `step.command` only.
   - Remove the how-to and question samples from `diagnose --window`.
   - Must pass `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`, including the win32 platform check the project already runs.

### Integration (orchestrator)
9. **Integrate.**
   - Read the combined diff and fix the seams: imports of deleted modules, the consumer to `runner_window_state` call, `views.py` to the panel signature, `tasks.py` imports.
   - Run `git grep -n -E 'how_to\b|HowTo|person_guide|PersonGuide|PersonStepQuestion|person_step_question|ManualStepGuide|manual_step_guide|MANUAL_GUIDE|PERSON_GUIDE|PERSON_STEP_QUESTION|questions_enabled|PERSON_STEP_ASSIST|PERSON_STEP_GUIDE|MANUAL_STEP_GUIDE|advice_context|Advice(Step|Case|Earlier)|related_knowledge|render_excerpts|format_how_to_text'` over everything but `docs/` and migrations. The only expected hits are the retired-name comments and `machine_readiness` `how_to_check`, which is unrelated.
   - Run `pre-commit run --all-files`.
10. **Infrastructure and docs.**
    - `docker-compose.yml`: `assist-celery` `--concurrency=2`.
    - `CLAUDE.md`:
      - Commands: assist worker comment becomes "readiness Scan / Check machine".
      - HITL paragraph: delete the "How to do it and Ask AI" block, and say once that person steps carry no AI how-to or Ask AI (removed, see the new change note).
      - Manual paragraph: delete the "Ask AI works on manual steps" and "Each manual step also gets the HITL How to do it" sentences.
      - TestRail step references: drop "and in the Ask AI step label".
      - Knowledge base Agents bullet: drop "The Ask AI advisor gets `related_knowledge` excerpts".
      - controller_client paragraph: no capability text to change, but mention CLIENT_VERSION 0.17.0 where versions are listed only if a sentence needs it.
11. **Review.** Run `reviewer` and `verifier` in parallel, fix, and loop until clean.
12. **Document.** Write `docs/changed/2026-11-05-remove-howto-and-ask-ai.md`: what changed, why (cost), migration 0056 and its data loss, protocol 0.17.0 with retired capabilities and keys, removed settings, the `assist` queue kept for Scan/Check at concurrency 2, and the rollback note. Set this plan to Done.

## Verification
- **Static:**
  - `pre-commit run --all-files` (isort, black, strict mypy for the server and `controller_client/`).
  - `python manage.py check`.
  - `python manage.py makemigrations --check --dry-run` (no pending changes after 0056).
  - `python manage.py sqlmigrate projects 0056` (expect `DROP TABLE` ×2 and `DROP COLUMN how_to`).
  - Compile every changed template with a `manage.py shell -c` loop over `get_template`.
  - `scripts/verify_server_image.sh`, because the protocol is imported by the server image and packaged into the ZIP.
- **Checkpoint compatibility** (read-only shell): `checkpoint_from_json` on a dict copied from a real HITL checkpoint that has `person.how_to` and `person_guide`. It parses, `checkpoint_to_json` of the result has neither key, and no warning is logged.
- **HITL, manual smoke check** (dev stack, Daphne with the size flags, `assist` and `execution` workers):
  - Start a HITL run.
  - The "Your turn" card shows instruction, case steps and command, with no How to do it and no Ask section.
  - The output field appears only for a case-stated command.
  - Report As expected; the confirm sub-agent passes and the next step is handed out.
  - The case log has no `[Guide]` / research lines.
  - Flower shows no task on `assist` during the run.
- **Manual, manual smoke check:**
  - Claim a case from the runner; no task is queued.
  - The case card shows steps without how-to or questions.
  - Pause, then Discuss: the thread opens, `get_manual_case` answers without guide or Q&A sections, and the brief has no "at claim" machine block.
  - Hand in; TestRail comment preparation is unchanged.
- **Old runner (0.16.0, unchanged):**
  - Connect it; the handshake is accepted.
  - The HITL step shows in the window with no How to do it and no Ask box, and answering works.
  - The Manual tab shows steps with no how-to and no Ask box; claim, mark, hand in and upload work.
  - Pause and Discuss work.
  - The Environment card offers Update Test Runner.
- **New runner (0.17.0 after Update):** the same flows. `python -m controller_client.diagnose --window --no-interactive` passes. The handshake capabilities no longer list the three retired names.
- **Readiness:** Scan and Check machine still complete through `assist-celery` at concurrency 2.
- **Agents:** run `reviewer` (design, resources) and `verifier` (with this plan and the original request) in parallel.

## Open questions
None. The decisions this draft made, and why:
- The `assist` queue is kept for Scan/Check, with concurrency lowered to 2.
- No refusal handler for old-runner questions: they cannot send one.
- The claim-time OS is dropped, and Discuss uses the live TEST MACHINE info.
- `knowledge_base.related_knowledge` is deleted as dead code.
- `CLIENT_VERSION` is bumped to 0.17.0 so runners are offered the slimmer bundle.
- No export of the dropped data; `dumpdata` is listed as an optional deploy step.

## Changelog
- 2026-11-05: created
- 2026-11-05: implemented. Review: `assist-celery` concurrency set to 4, not 2 (Scan/Check are I/O bound and hold a worker up to ~90 s while a person waits; 2 would queue a third project). `MANUAL_RUN_STATE_SAFE_BYTES` comments reworded: the server no longer measures the manual snapshot (the guide's `_fits` was the only estimate; the base snapshot was never bounded, so no regression). Not run locally: `sqlmigrate projects 0056` (no local Postgres) and the manual/old-runner smoke checks; do them on deploy.
