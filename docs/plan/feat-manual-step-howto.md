# feat: "How to do it" for Manual run steps

Status: Done
Created: 2026-11-01
Request: On a Manual run, every verbatim case step the tester sees should get the
same "How to do it" list Human in the loop steps have: grounded in the project's
knowledge base (one research pass per case plus per-step excerpts) and the
machine readiness artifacts profile, OS- and shell-specific for the runner,
with validated sources, secret redaction and Copy for suggested commands. It is
shown in the Test Runner's "Manual run" tab and read-only on the web case card,
and Ask AI on a manual step sees it too. The HITL generator and its one rule
set are reused, not forked. The step texts and verdicts stay verbatim (the AI
never rewrites them), and claiming a case must never wait on the AI.

## Context

- Manual run mode: `docs/changed/2026-11-01-manual-run-mode.md`,
  `docs/plan/feat-manual-run-mode.md` (D12: Ask AI on manual steps "with no
  hand-off or how-to").
  - Claim: `projects/manual_case_claim_services.claim_manual_case` (run lock,
    writes the verbatim plan via `manual_case_steps.build_manual_checkpoint`
    into `TestRunTestCase.execution_checkpoint`, `manual=True`). Release clears
    the checkpoint; the hand-in (`manual_case_submit_services.py`) rewrites it
    with person attempts.
  - Steps: `projects/manual_case_steps.py` (`ManualStep`, up to
    `MANUAL_CASE_MAX_STEPS` = 100; free-text case = one step of up to 8000
    chars).
  - Window snapshot: `projects/runner_manual_state_services.build_manual_run_state`
    → `ManualRunStateMessage` / `ManualActiveCasePayload` / `ManualStepPayload`
    (`controller_client/protocol.py`), pushed by
    `controller_consumer._push_manual_state` (only when changed), nudged by
    `runner_person_step_push.nudge_runner_person_steps`.
  - Window: `controller_client/person_window_manual.py` (`ManualView`; the
    window, not the server, knows which step the tester is looking at:
    `_go_to`, `_move`, `_on_pick`).
  - Web: `templates/projects/includes/_manual_case_card.html` fed by
    `projects/manual_case_display_services.get_manual_case_panel`
    (`ManualStepRow`).
  - Ask AI: `projects/person_step_question_services.ask_manual_step_question`
    → task `answer_person_step_question` (`assist`) →
    `projects/manual_advice_mapping.manual_advice_step` → `AdviceStep(source="case")`
    → `projects/advice_context.build_advice_context`. `AdviceStep.how_to`
    already exists and the advisor prompt already renders it
    (`agents/services/person_step_advice_prompts.py`, "How to do it
    (suggested)"); Manual simply never fills it.
- HITL how-to (`docs/changed/2026-10-29-hitl-ask-ai-and-howto.md`,
  `docs/changed/2026-11-01-hitl-howto-from-kb.md`):
  - `agents/services/person_steps.prepare_person_step` → one tool-free LLM call
    with `person_step_prompts.build_prepare_system_prompt` (kind / instruction /
    expected rules + the shared `_how_to_rules`) and `build_prepare_user_prompt`
    (+ `_reference_blocks`); `parse_hand_off` → `_parse_how_to` (≤ 12 items,
    `_restates`, `_how_to_source` validated against `guide.allowed_items`,
    `redact_secrets(known_text=task_description)`).
  - `agents/services/person_guide_context.py`: `refresh_person_guide_notes`
    (once-per-case KB research, `MAX_RESEARCH_ATTEMPTS` = 2) and
    `build_person_guide_context` (profile + notes + per-step excerpts, cited
    ids resolved in one query).
  - `projects/person_guide_sources.build_person_guide_sources(project_id)`:
    lazy, memoized sources (no ORM in `agents`).
  - Storage/display: `projects/person_step_rows.how_to_rows` / `stored_how_to`,
    `agents/how_to_source.parse_stored_source`,
    `projects/person_step_services.PersonHowToView` + `_how_to_view` (source
    links), `templates/projects/includes/_person_step_card.html` ("How to do
    it" `<ol>` with Copy), runner payload `PersonHowToPayload` and
    `runner_person_step_services._how_to_payloads`.
  - Notes serialization is private today: `agents/checkpoint._person_guide_to_json`
    / `_parse_person_guide`.
- Queues: `assist` runs Ask AI answers, artifact scans and quick checks with
  `--concurrency=2` (`docker-compose.yml`); `execution` can be full of
  long-running cases; `upload` runs imports and the manual note rewrite.
- Controller protocol: `CLIENT_VERSION` 0.14.0; `MANUAL_RUN_V1` (0.13.0) and
  `PERSON_STEP_GUIDE_V1` (0.14.0) exist. Old parsers ignore unknown keys
  (`_parse_manual_step` reads only the keys it knows), which is how
  `PersonHowToPayload.source` was added.

## Goals

- Every manual step (up to a configurable cap) gets a "How to do it" list
  produced by the HITL rule set, grounded the same way (research once per case,
  per-step excerpts, fenced artifacts profile, validated sources, redaction,
  runner OS and shell).
- Claiming stays instant: generation is asynchronous, step 1 first, steps
  appear in the window as they are ready.
- Shown in the runner's Manual tab (with Copy and sources) and read-only on
  the web case card; Ask AI on a manual step gets the step's how-to.
- Never blocks or fails anything: any fault means "no how-to for that step".
- Old `MANUAL_RUN_V1` runners keep working unchanged.

## Non-goals

- Rewriting, summarizing or re-ordering the verbatim step texts, expected
  results or verdicts; the how-to never reaches TestRail, the hand-in, the
  manual note rewrite (`manual_result_refiner`) or the checkpoint.
- A "Regenerate" button, editing a how-to, or refreshing it when the KB or
  profile changes mid-case (same as HITL).
- Pre-generating guides before a case is claimed.
- Live web-card updates while the guide is being written (the card renders the
  state at page load).
- Trimming the per-call reference size (the HITL note's "full profile every
  step" cost remark applies; separate plan if cost matters).

## Design

### Key decisions

- **D1 When**: on claim, after commit (`transaction.on_commit`), never inside
  the claim. Research first, then steps in order (1, 2, …), a few in parallel.
  Not lazily per viewed step: the server does not know which step the window
  shows, and a lazy design would need a new runner → server message and make
  the tester wait 10–30 s at each step. Eager in-order generation has step 1
  ready about one research loop plus one call after the claim, and later steps
  long before the tester reaches them.
- **D2 Where it runs**: the `assist` queue, in short **chunks** (one task =
  the research, or one chunk of up to `MANUAL_GUIDE_PARALLEL` steps), each
  chunk re-enqueuing the next. `assist` has only 2 slots shared with Ask AI;
  chunking keeps one guide from holding a slot for minutes, so a question waits
  at most about one chunk (FIFO). No new queue or compose service (see Q1).
- **D3 Where it is stored**: a new model `ManualStepGuide`, one row per run
  case, not the checkpoint. The checkpoint is written by the claim, rewritten
  by the hand-in and cleared by a release under the run lock; a background
  writer there would race (lost updates) or have to take the run lock for every
  step. A separate row has one writer (the guide task), fenced by a generation
  counter.
- **D4 Reuse**: the row carries a fingerprint of what the guide depends on
  (case task text, verbatim steps, runner OS). A re-claim of the same run case
  (after Release, Needs re-test or a stuck-claim release) with an unchanged
  fingerprint reuses finished steps and the research notes and only writes the
  missing ones. A new run (re-run as new run) has new run cases and generates
  afresh.
- **D5 One rule set**: the HITL generator gains a "case step" variant that
  writes **only** `how_to`; the instruction is the verbatim step, so the
  `kind`/`instruction`/`expected_result`/`command` rules are left out, while
  `_how_to_rules`, `_reference_blocks`, `_parse_how_to`, `_restates`,
  `_how_to_source` and redaction are shared unchanged. The HITL prompt text
  stays byte-identical (verified by diffing the rendered prompts before and
  after).
- **D6 Runner delivery**: new optional capability `MANUAL_STEP_GUIDE_V1`
  (CLIENT_VERSION 0.15.0). Only such runners get `how_to` / `how_to_state` per
  step in `manual_run_state`. Older `MANUAL_RUN_V1` runners get today's
  content: the keys are sent empty and their parser ignores them (the step
  text cannot carry the guide: it is verbatim); the web
  card and the Environment page's Update Test Runner cover them (Q3).
- **D7 Failure**: research failure → steps are still written without notes
  (profile + excerpts, or ungrounded), as in HITL. A step whose call raises
  `AIServiceError` or returns an unusable answer is stored `failed` with no
  items; no retry within the generation (the next claim retries missing and
  failed steps). A lost task is detected by staleness at read time; no beat
  sweep.

### Data model

New in `projects/models.py` (migration `projects` 0054, additive, empty table):

- `ManualGuideState(TextChoices)`: `queued`, `researching`, `writing`, `done`,
  `stopped` (claim gone mid-way), `failed` (task-level error), `off`
  (`MANUAL_GUIDE_MAX_STEPS` = 0 at request time).
- `ManualStepGuide`:
  - `test_run_test_case` OneToOne → `TestRunTestCase`, `related_name="manual_step_guide"`, CASCADE.
  - `fingerprint` CharField(64): sha256 of `build_task_description(test_case)`,
    the checkpoint's verbatim steps (text + expected) and the runner OS name.
  - `generation` PositiveIntegerField: bumped by every (re)start; every task
    write is a conditional update on it.
  - `state` (choices above), `os_name` CharField(32) (shell label for display).
  - `step_count` PositiveSmallIntegerField: steps to write (min(steps,
    `MANUAL_GUIDE_MAX_STEPS`)).
  - `research` JSONField(default=dict): `PersonGuideNotes` in the checkpoint's
    `person_guide` JSON shape (helpers made public in `agents/checkpoint.py`).
  - `steps` JSONField(default=dict): `{"<num>": {"state": "ready"|"failed",
    "how_to": [rows]}}`, rows in `person_step_rows.how_to_rows` shape (≤ 12,
    capped, sources validated). A step absent from the dict is not written yet.
  - `created_at`, `updated_at` (auto; doubles as the heartbeat for staleness).
- Size: ≤ 30 steps × 12 items × ~1.1 kB ≈ 0.4 MB worst case by default; one
  row per claimed case.
- Not shown in admin beyond an optional read-only registration (skip unless
  trivial).

### Services

Agents (no ORM, no projects import):

- `agents/services/person_step_prompts.py`:
  - `_how_to_rules(..., audience: Literal["person_step", "case_step"] = "person_step")`:
    the `case_step` audience changes only the wording that names the HITL
    hand-off fields ("the STEP says WHAT; how_to says HOW"; "mark the step Fail
    with a note" instead of "report the step as different"; a step that states
    a command may have that same command in an item). Default keeps HITL text
    identical.
  - `build_case_step_how_to_system_prompt(*, os_name, guide)` → output format
    `{"how_to": [...]}`.
  - `build_case_step_how_to_user_prompt(*, task_description, step_text,
    expected_result, step_num, total_steps, guide)` → fenced TEST CASE, fenced
    "TEST CASE STEP n of N (verbatim, the tester sees it as written)", fenced
    EXPECTED RESULT, `_reference_blocks(guide)`.
- `agents/services/person_steps.py`: `prepare_case_step_how_to(llm, *,
  step_text, expected_result, step_num, total_steps, task_description,
  system_info, guide) -> tuple[PersonHowToItem, ...] | None` (None = unusable
  answer; `AIServiceError` propagates). Parses `how_to` with the existing
  `_parse_how_to` and a `_HowToContext` whose `restated` is the verbatim step
  text and expected result. A small public `parse_how_to_answer` wrapper may be
  added if the projects side should not reach private names (it should not).
- `agents/checkpoint.py`: rename `_person_guide_to_json` / `_parse_person_guide`
  to public `person_guide_to_json` / `person_guide_from_json` (callers
  updated); no format change.
- Reused as is: `person_guide_context.refresh_person_guide_notes` /
  `build_person_guide_context` / `research_due`, `person_guide_research`,
  `knowledge_tool_loop`, `agents/how_to_source.py`, `agents/secret_redaction.py`.

Projects:

- `projects/manual_step_guide_fingerprint.py` (leaf, pure): `guide_fingerprint(
  task_description, steps, os_name) -> str`.
- `projects/manual_step_guide_services.py`: `request_manual_case_guide(*,
  pivot, project) -> int | None` called inside `claim_manual_case`'s
  transaction (one line, after the checkpoint is built; the run lock is already
  held). Locks/creates the guide row (`select_for_update` / `get_or_create`),
  decides reuse vs restart: same fingerprint and `done` with every step
  present → nothing; same fingerprint otherwise → bump generation, state
  `queued`, keep ready steps and research; new fingerprint → reset steps and
  research. `MANUAL_GUIDE_MAX_STEPS` = 0 → `off`. On commit sends
  `projects.tasks.prepare_manual_case_guide` by name with `(guide_id,
  generation)`. Adds 2–3 queries to a claim; no AI.
- `projects/manual_step_guide_worker.py`: `run_manual_guide_chunk(guide_id,
  generation) -> None`, the task body:
  1. Load guide + pivot (`select_related("test_case", "test_run__project")`,
     `only(...)`); stop if the generation moved on, or the case is no longer
     `IN_PROGRESS` with a claim (state `stopped`, missing steps stay missing).
  2. If research is due (`research_due` and KB ready): state `researching`,
     `refresh_person_guide_notes` with `build_person_guide_sources(project_id)`,
     `build_llm_config()`, `cancellation_check` = claim still held and
     generation current (checked between tool rounds; `AgentCancelledError` →
     `stopped`). Store notes, re-enqueue, return.
  3. Otherwise take the next ≤ `MANUAL_GUIDE_PARALLEL` missing steps in order,
     build each step's `PersonGuideContext` in the **main thread**
     (`build_person_guide_context`, related text = case title + step + expected,
     the same as `orchestrator._step_related_text`; this queries), then run
     `prepare_case_step_how_to` calls in a bounded `ThreadPoolExecutor` (HTTP
     only, no ORM in threads, as `manual_refinement_services.refresh_manual_refinements`).
  4. Write the finished steps with one conditional update on `generation`
     (main thread), state `writing` or `done`, then on commit
     `nudge_runner_person_steps(project_id)`. Re-enqueue while steps are
     missing.
  Logs: step numbers, item counts, durations; never step text, how-to text or
  KB content.
- `projects/manual_step_guide_read.py` (read side): `load_manual_guide(pivot,
  *, os_name) -> ManualGuideView | None` (one query): checks the fingerprint
  against the current checkpoint/case/OS (a stale guide is not shown), maps
  `queued/researching/writing` older than the stale threshold to "unavailable",
  and returns per-step `PersonHowToItem`s (`stored_how_to`) and a per-step
  state `pending | ready | none`. Used by the snapshot, the case card and Ask AI.
- `projects/how_to_view.py` (new leaf): `PersonHowToView` and `how_to_view(item,
  project_id)` moved out of `person_step_services.py` (re-imported there, no
  behaviour change), so the manual card can use them without importing the
  person step services.
- `projects/manual_case_claim_services.py`: one-line hook to
  `request_manual_case_guide`.
- `projects/runner_manual_state_services.py`: for runners with
  `MANUAL_STEP_GUIDE_V1`, `_active_case` reads the guide (one more query, total
  ≤ 7) and fills `ManualStepPayload.how_to` (cut to the protocol caps; the
  `_how_to_payloads` fold is not needed: guide-capable windows take 12) and
  `how_to_state`; `ManualActiveCasePayload.shell_label` for the block title.
  Older runners: unchanged payload.
- `projects/manual_case_display_services.py`: `ManualStepRow.how_to:
  tuple[PersonHowToView, ...]`, `ManualStepRow.how_to_state`,
  `ManualCasePanel.how_to_shell_label`, `ManualCasePanel.guide_preparing`
  (one more query on the case page).
- `projects/manual_advice_mapping.manual_advice_step(pivot, step_num, *,
  how_to=())` passes the step's items into `AdviceStep.how_to`; the answer path
  in `person_step_question_services` (where `manual_advice_step` is called)
  loads them via `load_manual_guide` (one query). The whole-case question gets
  none.

### Pages and forms

No new URL, view or form.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| existing case page `projects:test_run_case_detail` | GET | `@project_membership_required` (read-only members included) | none | `_manual_case_card.html`: per step `step.how_to` (`PersonHowToView`: text, command, source_label, source_url), `step.how_to_state`; `panel.how_to_shell_label`, `panel.guide_preparing` | none; a missing or stale guide just shows nothing |

- Extract the HITL "How to do it" `<ol>` from `_person_step_card.html` into
  `templates/projects/includes/_how_to_list.html` (contract: `items`,
  `shell_label`, `has_sources`; documented in `templates/partials/README.md`
  convention or the include's header comment). The person step card includes
  it with identical output; the manual card includes it per step inside a
  collapsed `<details>` "How to do it" (Copy via the existing `clipboard()`
  Alpine component). All text auto-escaped; no `|safe`.
- While the guide is being written: one hint "A how-to for each step is being
  prepared; reload to see it." Failed or empty steps show nothing. With items
  but no sources: the existing "General guidance from the AI" hint.

### Background work and WebSockets

- Celery task `projects.tasks.prepare_manual_case_guide(guide_id: int,
  generation: int)`, queue `assist` (route in `settings.CELERY_TASK_ROUTES`),
  `max_retries=0`, `soft_time_limit = max(PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS,
  MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS) + 15`, hard limit + 45. Thin: calls
  `run_manual_guide_chunk`. Task name constant next to the others; sent by
  name from the services (no import cycle).
- No new consumer or channel event: after each chunk the existing
  `PERSON_STEPS_CHANGED_EVENT` nudge makes the consumer re-read and push
  `manual_run_state` (already coalesced to one push per second and sent only
  when changed).
- Controller protocol (`controller_client/protocol.py`):
  - `ClientCapability.MANUAL_STEP_GUIDE_V1 = "manual_step_guide_v1"`, in
    `_PERSON_WINDOW_CAPABILITIES` (advertised only with the window, together
    with `MANUAL_RUN_V1`); `CLIENT_VERSION` 0.15.0.
  - `ManualStepPayload.how_to: tuple[PersonHowToPayload, ...] = ()` and
    `how_to_state: str = ""` (`"pending" | "ready" | "none"`, unknown values
    read as `""`); `ManualActiveCasePayload.shell_label: str = ""`.
    `to_wire` emits them; `_parse_manual_step` / `_parse_manual_active` read
    them leniently (absent from an older server → defaults), reusing
    `_parse_how_to` (caps 12 / 400 / 500 / 200).
  - Size: by default ≤ 30 guided steps; worst case ~0.4 MB, far below
    `MAX_MESSAGE_SIZE` (10 MiB). The snapshot is only re-sent when it changes.
- Window (`controller_client/person_window_manual.py`): a "How to do it ·
  <shell>" read-only block under the Step block for the shown step, with one
  Copy button per suggested command and "Source: …" lines; "Preparing a guide
  for this step…" while `pending`; hidden for `none` or empty. Re-rendered only
  when the shown step's items change. The rendering of items into segments and
  Copy buttons is extracted from `person_window_app._render_how_to` into a
  shared helper (`controller_client/person_window_how_to.py`) used by both tabs.
  Rendering never raises or focuses the window.
- Old runners: `MANUAL_RUN_V1` without `MANUAL_STEP_GUIDE_V1` get no how-to
  fields (D6). Manual runs still start with them (no new required capability).

### Settings / infrastructure

New in `settings.py` (python-decouple, validated at startup) and `example.env`:

- `MANUAL_GUIDE_MAX_STEPS` (default 30; 0 disables the feature; ≤
  `MANUAL_CASE_MAX_STEPS`). Steps beyond it get no guide (Ask AI still works).
- `MANUAL_GUIDE_PARALLEL` (default 3; 1–8): prepare calls per chunk.
- `MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS` (default 120): one chunk's budget; the
  stale threshold is `max(PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS, this) + 60`
  seconds without an `updated_at` bump.

Reused: `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS` (0 disables research for both
modes), `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS`, the `OPENAI_*` LLM settings.
No compose change (the `assist-celery` worker exists).

Cost per claimed case (first claim): one research loop (≤ 4 tool rounds, only
with ready KB items) + one prepare call per step up to the cap, each with up to
~22k characters of references (same as one HITL hand-off). A re-claim costs
only the missing/failed steps.

### Data flow

1. Tester presses Start in the Manual tab → `manual_case_action(claim)` →
   `claim_manual_case` (run lock) writes the checkpoint and calls
   `request_manual_case_guide` (row `queued`, generation g) → commit → claim
   broadcast + task `prepare_manual_case_guide(guide_id, g)` on `assist`. The
   window gets its steps at once (no how-to yet, `how_to_state="pending"`).
2. Chunk 1: research (if due) → notes stored → re-enqueue.
3. Chunk 2..k: steps 1–3, 4–6, … contexts built, calls in parallel, results
   written (conditional on g) → nudge → consumer pushes `manual_run_state`
   with the new items → the window shows them for the step on screen.
4. Release / hand-in / re-test mid-way: the next chunk sees no claim → state
   `stopped`. A later claim with the same fingerprint resumes the missing steps.
5. Ask AI on step n: `answer_person_step_question` loads the guide → `AdviceStep.how_to`
   → advisor prompt includes "How to do it (suggested)".
6. Case page: `get_manual_case_panel` loads the guide once and renders the
   per-step lists.

## Alternatives considered

- **Lazy per viewed step** (window reports the step it shows): needs a new
  runner → server message, a per-step wait for the tester, and still a research
  pass; rejected (D1). A hybrid (eager but prioritise the viewed step) adds the
  same message for little gain.
- **Generate at run start for every case**: pays for cases nobody performs and
  may use the wrong OS if the runner changes; rejected.
- **One LLM call for all steps of a case**: fewer calls, but a different output
  shape (a second parser and prompt), one step's excerpts crowd out others, and
  long cases risk truncated output; rejected to keep one rule set.
- **Store in the checkpoint** (`person_guide` key + per-step how-to): races
  with claim/hand-in/release writers and is wiped by release; rejected (D3).
- **New `guide` queue / `execution` queue**: a dedicated queue is cleanest for
  isolation but adds a compose service and deploy step; `execution` may be
  saturated by long cases so guides would arrive too late (Q1).
- **Show the guide to old runners inside the step text or preconditions**:
  breaks the verbatim rule; rejected.

## Risks and mitigations

- **Ask AI latency on `assist`** (2 slots): chunking bounds a guide's hold on
  a slot to one chunk; `MANUAL_GUIDE_PARALLEL` keeps chunks short. Watch with
  Flower; Q1 offers a dedicated queue.
- **LLM cost**: per-case cap `MANUAL_GUIDE_MAX_STEPS`, reuse across re-claims,
  research once, no pre-warm, kill switch (0).
- **Lost task / worker crash**: staleness at read time turns "preparing" into
  "unavailable"; the next claim restarts from the missing steps.
- **Stale writer after a release and re-claim**: every write is conditional on
  `generation`; a superseded task's update matches no row and it stops.
- **Threads and the ORM**: contexts and writes in the main thread only; the
  pool runs pure HTTP calls; pool bounded and closed (`with`).
- **Secrets / XSS**: items pass `redact_secrets` (case-stated values kept);
  web output auto-escaped (no `|safe`); Tk renders plain text. Source links are
  built from validated ids (a KB item deleted later 404s, as in HITL).
- **Exposure through the runner socket**: guides go only to the project's
  owner runner connection, as the HITL how-to does; same accepted risk as Ask
  AI (Connection-key holders can read project KB-derived text).
- **Verbatim rule**: the how-to is stored apart from the checkpoint and never
  read by the hand-in, TestRail mapping or `manual_result_refiner`; the step
  text in every payload stays the checkpoint's verbatim text.
- **HITL regression**: the prompt refactor (`audience`) must leave HITL prompt
  text identical; verify by rendering before/after.
- **Migration**: additive table, safe on populated databases; rollback drops
  the table (guides are regenerable).
- **Old runners**: no protocol change visible to them; manual runs keep
  starting with 0.13/0.14.

## Implementation steps

1. Shared agents pieces: `audience` on `_how_to_rules`, case-step prompt
   builders, `prepare_case_step_how_to`, public guide-notes JSON helpers.
   Check the HITL prompt is unchanged. (files: `agents/services/person_step_prompts.py`,
   `agents/services/person_steps.py`, `agents/checkpoint.py`)
2. Model + migration + settings: `ManualGuideState`, `ManualStepGuide`,
   migration 0054, three settings with validation, `example.env`, task route.
   (files: `projects/models.py`, `projects/migrations/0054_manual_step_guide.py`,
   `auto_tester/settings.py`, `example.env`)
3. Projects services: fingerprint leaf, request service + claim hook, worker,
   read side, task. (files: `projects/manual_step_guide_fingerprint.py`,
   `projects/manual_step_guide_services.py`, `projects/manual_step_guide_worker.py`,
   `projects/manual_step_guide_read.py`, `projects/manual_case_claim_services.py`,
   `projects/tasks.py`)
4. Protocol: capability, CLIENT_VERSION 0.15.0, payload fields, lenient parse.
   (files: `controller_client/protocol.py`)
   — steps 4–6 can go to parallel sub-agents once 1–3 are in.
5. Runner window: shared how-to renderer, Manual tab block, diagnostics sample
   if `diagnose --window` renders manual payloads. (files:
   `controller_client/person_window_how_to.py`, `controller_client/person_window_app.py`,
   `controller_client/person_window_manual.py`,
   `controller_client/person_window_diagnostics.py`)
6. Server read surfaces: snapshot fields (capability-gated), case card, Ask AI.
   (files: `projects/runner_manual_state_services.py`, `projects/how_to_view.py`,
   `projects/person_step_services.py`, `projects/manual_case_display_services.py`,
   `projects/manual_advice_mapping.py`, `projects/person_step_question_services.py`,
   `templates/projects/includes/_how_to_list.html`,
   `templates/projects/includes/_person_step_card.html`,
   `templates/projects/includes/_manual_case_card.html`,
   `static/css/punk-hazard.css` if a class is needed)
7. Docs: CLAUDE.md Manual paragraph (one sentence + capability), change note.
   (files: `CLAUDE.md`, `docs/changed/2026-11-01-manual-step-howto.md`)

## Verification

- `pre-commit run --all-files` (black, isort, mypy strict for the server and
  `controller_client/`), `python manage.py check`, `makemigrations --check`
  after the migration, `sqlmigrate projects 0054`.
- Prompt parity: render `build_prepare_system_prompt` for Windows/Linux/Darwin,
  with and without a guide, before and after step 1; identical strings.
- Stubbed-LLM script (no tests in the repo; run from the shell, not committed):
  a 5-step case → research once, steps written in order in chunks, items
  validated (a fake KB id dropped, a profile source kept only with a profile),
  a secret redacted, a restating item dropped; an `AIServiceError` on step 3
  stores it `failed` and the others continue.
- Throwaway-DB smoke: claim → row `queued` and task sent on commit; release
  mid-way → `stopped`; re-claim → only missing steps written; a superseded
  generation's write updates 0 rows; a changed case text (Refresh from
  TestRail) → new fingerprint → reset. Count queries: claim +2–3, snapshot ≤ 7,
  case page +1.
- Runner: a 0.15.0 runner shows "Preparing…", then the list with Copy and
  sources for the shown step; navigating steps re-renders; a 0.14.0 runner's
  Manual tab is unchanged and parses the new snapshot without errors (keys
  ignored).
- Case page in a browser: collapsed how-to per step, Copy works, source links
  open the KB item page / Machine readiness tab; read-only member sees it.
- Ask AI on a guided step: the advisor prompt contains "How to do it
  (suggested)" (log/stub check); whole-case question unchanged.
- Kill switch: `MANUAL_GUIDE_MAX_STEPS=0` → state `off`, no task, no UI.
- Run `reviewer` and `verifier` agents in parallel on the diff.

## Open questions

- Q1 **Queue**: run guide generation on `assist` in short chunks (shares the 2
  Ask AI slots), on a new dedicated `guide` queue (new compose service and
  deploy step, best isolation), or on `execution`? (Assumption in this draft:
  `assist`, chunked by `MANUAL_GUIDE_PARALLEL` steps.)
- Q2 **Step cap and long cases**: how many steps per case get a guide, and
  what happens beyond? (Assumption: `MANUAL_GUIDE_MAX_STEPS` = 30, beyond it no
  how-to, Ask AI still available.)
- Q3 **Older runners** (0.13/0.14 with `MANUAL_RUN_V1`): is "no how-to in the
  window, visible on the web case card; update the runner to get it" acceptable,
  or should Manual runs require the new capability to start? (Assumption: not
  required; old runners just lack the block.)
- Q4 **Reuse**: reuse a run case's guide when it is claimed again with
  unchanged case text and runner OS, even if the knowledge base or profile
  changed in between? (Assumption: yes within the same run case; a new run
  generates afresh; no time-based expiry.)
- Q5 **Web card display**: show the how-to on the case page only while the
  case is claimed, or also after the hand-in (useful for reviewers)?
  (Assumption: always, collapsed per step, while the guide matches the case;
  no live refresh, a "reload to see it" hint while preparing.)

## Decisions (open questions resolved 2026-11-01)

- Q1: `assist`, chunked by `MANUAL_GUIDE_PARALLEL` steps.
- Q2: default accepted (`MANUAL_GUIDE_MAX_STEPS` = 30, beyond it no how-to).
- Q3: not required; older runners just lack the block.
- Q4: reuse within the same run case; a new run generates afresh.
- Q5: always shown on the case page, collapsed per step.

## Changelog

- 2026-11-01: created (Draft).
- 2026-11-01: user accepted all recommended defaults (Q1–Q5); Status In progress.
- 2026-11-01: implemented (foundation, then runner window and server read surfaces in parallel, integration). Review/verify round 1 fixed: guide pinned to the OS stored at claim (a runner disconnect no longer hides or stops it), a real per-chunk wall-clock budget (`wait` + non-blocking shutdown, per-call timeout), snapshot size budget (`MANUAL_GUIDE_MAX_STEPS` capped at 30, protocol asserts, `_fits` drops the how-to when over), `updated_at` bump at chunk start, `request_manual_case_guide(project_id=)`. Round 2: black, skip a call with no budget left, size comment. Round 3: logic confirmed by both agents; only formatting (pre-commit skips untracked files, so the new files were `git add -N`ed) and comments changed after it. Accepted: D6 reworded (empty keys to old runners); a guide waiting longer than the stale window between chunks briefly reads "none" (cosmetic); httpx timeouts are per phase, so a pool thread may outlive the chunk (result discarded, pool bounded). Done.
