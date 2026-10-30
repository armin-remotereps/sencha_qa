# feat: TestRail step references on person steps

Status: In progress
Created: 2026-10-30
Request: In a Human in the loop run, every Punk Hazard (PH) plan step handed to a
person must say which TestRail step(s) of the case it covers. Usually one PH step
covers several TestRail steps ("TestRail steps 3–5"). Show it in the Test Runner's
native person window. Also show it on the web "Your turn" card if that is cheap and
consistent. Today the agent's plan carries no link back to the case's numbered
steps, so the planner has to output one. It must be validated, optional, and work
with existing checkpoints and redesigned plans. Older Test Runners must keep working.

## Decisions (answers, 2026-10-30)

- Q1: numbers **and** the TestRail step text (plus its expected result), to cut
  back-and-forth between tester and PH. This replaces the "no protocol change"
  design below for the window: `CasePersonStep` stores the covered steps' texts
  (`case_steps` = `[{num, text, expected}]`, ≤ 50 items (15 on the wire), ≤ 1000 chars each,
  copied from the case when the step is handed out, in
  `services._pause_run_for_person` via `projects.case_step_refs.case_step_rows`)
  and their label (`case_steps_label`). Protocol 0.10.2 adds
  `person_step_case_steps_v1` and the snapshot fields `case_steps_label` /
  `case_steps`; the window shows a "Covers …" block with the texts. Runners
  without the capability get the label appended to `step_label` (numbers
  only). The web "Your turn" card shows the texts too.
- Q2: free-text cases show nothing.
- Q3: "Case step(s)" when the case has no `testrail_id`, "TestRail step(s)"
  otherwise (`format_case_steps_label(refs, testrail=)`).
- Q4: no other places in this change.
- Q5: Steps card label included (own commit).
- Step text parsing: `numbered_case_steps` splits `steps` on "<n>. " markers at
  the start or after a blank line (as `flatten_steps` joins them), in sequence;
  `expected` markers ascending and ≤ the step count.

## Context

- Plan step type: `agents/types.py::SubTask` (description, expected_result,
  timeout_seconds). It is built in `orchestrator._build_sub_task` (planner JSON and
  recovery task), `orchestrator._build_resume_sub_task`, `checkpoint._parse`,
  `plan_revision.apply_plan_revision`, `person_step_prompts` (confirm sub-task) and
  `case_teardown_prompts`. Only the planner and the redesigns need the new field.
  The others keep the default.
- Planner prompt: `agents/services/orchestrator_prompts.build_plan_system_prompt`
  (output format `{"sub_tasks": [{description, expected_result, timeout_seconds}]}`).
  The user message is `projects/case_task_text.build_task_description(test_case)`,
  which includes `Steps:\n<test_case.steps>`.
- Case steps: `projects/testrail_mapping.flatten_steps` stores separated-steps cases
  (`template == STEPS_TEMPLATE_NAME == "Test Case (Steps)"`) as `"1. …\n\n2. …"`.
  Text-template cases (`"Test Case"`) keep TestRail's free text, so there are no
  reliable step numbers. Manual and CSV cases set `template` freely (`TestCaseForm`).
- Checkpoint: `agents/checkpoint.py`, `CHECKPOINT_VERSION = 1`. New keys are optional
  and written only when set (the `how_to` and `plan_version` precedent).
- Person hand-off: `PersonStepHandOff` (checkpoint `person` key) is built in
  `agents/services/person_steps.prepare_person_step` → `parse_hand_off` /
  `person_step_prompts.fallback_hand_off`. It is re-handed with `objection` via
  `dataclasses.replace` (orchestrator ~L958). It is rebuilt from the row in
  `projects/person_step_services` (~L544) and `person_step_question_services` (~L415).
- Row: `projects.models.CasePersonStep`, created and reopened in
  `projects/person_step_rows.py` (~L400–440). The latest migration is
  `0042_hitl_assist_and_project_mode`.
- Runner snapshot: `projects/runner_person_step_services.get_runner_person_steps`
  (one `.values()` query, `_STEP_FIELDS`). `_build_view` builds
  `step_label = "Step N of M"`. The wire type is
  `controller_client/protocol.PersonStepStatePayload.step_label` (≤ 255 chars,
  truncated on parse). The window shows it in the subheader (`"{step_label} ·
  attempt {round}"`) and in the multi-step picker
  (`person_window_app.py` ~L755, ~L884). Every runner with `PERSON_STEPS_V1`
  already renders `step_label`.
- Web card: `projects/person_step_services.get_person_step_panel` → `PersonStepPanel`,
  `templates/projects/includes/_person_step_card.html` (heading
  `Your turn: {{ step_label|lower }}`).
- Steps card: `projects/case_step_progress.py` (`CaseStepRow` → JSON payload,
  rendered by Alpine in `templates/projects/test_run_case_detail.html` ~L270,
  `x-text`).
- Redesigns: `agents/plan_revision.py` (`PlanStepInput`, `RevisionStep`,
  `validate_plan_revision`, `apply_plan_revision`, `revision_steps_to/from_json`,
  `plan_fingerprint`), the tool schema `machine_setup/services/plan_step_args.py`
  (`STEP_ITEM_SCHEMA`, `parse_plan_steps`), and the re-validation on apply in
  `projects/case_plan_revision_services.py` (~L287) and
  `projects/case_review_redesign_services.py` (~L281). The agent sees the plan
  in `machine_setup/case_help_brief.py` / `case_result_brief.py`, filled by
  `machine_setup/services/case_help_context.py` / `case_result_context.py`.
- Controller: `CLIENT_VERSION = "0.10.1"`.

## Goals

- The planner tags each sub-task with the case's numbered steps it covers. The tags
  are validated and stored with the plan, and old checkpoints still load.
- A person step shows "TestRail step 3" / "TestRail steps 3–5" / "TestRail steps 1,
  3–4" in the Test Runner window on every runner version that has person steps,
  with no protocol change.
- The web "Your turn" card shows the same label.
- Redesigned plans keep or set the tags. Carried steps keep the tags of their
  earlier step.
- A case without reliable numbered steps shows nothing.

## Non-goals

- No TestRail step text in the window or on the card. That is Q1. If the answer is
  yes, it becomes a protocol change; see Alternatives.
- No changes to the TestRail result comment (`format_step_breakdown`), the run
  page banner, the run table reason column, or the redesign draft view. These are
  follow-ups.
- Old plans are not backfilled. They show nothing until the case is re-planned.
- No new controller capability and no `CLIENT_VERSION` bump.

## Design

### Data model

- `SubTask.case_steps: tuple[int, ...] = ()`. These are the 1-based case step
  numbers the step performs or checks, sorted, unique, and empty for implicit or
  precondition steps.
- `PersonStepHandOff.case_steps: tuple[int, ...] = ()`. It is copied from the plan
  step at prepare time. It is always empty for `kind="restore"`, because a restore
  covers earlier steps and a label there would mislead.
- `OrchestratorCheckpoint.case_step_count: int = 0`. This is the number of numbered
  case steps when the case was planned, and the bound for every later validation
  (redesigns). Storing it means the redesign tools do not have to load the
  `TestCase`. It also keeps the step numbers relative to the text the plan was made
  from.
- Checkpoint JSON. All new keys are optional, so `CHECKPOINT_VERSION` is unchanged
  and older parsers ignore them:
  - each plan item: `"case_steps": [3, 4, 5]`, written only when non-empty;
  - `person` key: `"case_steps"`, written only when non-empty;
  - root: `"case_step_count"`, written only when > 0.
  - Parsing is lenient: a missing or malformed value becomes `()` / `0`, with no
    `_MalformedCheckpoint`. Plan item refs are re-normalized against the stored
    count when the count is > 0, otherwise kept as positive ints (capped).
- `CasePersonStep.case_steps = JSONField(default=list, blank=True)` holds a list of
  ints. Migration `0043_casepersonstep_case_steps` is additive, has a default, and
  needs no backfill. Existing rows show nothing.
- `CasePlanRevision.steps` JSON (`revision_steps_to_json` / `from_json`) gains an
  optional `"case_steps"` per step. No schema change, since it is a JSONField.
- `plan_fingerprint` deliberately excludes `case_steps`. Drafts proposed before
  the deploy stay valid. The tags are display metadata and cannot change without the
  description changing too.

### Services

- **`agents/case_step_refs.py`** (new, pure, no ORM) holds the validation rules in
  one place:
  - `normalize_case_step_refs(raw: object, step_count: int) -> tuple[int, ...]`.
    Not a list, or `step_count <= 0` → `()`. It keeps `int` (not `bool`) values in
    `1..step_count`, dedupes, sorts, and caps at `MAX_CASE_STEP_REFS` (e.g. 50).
    Everything else is dropped silently. This is the planner, checkpoint and
    revision path.
- **`projects/case_step_refs.py`** (new, pure, no ORM) holds the presentation:
  - `format_case_steps_label(refs: Sequence[int]) -> str` returns `""`,
    `"TestRail step 3"`, `"TestRail steps 3–5"` or `"TestRail steps 1, 3–4, 7"`
    (en dash, consecutive runs collapsed). Its output stays far below 255 chars for
    ≤ 50 refs.
  - `stored_case_steps(value: object) -> tuple[int, ...]` reads a row's JSON
    leniently (positive ints only).
  - The label is formatted on the server and sent preformatted to the runner
    (`controller_client` cannot import server code, and this avoids a runner
    change). The web card, the Steps card, the briefs and the runner snapshot all
    use this one formatter.
- **`projects/case_task_text.py`** gains
  `numbered_case_step_count(test_case: TestCase) -> int`. It returns 0 unless
  `test_case.template == STEPS_TEMPLATE_NAME`. Otherwise it scans `steps` for
  line-start `^(\d+)\. ` markers in sequence 1, 2, 3, … (a marker out of sequence is
  ignored, so a "2." inside step 1's text cannot inflate the count) and returns the
  last number found. It stays a leaf. Import `STEPS_TEMPLATE_NAME` from
  `projects.testrail_mapping`, or move the constant to a leaf if that import cycles.
- **Planner** (`orchestrator_prompts.build_plan_system_prompt(case_step_count=)`):
  when `case_step_count > 0`, add one rule and extend the output format:
  - "CASE STEP REFERENCES: the test case's Steps are numbered 1 to N. Give every
    sub-task `case_steps`: the numbers of the case steps it performs or checks (one
    sub-task usually covers several, e.g. [3, 4, 5]). Use [] for preconditions and
    implicit steps you added. Never invent numbers outside 1..N."
  - The JSON example becomes `{…, "timeout_seconds": N, "case_steps": [n, …]}`.
  - With 0, the prompt is byte-identical to today's.
- **Orchestrator**: `run_orchestrator(..., case_step_count: int = 0)`.
  - On a fresh plan, `_plan_sub_tasks` / `_build_sub_task(item, bounds,
    case_step_count)` normalize `item["case_steps"]`.
  - On resume, it uses `resume.case_step_count` when > 0, else the passed value
    (an old checkpoint picks it up, which only matters for a later redesign).
  - `_Progress.case_step_count` is carried into every checkpoint, as
    `plan_version` is. Dropping it would lose the bound on the first save after a
    resume.
  - The recovery task (`_build_sub_task` from the evaluate answer) passes 0, so it
    gets no refs.
- **Person hand-off**: `prepare_person_step` returns
  `dataclasses.replace(hand_off, case_steps=sub_task.case_steps)` for non-restore
  hand-offs. That covers both the parsed and the fallback hand-off, and the model
  never writes refs. The re-hand with `objection` keeps them (`replace`).
- **Rows** (`person_step_rows`): set `case_steps=list(hand_off.case_steps)` on
  create, and also on the reopen path (`update_fields` += `"case_steps"`), so a
  re-hand always matches the checkpoint. Rebuild-from-row fallbacks
  (`person_step_services` ~L544, `person_step_question_services` ~L415) pass
  `stored_case_steps(step.case_steps)`.
- **Redesigns** (`agents/plan_revision.py`):
  - `PlanStepInput.case_steps: object = ()` (raw) and
    `RevisionStep.case_steps: tuple[int, ...] = ()`.
  - `validate_plan_revision` normalizes against `checkpoint.case_step_count`. A
    carried step whose normalized refs are empty inherits
    `checkpoint.plan[carried_from - 1].case_steps`. That is the "carried steps keep
    their reference" rule, and it never raises for refs.
  - `apply_plan_revision` passes them into the new `SubTask`s and keeps
    `case_step_count`.
  - `revision_steps_to_json` writes `case_steps` only when non-empty, and
    `from_json` reads it leniently.
  - Both apply-time re-validations (`case_plan_revision_services`,
    `case_review_redesign_services`) pass `case_steps=step.case_steps` through.
  - `machine_setup/services/plan_step_args.py`: `STEP_ITEM_SCHEMA` gains optional
    `"case_steps": {"type": "array", "items": {"type": "integer"}, "description":
    "Optional. The test case's numbered steps this step covers; omit for a carried
    step to keep its earlier numbers."}`. `parse_plan_steps` raises
    `PlanStepArgumentError` only for a non-list or non-int item (shape). Range is
    filtered silently by the validator.
  - Briefs: `PlanStep` / `ResultPlanStep` gain `case_steps_label: str = ""`. The
    plan lines append ` ({label})` when set, so the chat agent can carry the numbers
    over. The fill sites are `case_help_context.py` ~L96/~L186 and
    `case_result_context.py` ~L206.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| case page (`projects:test_run_case_detail`), existing | GET | `@project_membership_required` (unchanged) | none new | `person_step_panel.case_steps_label` (new `str` on `PersonStepPanel`); Steps card rows gain `case_steps_label` in the JSON payload | none |

- `_person_step_card.html`: under the `<h2>`, add
  `{% if person_step_panel.case_steps_label %}<p class="muted">Covers {{ person_step_panel.case_steps_label }}</p>{% endif %}`.
  It is autoescaped (server-formatted digits anyway) and needs no new CSS class.
  Update the context-contract comment.
- Steps card (`test_run_case_detail.html` ~L270): one
  `<p class="step-detail muted" x-show="row.case_steps_label" x-cloak x-text="row.case_steps_label"></p>`.
  `CaseStepRow.case_steps_label` comes from `task.case_steps` and is added to the
  row payload builder (~L615). The same payload feeds the live socket update, so
  no event change is needed.

### Background work and WebSockets

- `person_step_state` (server → runner): no wire change. `_build_view` appends the
  label to the existing `step_label`: `"Step 2 of 5 · TestRail steps 3–5"`, or
  `"Step 2 · TestRail steps 3–5"` when the plan size is unknown. `"case_steps"` is
  added to `_STEP_FIELDS`, so it is still one query.
- The window shows it in the subheader (`"Step 2 of 5 · TestRail steps 3–5 ·
  attempt 1"`) and in the picker label of every runner since `PERSON_STEPS_V1`.
  There is no capability, no `CLIENT_VERSION` bump and no runner edit.
- Ask AI context: `person_step_question_services` (~L414) builds `step_label`
  `"Step N"`. Append the same label so the advisor can relate the step to the
  case text. This is a one-line, prompt-only change.
- No Celery or queue change. The planner change runs inside the existing
  `execution` worker.

### Settings / infrastructure

None. No new env vars.

### Data flow

1. `projects.services.execute_test_run_test_case` builds `task_description` and
   `case_step_count = numbered_case_step_count(pivot.test_case)`, then calls
   `run_orchestrator(..., case_step_count=)`.
2. The planner answers `case_steps` per sub-task. `_build_sub_task` normalizes
   them. `_Progress` saves plan refs and `case_step_count` in every checkpoint.
3. In HITL, `prepare_person_step` copies the step's refs onto the hand-off, which
   goes into the checkpoint `person` key. The save hook creates or reopens the
   `CasePersonStep` with `case_steps`.
4. Runner: the consumer push → `get_runner_person_steps` → `step_label` with the
   label → the window subheader.
5. Web: `get_person_step_panel` → `case_steps_label` → card. The Steps card reads
   the refs from the checkpoint plan.
6. Redesign: the agent sends `case_steps` per step, or omits it for carried steps.
   Validation normalizes and inherits, the draft JSON stores them, and apply
   writes them into the new plan.

## Alternatives considered

- **A new wire field (`case_steps_label`) with a `PERSON_STEP_REFS_V1` capability,
  a 0.10.2 bump and a separate window row.** This would allow a dedicated line or
  the step text, and would fall back to `step_label` for old runners. It was
  rejected for now: `step_label` already reaches every runner and is exactly
  "which step is this". A new field would add protocol, capability, version and
  window code for the same information. If Q1 (show the TestRail step text) is
  answered yes, this alternative becomes necessary. The text does not fit a
  255-char label, and inlining it into `instruction` would blur model text with
  case text. Then add `case_steps: list[{num, text}]` to `PersonStepStatePayload`
  (optional, default empty, parsed leniently with caps), a capability, a version
  bump, a collapsible "TestRail steps" block in `person_window_app.py` (the window
  already scrolls), and keep the label in `step_label` for older runners.
- **Runner formats the range.** This would need the ints on the wire, which means a
  protocol change, and duplicates the formatter in `controller_client`. Rejected.
- **Derive the bound at each redesign from `pivot.test_case`.** That is an extra
  join or query at four call sites, and the numbering could drift if the case was
  re-imported mid-run. Storing `case_step_count` in the checkpoint is simpler.
- **Count numbered lines in free-text (`"Test Case"`) templates too.** Authors' lists
  restart, nest or number preconditions, so wrong references are worse than none.
  This is left as Q2.
- **Let the person-step prepare LLM write the refs.** That is a second source of
  truth that could disagree with the plan. The refs belong to the plan step.

## Risks and mitigations

- *Planner returns wrong numbers.* They are bounded to 1..N and the prompt asks
  for [] when unsure. Wrong but in-range numbers are possible, and the label is
  advisory only. During smoke checks, review a few plans against their cases.
- *Prompt regressions for cases without numbered steps.* With a count of 0 the
  prompt is unchanged.
- *Checkpoint compatibility.* There are only optional keys and no version bump. A
  rollback ignores them. A malformed value is dropped and never fatal.
- *Fingerprint churn.* `case_steps` is kept out of `plan_fingerprint`, so drafts
  pending at deploy stay applicable.
- *Migration.* `CasePersonStep` gets an additive JSON column with a default. It is
  instant on Postgres (constant default) and needs no backfill.
- *Label length.* The 255-char `step_label` cap holds: 50 refs worst case is about
  200 chars plus "Step 20 of 20 · ". Truncation on parse is harmless anyway.
- *XSS.* Labels are server-formatted from ints, autoescaped in templates and
  `x-text` in Alpine, never `|safe`.
- *Query count.* The runner snapshot only adds one column to the existing
  `.values()`. The web panel already has the row. The Steps card reads the
  already-loaded checkpoint. No N+1.
- *Old runners.* They show the label as part of the step text they already render.
  Nothing is needed from them.

## Implementation steps

Each step is one emoji commit. Wave 1 runs alone (shared code). The steps inside
waves 2 and 3 touch disjoint files and can run as parallel sub-agents.

**Wave 1 (shared, first)**

1. ✨ Step references: types, validation, checkpoint, label formatter.
   Files: `agents/types.py` (`SubTask.case_steps`, `PersonStepHandOff.case_steps`,
   `OrchestratorCheckpoint.case_step_count`), `agents/case_step_refs.py` (new),
   `agents/checkpoint.py` (plan item / `person` / root keys, lenient parse),
   `projects/case_step_refs.py` (new: `format_case_steps_label`,
   `stored_case_steps`).

**Wave 2 (parallel, all depend on step 1 only)**

2. ✨ Planner tags sub-tasks with case steps.
   Files: `agents/services/orchestrator_prompts.py`,
   `agents/services/orchestrator.py` (`run_orchestrator` kwarg, `_plan_sub_tasks`,
   `_build_sub_task`, `_Progress`/`_new_progress`), `agents/services/person_steps.py`
   (copy refs onto the hand-off), `projects/case_task_text.py`
   (`numbered_case_step_count`), `projects/services.py` (pass the count).
3. ✨ Redesigned plans keep and set case step references.
   Files: `agents/plan_revision.py`, `machine_setup/services/plan_step_args.py`,
   `projects/case_plan_revision_services.py`,
   `projects/case_review_redesign_services.py`, `machine_setup/case_help_brief.py`,
   `machine_setup/case_result_brief.py`,
   `machine_setup/services/case_help_context.py`,
   `machine_setup/services/case_result_context.py`.
4. 🗃️ Store the case steps on `CasePersonStep`.
   Files: `projects/models.py`,
   `projects/migrations/0043_casepersonstep_case_steps.py`,
   `projects/person_step_rows.py` (create + reopen).
5. 💄 Steps card shows the TestRail steps of each plan step.
   Files: `projects/case_step_progress.py`, `templates/projects/test_run_case_detail.html`.

**Wave 3 (parallel, depend on step 4)**

6. 💄 Test Runner window shows the TestRail steps of a person step.
   Files: `projects/runner_person_step_services.py` only.
7. 💄 "Your turn" card and Ask AI context show the TestRail steps.
   Files: `projects/person_step_services.py` (panel field + row-rebuild fallback),
   `templates/projects/includes/_person_step_card.html`,
   `projects/person_step_question_services.py` (row-rebuild + advice `step_label`).

**Wave 4 (orchestrator)**

8. 📝 Docs: `docs/changed/2026-10-30-testrail-step-refs.md`, a short addition to
   `CLAUDE.md` (HITL paragraph) and `agents/CLAUDE.md` (person steps / plan
   redesign paragraphs). Set this plan to Done.

After each wave, run `pre-commit run --all-files` (black, isort, mypy server +
`controller_client/`).

## Verification

- `pre-commit run --all-files` is clean. `python manage.py makemigrations --check`
  shows nothing pending after step 4. `python manage.py check` passes.
- Smoke script (`manage.py shell -c`, read-only, no DB writes):
  - `normalize_case_step_refs([3, "4", 5, 5, 99, True, 0], 6) == (3, 5)`;
    `format_case_steps_label` on `()`, `(3,)`, `(3, 4, 5)`, `(1, 3, 4, 7)` gives
    `""`, `"TestRail step 3"`, `"TestRail steps 3–5"`, `"TestRail steps 1, 3–4, 7"`.
  - `checkpoint_from_json` on a stored pre-change checkpoint from the dev DB
    parses, and `checkpoint_to_json` round-trips with no new keys when there are
    no refs.
  - `numbered_case_step_count` on a Steps-template case, a text-template case, and
    a step whose text contains a "2." line.
- Manual, HITL run on a TestRail separated-steps case with a connected runner:
  - The case log's planned sub-tasks and the stored checkpoint have `case_steps`.
  - The Test Runner window subheader reads "Step N of M · TestRail steps a–b ·
    attempt 1", and the picker shows the same text when two steps wait.
  - The "Your turn" card shows "Covers TestRail steps a–b". The Steps card shows
    the label per row and updates live.
  - A restore hand-off (Resume mid-case) shows no label.
  - A text-template case shows no label anywhere, and its planner prompt is
    unchanged.
  - Not confirmed → re-hand keeps the label.
  - Help chat redesign: carry step 1, add a new step with `case_steps`, then
    Continue. The carried step keeps its old label and the new step shows its own.
    Do the same through the result chat "Re-run with redesigned plan".
  - A draft proposed before the deploy still applies (fingerprint unchanged).
  - A pre-0.10.1 runner, if available, shows the label in its subheader.
- Run the `reviewer` and `verifier` agents (give `verifier` the request and this
  plan path).

## Open questions

- Q1. In the window and on the card, show only the numbers ("TestRail steps 3–5"),
  or also the TestRail step text, e.g. a collapsible list "3. Open Settings…
  4. …"? (Assumption used in this draft: numbers only. The text would need the
  wire field + capability + CLIENT_VERSION 0.10.2 + window block described under
  Alternatives.)
- Q2. Cases whose steps are free text (TestRail "Test Case" text template, or
  manual or CSV cases not using "Test Case (Steps)"): show nothing, or also number
  them when the text happens to be a numbered list? (Assumption: nothing; only
  "Test Case (Steps)" cases get references.)
- Q3. The label wording is "TestRail step(s) …" even for a manually created case
  that uses the Steps template. Is "TestRail" right, or should cases without a
  `testrail_id` say "Case step(s)"? (Assumption: always "TestRail".)
- Q4. Besides the person window, the "Your turn" card and the Steps card, should
  the references also appear for agent-run steps elsewhere: the TestRail result
  comment step breakdown, the run page "Waiting for a person" banner and reason
  column, or the redesign draft view? (Assumption: not in this change; follow-up
  plans.)
- Q5. Is the Steps card label (step 5) wanted in this change? It is cheap and
  separable, and can be dropped without affecting the rest. (Assumption: yes,
  included.)

## Changelog

- 2026-10-30: created
- 2026-10-30: answers recorded (Decisions); window gets a new wire field +
  capability + 0.10.2 instead of only the `step_label` suffix.
- 2026-10-30: review round 1 fixes: CRLF steps (web form) normalised; the
  TestRail/Case label source stored at plan time
  (`OrchestratorCheckpoint.case_steps_testrail`, optional key) instead of read
  from a possibly uncached `pivot.test_case`; label capped to 255; rows keep up
  to 50 steps; no texts stored when the case's step count changed since
  planning (stale guard); runner accepts any positive step number.
- 2026-10-30: review round 2: a not-confirmed reopen keeps the row's stored
  case steps and label. Accepted, not fixed: the stale guard compares only the
  step count (a same-count reword mid-run shows the new text); a row rebuilt
  without texts gives the Ask AI hand-off no numbers (its `step_label` still
  has the label); the 15-item wire cap lists only the first 15 steps in the
  window while the label names all.
