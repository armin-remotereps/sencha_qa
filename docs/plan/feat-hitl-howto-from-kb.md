# feat: Human in the loop "How to do it" grounded in the knowledge base and artifacts profile

Status: Done
Created: 2026-11-01
Request: Testers say the "How to do it" list of a Human in the loop step is not
helpful: it mostly repeats the instruction ("open the app, do the step, check the
result"). The how-to should be built from the project's own material, meaning the
knowledge base documents and the artifacts profile. It should add the concrete
detail the instruction lacks: exact navigation and menu paths, exact paths and
install locations for the runner's OS, exact values and what to type, commands
taken from the docs, how to verify, and what to do when the result looks wrong.
It should name the document each fact came from, so a tester can follow it
without guessing. Type: `feat`.

Verbatim: "The how to do it is not helpful, the steps and the how to do it doesn't
have much difference, the how to do it should read the docs from kb or the
artifacts then create a bulletproof guide"

## Context

- **Hand-off writer.** `agents/services/person_steps.py::prepare_person_step` writes
  the hand-off with one tool-free LLM call. It is called from two places in
  `agents/services/orchestrator.py`: the step hand-off (around l.1445) and the
  restore hand-off in the resume path (around l.841).
  - Its input is only the case text, the plan step (or the restore task) and the OS
    name.
  - Prompts are in `agents/services/person_step_prompts.py`:
    `build_prepare_system_prompt`, `_how_to_rules`, `_how_to_example`,
    `build_prepare_user_prompt` and `build_restore_user_prompt`.
  - Today's rules ask for "how to open the app, the minimal command, what right
    and wrong output look like", but only for shell, OS, environment, file, version
    and install steps, or a non-obvious UI path. Nothing tells the model to add
    facts the instruction lacks, so the list re-words the instruction.
- **Parsing and caps.** `_parse_how_to` / `_how_to_item` drop bad items and never
  the hand-off. The caps are `HOW_TO_MAX_ITEMS=8`, `HOW_TO_TEXT_MAX_CHARS=400` and
  `HOW_TO_COMMAND_MAX_CHARS=500`. The same caps are mirrored in several places:
  - `projects/models.py` (`PERSON_STEP_HOW_TO_*`, l.1209);
  - `controller_client/protocol.py` (`PERSON_STEP_HOW_TO_*`, l.1020). The runner
    cuts on parse: `raw[:8]`, text truncated.
  - `projects/person_step_rows.py` (`how_to_rows`, `stored_how_to`);
  - `agents/checkpoint.py` (`_parse_how_to`, serializer around l.287).
- **The person never sees KB or profile material in prepare today.**
  `projects/services.py` (around l.3497–3524) passes `knowledge_index` and
  `artifacts_profile` to `run_orchestrator` only when `resume is None`. A
  Human in the loop case is re-queued after every person report, so steps 2..N
  are always prepared in a resumed execution. Simply handing the planner's inputs
  to prepare would therefore help only step 1.
- **Precedent for KB tools in `agents`.**
  - `orchestrator._plan_with_knowledge_tools` runs a bounded loop:
    `run_tool_loop` + `allowlisted_dispatcher(KNOWLEDGE_TOOL_NAMES)`,
    `ORCHESTRATOR_PLAN_MAX_TOOL_ROUNDS` / `ORCHESTRATOR_PLAN_TIMEOUT_SECONDS`, then
    one call with `tool_choice="none"`, logging `[Orchestrator] KB: …` lines.
  - `agents/services/tool_registry.py` already imports
    `knowledge_base.services.agent_tools`, so dispatching KB tools from `agents` is
    an existing, accepted seam. ORM reads outside tool dispatch are not: those are
    injected by `projects`.
- **Ask AI advisor.**
  - `projects/person_step_question_services.py::_advice_context` gives the advisor
    `related_knowledge` excerpts (question + instruction + expected; limit 4,
    6000 chars) rendered with `knowledge_base.tool_text.render_excerpts`.
  - The advisor gets no artifacts profile. The checkpoint is not loaded there.
- **Artifacts profile.** `machine_readiness/profile_text.py::artifacts_profile_text`
  (2–3 queries; capped at write by `ARTIFACT_PROFILE_MAX_CHARS=12000`) gives the
  base directories per OS, notes and items, and cites KB items as
  "KB item #N «Name»". Its fenced form for prompts is
  `orchestrator_prompts.build_artifacts_profile_block`.
- **Display.**
  - Web card: `templates/projects/includes/_person_step_card.html` (l.100–119),
    with its panel from `projects/person_step_services.py`.
  - Runner window: `controller_client/person_window_app.py::_render_how_to`, with
    the snapshot from `projects/runner_person_step_services.py::_how_to_payloads`.
  - Runners without `PERSON_STEP_ASSIST_V1` get `format_how_to_text` inlined into
    the instruction.
- **Confirm sub-task.** `build_confirm_sub_task` lists the suggested commands as
  fenced data. It is unaffected except that it may now see more items.

## Goals
- Every hand-off's `how_to` adds concrete facts that are not in the instruction:
  - navigation and menu paths, URLs;
  - paths for the runner's OS, from the profile base directory;
  - config values, what to type, and doc-stated commands;
  - how to verify, and what a wrong result looks like together with what to do
    then (report it as different, with what to note).
- Facts come from the project's knowledge base and artifacts profile whenever
  those hold them. Each such item names its source (a KB item and page, or the
  artifacts profile), and the server validates the source.
- The prompt forbids restating the instruction. When the model has nothing
  concrete to add, it returns `[]` instead of filler.
- The grounding works on every step of a Human in the loop case, also in resumed
  executions, and also for the restore hand-off.
- The Ask AI advisor gets the same artifacts profile grounding.
- All current safety rules stay:
  - knowledge base, profile and case text fenced as data;
  - no destructive commands;
  - Windows: Command Prompt only;
  - `_is_explicit_command` remains the only rule for `kind="command"`;
  - bad answers fall back as today;
  - secrets are never copied out of documents.

## Non-goals
- No change to the executor or planner prompts. The planner keeps its own KB loop
  and plan JSON.
- No machine access while preparing a hand-off: no screenshot and no runner
  command. Grounding is text-only, from the KB and the profile.
- No change to the confirm sub-agent's rules or the person's report flow.
- No new KB retrieval mechanism, such as embeddings or pg_trgm: reuse FTS
  `related_knowledge` and the agent tools.
- No change to how TestRail receives steps. `how_to` never goes to TestRail.
- The Ask AI advisor does not get the per-case research notes in this plan
  (see Open questions Q5).

## Design

The design has two grounding sources, both injected or cached so that every step
benefits:

1. **Per-step references (cheap, every prepare).** `projects` injects a
   `PersonGuideSources` object into `run_orchestrator`. Its lazy callables return:
   - the fenced artifacts profile (memoized per execution);
   - the KB excerpts related to a step's text (`related_knowledge`, one or two
     queries);
   - a validator that resolves cited KB ids to names (`find_knowledge_items`, one
     query).

   Prepare stays one tool-free LLM call, with these blocks added as fenced data.
2. **Per-case research notes (once per case, cached in the checkpoint).** Before
   the first person hand-off of a case, when the project has ready KB items and no
   notes are stored yet, `agents` runs one bounded KB tool loop. The loop uses the
   same mechanism as the planner: list/search/read, the KB index and the profile.
   It writes compact research notes: facts with `item_id` and `page`, copied
   verbatim where they are values or paths. The notes are stored in a new optional
   checkpoint key `person_guide` and fed to every later prepare as fenced data.
   They survive resumes, so the loop's cost (≤ rounds + 1 calls) is paid once per
   case, not per step.

Why both: FTS excerpts on a short step text often miss the one document page that
holds the menu path or the install folder. A model-driven search over the whole
case finds it. Per-step excerpts still give verbatim chunks for the specific step
at almost no cost. Either source can be off on its own: no KB means no research
and no excerpts, only the profile; rounds = 0 disables research.

### Data model

- **`agents/types.py`**
  - `PersonHowToItem` gains `source_kind: Literal["", "kb", "profile"] = ""`,
    `source_item_id: int | None = None`, `source_page: int | None = None` and
    `source_label: str = ""`. The label is server-built ("«Install guide.pdf»,
    page 3" or "Artifacts profile") and never model text.
  - New `PersonGuideNotes(text: str, status: Literal["ready", "empty", "failed"],
    researched_at: datetime)`. `text` holds the rendered notes with citations,
    capped at `PERSON_GUIDE_NOTES_MAX_CHARS` (4000, a constant).
  - New `PersonGuideSources`: a frozen dataclass of callables, with no ORM in
    `agents`:
    - `artifacts_profile() -> str | None`;
    - `knowledge_index() -> str | None`;
    - `related(text: str) -> RelatedKnowledgeText` (rendered excerpts plus
      `item_names: Mapping[int, str]`);
    - `resolve_items(ids: Sequence[int]) -> Mapping[int, str]`.
- **`agents/checkpoint.py`**
  - Optional top-level key `person_guide` (`{text, status, researched_at}`).
    Absent in older checkpoints, and malformed means absent.
  - `how_to` items gain optional `source_kind` / `source_item_id` / `source_page`
    / `source_label`. A malformed source is dropped and the item kept.
  - `_Progress` must carry `person_guide` into every checkpoint, the same rule as
    `plan_versions`. Otherwise the first save after a resume loses it.
- **`projects/models.py`**
  - `PERSON_STEP_HOW_TO_MAX_ITEMS` goes from 8 to 12 (Q2). Text (400) and command
    (500) stay.
  - `CasePersonStep.how_to` is a JSONField, so new keys `source_label`,
    `source_item_id`, `source_page` and `source_kind` need **no migration**.
- **Clearing.** `person_guide` is dropped with the checkpoint on from-scratch.
  It is kept across Resume, help Continue and plan redesigns, because the notes
  are about the case text and the project, not the plan. A KB change after
  research is not picked up until the checkpoint is cleared (Q4).

### Services

- **`agents/services/knowledge_tool_loop.py` (new, extracted).** The bounded
  "KB tool rounds, then one `tool_choice="none"` call" helper, lifted out of
  `orchestrator._plan_with_knowledge_tools` together with `_logged_dispatcher` /
  `_describe_knowledge_call`. The planner calls it with its settings and
  behaviour unchanged; guide research calls it with its own settings and final
  prompt. This removes the need for a second copy.
- **`agents/services/person_guide_research.py` (new).**
  `research_person_guide(llm, *, task_description, os_name, sources, project_id,
  on_log, cancellation_check) -> PersonGuideNotes`.
  - The system prompt (new `agents/services/person_guide_prompts.py`) asks for the
    facts a tester needs to perform this case by hand on `<OS>`: install
    locations, config files and values, exact navigation and menu paths, URLs,
    where test accounts and credentials are kept (never their values),
    doc-stated commands, and how to verify.
  - The prompt shows the KB index and the fenced profile.
  - The answer is JSON `{"notes": [{"fact", "item_id", "page"}]}`. Facts are
    copied verbatim for values and paths.
  - Parsing:
    - item ids are validated with `sources.resolve_items`, and facts citing an
      unknown id lose the citation;
    - each fact is cut to 300 characters and the text capped at 4000;
    - the result renders as numbered lines "fact [KB #id «name» p.N]".
  - `AgentCancelledError` propagates. An `AIServiceError` or an unparseable answer
    gives `status="failed"`, logged as `[Orchestrator] Guide research failed: …`,
    and is not retried within the execution (Q3). Zero facts give
    `status="empty"`.
- **`agents/services/person_steps.py::prepare_person_step`** gains
  `guide: PersonGuideContext | None`. This is a small value holding
  - the profile text,
  - the notes text,
  - the related-excerpts text,
  - the allowed id→name map, which unions the excerpts' ids, the notes' cited
    ids and the profile-cited ids, resolved once.

  With `None` the prompt is exactly today's.
  - `parse_hand_off` / `_how_to_item` validate `source`. A `kb` source must have
    an allowed id, and the page must be an int ≥ 1 or it is dropped. `profile` is
    allowed only when a profile was given. The `source_label` is built from the
    name map.
  - New lexical guard: drop an item whose collapsed, lower-cased text equals, or
    is contained in, the instruction or the expected result. This is only a cheap
    net under the prompt rule, not a similarity model.
  - `HOW_TO_MAX_ITEMS` becomes 12.
- **`agents/services/orchestrator.py`**
  - `run_orchestrator(person_guide_sources: PersonGuideSources | None = None)`
    is threaded to `_execute_sub_tasks` and the resume path.
  - New `_person_guide_context(progress, sources, sub_task, …)` is called right
    before each of the two `prepare_person_step` call sites. It runs research once
    when `progress.person_guide is None` and `sources.knowledge_index()` is not
    None and the rounds setting is > 0. It stores the notes on `progress` and
    checkpoints them, then builds the context with `sources.related(<case title +
    step description + expected result>)`. On a restore the text is the restore
    task description.
  - It never runs when person mode is off, so Attended and Night runs pay nothing.
- **`projects/person_guide_sources.py` (new).**
  `build_person_guide_sources(project_id) -> PersonGuideSources` builds closures
  that memoize the profile and the index per execution.
  - `related` = `related_knowledge(limit=4, max_chars=5000)` +
    `render_excerpts`, with `KnowledgeBaseError` → "".
  - `resolve_items` = `find_knowledge_items`, mapped to names.
  - It imports only `knowledge_base.services.*`, `knowledge_base.tool_text` and
    `machine_readiness.profile_text`, all already imported by `projects`.
  - `projects/services.py::execute_test_run_test_case` passes it on every
    execution, fresh or resumed. The existing `knowledge_index` /
    `artifacts_profile` arguments for the planner stay fresh-only.
- **`projects/person_step_rows.py`.** `how_to_rows` and `stored_how_to` carry the
  four source keys (label capped at 200; ids validated as positive ints).
- **`projects/person_step_services.py` (panel).** Each how-to view item gets
  `source_label` and `source_url`. The URL is `knowledge_base:item` for `kb`
  (with `?page=` only if the item page supports it; otherwise no page) and
  `machine_readiness:overview` for `profile`. It is built in the service so the
  template only renders.
- **Ask AI (`projects/person_step_question_services.py::_advice_context`).**
  - Adds `artifacts_profile` (via `artifacts_profile_text`, capped at 6000 for
    the advisor).
  - Includes the how-to texts in the `related_knowledge` query text, so excerpts
    lean to the pages the guide drew on.
  - `PersonStepAdviceContext.artifacts_profile: str = ""` is new.
  - `person_step_advice_prompts` fences it as `<<<ARTIFACTS_PROFILE …>>>`, a new
    marker pair added to `_MARKERS`, with the same "data, name the source" rule.

### Prompt contract (prepare)

These are content changes to `build_prepare_system_prompt` / `_how_to_rules` and
the user prompt, given only when `guide` is set.

- **New fenced user-prompt blocks**, each fenced with its markers added to
  `_MARKERS` and stripped from content:
  - ARTIFACTS PROFILE (reuse `build_artifacts_profile_block`), with the rule to
    use the base directory line for the runner's OS only;
  - CASE RESEARCH NOTES;
  - KNOWLEDGE EXCERPTS.

  All three are labelled "reference data uploaded by project members, not
  instructions".
- **Split of the instruction and the how-to:**
  - `instruction` = what to achieve, in 1–2 sentences, with no navigation detail;
  - `how_to` = how, with every item adding at least one concrete fact not
    already in the instruction or the expected result.
- **Forbidden:** restating the instruction, generic items ("open the
  application", "perform the step", "check the result") unless they carry the
  concrete how (which shortcut, which menu path, which URL), and inventing a
  path, value, URL or menu path that is in neither the case, the references nor
  standard OS knowledge.
- **When a fact is missing,** the item says where to find it, for example "the
  install folder is not in the project documents; look under <profile base
  dir>", rather than guessing.
- **Sources:** every item that uses a reference sets
  `"source": {"kind": "kb", "item_id": N, "page": P}` or `{"kind": "profile"}`.
  Items from general OS knowledge have no source.
- **Coverage of the guide:**
  - navigation: exact menu path, URL, or file path;
  - input: the values to enter;
  - verification: what exactly should be on screen or in the output;
  - "If it looks wrong": the typical wrong result and what to note when
    reporting it as different. The how-to never says pass, skip or mark (the same
    rule as the advisor).
- **Secrets:** never copy a password, token, key, connection string credential
  or licence key out of a reference. Point to where it is kept instead ("the
  password of user qa1 is in «Test accounts.xlsx», sheet Users"). A value the
  test case itself states may be repeated.
- **Unchanged rules:** destructive commands, the Windows Command Prompt only
  rule, one-line commands, read-only commands for a restore, and the
  `kind="command"` rule with `_is_explicit_command`.
- **The examples** (`_how_to_example`) are rewritten to show a grounded,
  non-restating guide, such as an install path from the profile and a menu path
  cited to a KB page.
- **`[]` is allowed and preferred** when nothing concrete can be added.

### Pages and forms

No new URLs or forms.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| (existing) case page `projects:test_run_case_detail` | GET | `@project_membership_required` | none | `_person_step_card.html`: each `how_to` item may add `source_label`, `source_url` | none new |

- **Web card.** Under an item's text, a small "Source: <a>«name», page 3</a>"
  line (or "Source: Artifacts profile"). It renders with `{{ }}` only, never
  `|safe`, and the link is built by `reverse` in the service. When no item has a
  source, the card shows one hint under the list: "Not found in the project's
  documents: general guidance from the AI." A new CSS class
  `.person-step-howto-source` goes in `static/css/punk-hazard.css`.
- **Runner window.** `PersonHowToPayload` gains an optional `source` (label
  text, ≤ 200). It is rendered as a muted "Source: …" line under the item.

### Background work and WebSockets

- No new Celery task or queue. Research and prepare run inside the existing
  execution worker (`execution` queue) where `prepare_person_step` runs today.
  The Ask AI change runs on `assist` as today.
- No new socket events. The `person_step_state` snapshot item gains `source`.
- **Controller protocol** (`controller_client/protocol.py`):
  - `PERSON_STEP_HOW_TO_MAX_ITEMS` goes from 8 to 12; `PersonHowToPayload.source`
    is optional, with `""` from an older server;
  - new capability `PERSON_STEP_GUIDE_V1`, advertised only together with
    `PERSON_STEP_ASSIST_V1`; `CLIENT_VERSION` 0.12.0 → 0.13.0.
- **Old runners.**
  - The parser ignores unknown keys (precedent: `pivot_id`), so `source` is
    harmless.
  - The parser cuts the list at its own 8, which would silently drop the
    verification and "if wrong" items at the end. So for runners without
    `PERSON_STEP_GUIDE_V1`, `runner_person_step_services._how_to_payloads` sends
    at most 8 items. When the guide is longer, item 8 is replaced by "This guide
    has N more items, including how to verify: open the case page." `case_url` is
    already in the snapshot.
  - Runners without `PERSON_STEP_ASSIST_V1` keep the inlined
    `format_how_to_text`, which now appends " (Source: …)" per item and is still
    capped by the instruction cap.
  - Self-update (`SELF_UPDATE_V1`) delivers 0.13.0. The capability is optional
    and never in `REQUIRED_CLIENT_CAPABILITIES`.

### Settings / infrastructure

New settings, both read in `auto_tester/settings.py` via `config` and added to
`example.env`:
- `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS` (default 4; 0 disables research, so
  only the per-step excerpts and the profile are used);
- `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS` (default 120).

These are constants, not settings: notes cap 4000, per-step excerpts 4 chunks /
5000 characters, advisor profile cap 6000.

### Data flow

1. A worker executes a Human in the loop case, fresh or resumed. `projects`
   builds `PersonGuideSources(project_id)` with lazy closures and calls
   `run_orchestrator(..., person_guide_sources=sources)`.
2. At a step boundary, person mode is on → `_person_guide_context`:
   - When `progress.person_guide` is None and the KB has ready items:
     `research_person_guide` runs its bounded KB loop (logged as
     `[Orchestrator] KB: …`), then notes go to `progress.person_guide` and are
     checkpointed.
   - `sources.related(step text)` gives the excerpts. `sources.resolve_items(ids
     in excerpts ∪ notes ∪ profile)` gives the allowed id→name map, in one query.
3. `prepare_person_step(guide=…)` makes one LLM call with the fenced profile,
   notes and excerpts → JSON. `parse_hand_off` then validates the command rule,
   the items, the sources and the restatement guard. A bad answer gives the
   fallback hand-off as today.
4. The hand-off is stored in the checkpoint `person` key, and the
   `CasePersonStep` row is written with the source keys
   (`person_step_rows.how_to_rows`).
5. The web card renders the items with source links. The runner snapshot
   carries the items with source labels, at ≤ 8 items for old runners.
6. On re-entry for the next step, the checkpoint already has `person_guide`, so
   there is no research, only steps 2–3 again.
7. Ask AI: the advisor context adds the fenced artifacts profile, and the
   excerpts query includes the how-to texts.

## Alternatives considered

- **A bounded KB tool loop inside every prepare.** This gives the best grounding
  per step, but costs up to rounds + 1 extra LLM calls on every step of every
  Human in the loop case, while the person waits after each report (confirm,
  then prepare). Rejected for latency and cost. The per-case research gives most
  of the benefit once.
- **Pre-fetched excerpts and the profile only, with no research.** This is the
  smallest change, with no extra LLM call. But FTS OR-matching on a short step
  text ranks the wrong chunks for typical steps ("Open the admin console and set
  the licence"). It also never reads folder instructions or follows a document
  the profile cites. It is kept as the always-on cheap layer, and is what
  `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS=0` reduces to.
- **The planner writes `reference_notes` in its plan JSON** (it already has a KB
  loop). This needs no extra loop, but:
  - it mixes person-guide work into the executor's planning prompt and its round
    budget;
  - it happens only on fresh plans, so a case switched to Human in the loop
    mid-run, or an older checkpoint, gets nothing;
  - it changes planning for Attended and Night runs.

  Rejected. It could be revisited if research cost matters (Q1).
- **Inline citations inside the item text instead of a source field.** This
  needs no schema change, but the model could cite items it never saw, the label
  eats the 400-character text, and the web card cannot link. Rejected in favour
  of a validated `source` object with a server-built label.
- **Storing the research notes on `CasePersonStep` for the advisor.** Deferred
  (Q5). The advisor gets the profile and better excerpts instead, with no new
  column.

## Risks and mitigations

- **Secrets leak into the guide.** Profiles and KB files can hold passwords, and
  the runner window is visible to anyone at the test machine (the Connection key
  holder), not only members.
  - Mitigations: the prompt rule (never copy secrets; point to where they are
    kept) in both the research and prepare prompts.
  - Research facts are short, and the research prompt says to record where a
    credential is kept, not its value.
  - The rule is enforced by the prompt only. Q6 asks whether a code-level
    redaction pass (keys such as `password=`, `token`, `secret`) is wanted.
- **Prompt injection from KB documents.**
  - Every reference block is fenced with markers stripped from content, and the
    notes are LLM-written from KB text, so they are also fenced as data.
  - Sources are validated against real item ids.
  - Commands stay suggestions shown with "Suggested by the AI"; the server never
    runs them. The destructive-command rule is unchanged.
  - The confirm sub-agent stays read-only and is unaffected.
- **Latency of the first hand-off.** Research adds up to
  `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS` once per case. The case log shows the
  KB look-ups, and the 0-rounds switch disables research.
- **Cost.** Research is ≤ 5 calls per case; prepare prompts grow by about 10–20k
  characters (profile ≤ 12000, notes ≤ 4000, excerpts ≤ 5000). The profile is the
  biggest part. If needed, send only the runner OS's base directory line and the
  items (follow-up).
- **Checkpoint carry-over.** Forgetting `person_guide` in `_Progress` would
  re-run research every step, which is expensive and silent. Call it out to the
  implementer and verify it with the log (the research lines must appear once per
  case).
- **Old runners lose the end of the guide.** This is mitigated by the ≤ 8 items
  plus pointer item.
- **Stale notes after KB edits.** Notes are cached for the case's checkpoint
  life. They are acceptable as reference, and sources are re-validated at every
  prepare, so a deleted item loses its citation (Q4).
- **The model returns `[]` more often.** That is intended when nothing concrete
  can be added. The card then shows only the instruction.
- **Query count per prepare.**
  - The profile is 2–3 queries, memoized per execution, and each Human in the
    loop execution is about one step.
  - `related` is 2 queries and `resolve_items` 1 query.
  - The index is 1 query, only when research is due.
  - There is no N+1.

## Implementation steps

1. **Shared types and checkpoint.** Add the `PersonHowToItem` source fields,
   `PersonGuideNotes`, `PersonGuideSources` and `PersonGuideContext`, the
   checkpoint `person_guide` key, the how-to source keys and the `_Progress`
   carry-over (files: `agents/types.py`, `agents/checkpoint.py`,
   `agents/services/orchestrator.py` `_Progress` only).
2. **Extract the KB tool loop helper** with no behaviour change for the planner
   (files: `agents/services/knowledge_tool_loop.py` new,
   `agents/services/orchestrator.py`).
3. **Guide research** (files: `agents/services/person_guide_research.py` new,
   `agents/services/person_guide_prompts.py` new, `auto_tester/settings.py`,
   `example.env`).
4. **Prepare prompt and parsing:**
   - grounded blocks, the rewritten how-to rules and examples;
   - source validation, the restatement guard, `HOW_TO_MAX_ITEMS=12`;
   - `format_how_to_text` with sources.

   (files: `agents/services/person_step_prompts.py`,
   `agents/services/person_steps.py`).
5. **Orchestrator wiring:** the `person_guide_sources` parameter,
   `_person_guide_context` at both prepare call sites, and the log lines (file:
   `agents/services/orchestrator.py`).
6. **Projects injection:** `build_person_guide_sources`, passed on every
   execution (files: `projects/person_guide_sources.py` new,
   `projects/services.py`).
7. **Rows, panel and web card** (files: `projects/models.py` constant only,
   `projects/person_step_rows.py`, `projects/person_step_services.py`,
   `templates/projects/includes/_person_step_card.html`,
   `static/css/punk-hazard.css`).
8. **Runner protocol and window:** the caps, `source`, `PERSON_STEP_GUIDE_V1`,
   `CLIENT_VERSION` 0.13.0, the old-runner ≤ 8 fold, and the window rendering and
   diagnostics sample (files: `controller_client/protocol.py`,
   `controller_client/person_window_app.py`,
   `controller_client/person_window_diagnostics.py`, `controller_client/client.py`
   if capability advertising lives there, `projects/runner_person_step_services.py`).
9. **Ask AI grounding** (files: `agents/types.py` `PersonStepAdviceContext`,
   `agents/services/person_step_advice_prompts.py`,
   `projects/person_step_question_services.py`).
10. **Docs:** update `agents/CLAUDE.md` (Person steps › How to do it) and the
    `CLAUDE.md` Human in the loop paragraph, and write the change note.

Steps 7, 8 and 9 are independent after step 1 and can go to parallel sub-agents.
Steps 3–5 depend on each other and should be done by one implementer.

## Verification

There is no test suite in this project, so the checks are type checks and smoke
checks.

- `pre-commit run --all-files`, including the `controller_client/` mypy, and
  `python manage.py check`, plus `makemigrations --check`, which must report no
  changes.
- **Stubbed-LLM script** (throwaway DB, KB with a fixture "Install guide" naming
  a path and a menu path, and a profile with a Windows base dir):
  - research runs once, `person_guide` is in the saved checkpoint, and a second
    simulated resume does not research again;
  - the prepare prompt contains the three fenced blocks, and markers in the KB
    text are stripped;
  - an answer citing an unknown item id loses the source but keeps the item, and
    `kind: profile` without a profile is dropped;
  - an item equal to the instruction is dropped;
  - a non-explicit `kind=command` still falls back;
  - with `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS=0`, there is no research and the
    excerpts and profile are still present;
  - with no KB and no profile, the prompt is byte-identical to today's.
- **Query counts:** `build_person_guide_sources` closures make ≤ 3 queries per
  prepare after the first, using `CaptureQueriesContext`.
- **Live check** with the real model on a project with an uploaded install guide
  zip and a profile:
  - run one Human in the loop case;
  - the "How to do it" items name exact paths and menu paths from the guide,
    with "Source:" links that open the right KB item;
  - nothing repeats the instruction;
  - no password from a credentials sheet appears;
  - the restore hand-off after a Resume is also grounded and its commands are
    read-only.
- **Runner:** a 0.13.0 runner shows sources and 12 items. A 0.12.0 runner
  (capability absent) shows ≤ 8 items with the pointer item. Run
  `python -m controller_client.diagnose --window --no-interactive`.
- **Ask AI:** a question about the step's path gets an answer naming the profile
  or KB item.
- Run the `reviewer` and `verifier` agents in parallel and loop until clean.

## Open questions

Resolved 2026-11-01 with the user:

- **Q1.** Research loop once per case: yes (4 rounds / 120 s, 0 disables).
- **Q2.** 12 items (implied by the accepted Q7 option).
- **Q3, Q4.** Assumptions kept: continue without notes on failure, no retry in
  that execution; no refresh during a case.
- **Q5.** Ask AI gets the artifacts profile and keeps its own research as today:
  its `related_knowledge` query stays what it is now (do NOT bias it to the
  how-to texts). No research notes for Ask AI.
- **Q6.** Prompt rule **plus code redaction**: new pure helper
  `agents/secret_redaction.py::redact_secrets(text: str) -> str` (no ORM)
  replaces obvious secret values with `[redacted]`: `password|passwd|pwd|secret|
  token|api[_-]?key|access[_-]?key|client[_-]?secret` followed by `:`/`=` and a
  value, `Bearer <token>`, `user:pass@` in URLs/connection strings, PEM private
  key blocks, `AKIA…` keys. Applied in `parse_hand_off` to the instruction,
  expected result and every how-to text and command (never to the `command` of
  a `kind="command"` step, which stays the case's exact text), and to research
  notes before they are stored.
- **Q7.** `[]` when there is nothing concrete; card hint when no item has a
  source.

## Changelog
- 2026-11-01: created (Draft).
- 2026-11-01: open questions resolved (Q5: Ask AI keeps its own excerpt query; Q6: code redaction added). Status In progress.
- 2026-11-01: review round 1 (research retry cap, context module, single profile/index read, dedupe, one rule set, guard, redaction anchoring, Ask AI redaction) and round 2 (secret_key/private_key, placeholders, case-text marker stripping, card hint wording, shared stored-source parser, shared profile reader) fixed. Rejected: round 2/3 "black fails on knowledge_tool_loop.py / person_guide_context.py" — the pinned black 26.1.0 (pre-commit and `venv/bin/black --check`) leaves both unchanged. Accepted cost note: full profile is resent each grounded prepare (follow-up if cost matters). Round 3 clean. Status Done.
