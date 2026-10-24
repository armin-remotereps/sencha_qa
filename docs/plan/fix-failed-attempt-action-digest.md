# fix: Recovery and retry sub-agents learn what the failed attempt did

Status: Done
Created: 2026-10-24
Request: When a sub-task fails (most visibly on timeout), the orchestrator asks the
LLM whether to recover, then runs a recovery sub-agent and retries the step. Neither
the evaluate call, the recovery sub-agent nor the retried step knows which actions
the failed attempt took: the transcript is thrown away and only a one-line summary
such as "Timed out after 312.4 seconds" survives. The fix builds a bounded,
deterministic digest of the failed attempt from its transcript, with no extra LLM
call. The digest lists each tool call (name, compact arguments, the head of its
result) and the attempt's last observation. It is stored on `SubTaskResult` and fed
to the evaluate prompt, to the recovery sub-agent's state and to the retried step's
state.

## Context
- `agents/services/agent_loop.py` `run_tool_loop`: on deadline it returns
  `AgentResult(stop_reason=TIMEOUT, messages=<full transcript>, error="Timed out
  after N seconds")`. On max iterations it returns `MAX_ITERATIONS` with the
  transcript and no error. Tool results enter the transcript as
  `ChatMessage(role="tool", content=<str, capped>, tool_call_id=...)`. Assistant
  messages carry `tool_calls` (`ToolCall.tool_name`, `.arguments: dict[str, object]`)
  and often have empty `content` (reasoning model). `ChatMessage.content` is typed
  `str | tuple[ContentPart, ...] | None`.
- `agents/services/sub_agent.py` `_parse_sub_task_result(agent_result)`: it reads
  only the last assistant text. On timeout there is no `RESULT:` line, so the
  summary is that text (often a stale fragment) or `agent_result.error`. The
  `messages` are dropped.
- `agents/types.py`: `SubTaskResult(status, summary, iterations, error=None)` and
  `SubTaskAttempt(step_num, kind, result, duration_seconds)`.
- `agents/services/orchestrator.py`:
  - `_execute_sub_tasks` computes `state_description` from `state_lines` *before*
    the step runs.
  - On failure it calls `_evaluate_failure`, then `_attempt_recovery(...,
    state_description=state_description)`. So the recovery sub-agent sees only the
    earlier steps, without even a line saying step N failed.
  - A passed recovery appends only `"Recovery for step N: <summary>"`. The retried
    step therefore has no idea what attempt 1 did, so it can type text twice,
    re-submit forms, and so on.
  - `_rebuild_state_lines` must reproduce exactly the state lines the live loop
    writes, for resumed cases.
- `agents/services/orchestrator_prompts.py`:
  - `build_evaluate_prompt` shows only `summary` and `error`.
  - `format_attempt_lines` feeds the verdict prompt and `_deterministic_summary`.
- `agents/checkpoint.py`: pure (de)serializer, `CHECKPOINT_VERSION = 1`. Its module
  docstring requires that new keys stay optional with a default and that the
  version is not bumped (a bump makes every stored checkpoint unusable; older
  parsers ignore unknown keys).
- `projects/case_step_progress.py` and `projects/case_checkpoint_services.py` show
  `summary`/`error` per attempt on the case page and in TestRail comments.
- `ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS` defaults to 1 (`auto_tester/settings.py`).
  A failed recovery always stops the case (`_attempt_recovery` returns False).
- Change notes read: `docs/changed/2026-10-22-recovered-step-verdict.md`,
  `2026-10-22-resume-blocked-case.md` and `2026-10-23-remove-llm-summarizers.md`.
  The last one is why this fix is deterministic: LLM summarizers were removed on
  purpose.

## Root cause
Confirmed by reading the code paths above (no runtime reproduction needed):
1. The failed attempt's transcript (`AgentResult.messages`) is discarded in
   `_parse_sub_task_result`. Only a summary survives, and on timeout that summary
   is the timeout string or stale narration, because reasoning-model tool-call
   turns have empty `content`.
2. `_attempt_recovery` receives the pre-step `state_description`, so the recovery
   sub-agent is not told that step N ran, failed, or what it did.
3. After a passed recovery, the retried step sees only the recovery's summary.
4. `build_evaluate_prompt` writes the `recovery_task` blind for the same reason.

## Goals
- Every non-passing sub-agent result (timeout, max iterations, explicit `RESULT:
  FAIL`, no-RESULT failure) carries a deterministic, size-bounded digest of the tool
  calls it made and its last observation.
- The evaluate prompt, the recovery sub-agent's state and the retried step's state
  include that digest.
- Resumed cases rebuild the same state lines (the digest is persisted in the
  checkpoint) and old checkpoints still load.

## Non-goals
- No extra LLM call and no change to the summary text itself. The timeout summary
  stays as it is; the digest sits next to it.
- No digest in the verdict prompt, `_deterministic_summary`, the case page step
  rows, TestRail comments or the case log. These stay summary-only, so the verdict
  prompt and the UI do not bloat.
- No digest on the "continue past a failure" or "stopped" state lines. Later steps
  keep seeing the one-line `FAILED — summary` as today. A step that was skipped past
  is not retried, so duplication is not a risk there (possible follow-up).
- A failed recovery's own digest is not used: a failed recovery always stops the
  case, so there is no second recovery that could read it. It is still stored on the
  attempt, because it falls out of the same code path and is free.
- No digest for a resumed case's interrupted, in-flight failed attempt (the cursor
  step with no passed recovery). The restore sub-task restores state for the steps
  before the cursor, as today.
- No controller protocol, model, migration, URL, template or WebSocket change.

## Design

### Data model
- `agents/types.py` `SubTaskResult` gains one new field:
  `actions_digest: str = ""`. It is empty for passed results. The default keeps
  every existing constructor valid. No DB model change: the checkpoint JSON is the
  only persistence.

### New pure module: `agents/attempt_digest.py`
This module sits beside `agents/checkpoint.py` and `agents/transcript.py`. It is
pure: it imports only `agents.types` and the stdlib (`json`). It has no
`django.conf` import and no `agents.services` import, so the
`agents.services` -> `projects.services` cycle cannot reach it. The function is
not generic text utility: it knows the agent transcript shape, so it belongs in
`agents/`, not in a `utils` module.

Public interface:
- `ATTEMPT_DIGEST_MAX_CHARS: Final = 6000` is the cap for the whole digest.
- `build_attempt_digest(messages: Sequence[ChatMessage]) -> str` returns `""` when
  the attempt made no tool calls and had no observation text.

Private constants:
- `_ARGS_MAX_CHARS = 300` caps one call's compact arguments.
- `_ARG_VALUE_MAX_CHARS = 150` caps each string argument value.
- `_RESULT_HEAD_CHARS = 300` caps the head of one tool result.
- `_OBSERVATION_MAX_CHARS = 600` caps the last observation.

Behaviour:
1. **Index tool results.** Map `tool_call_id` to the `role="tool"` message. Turn
   content into text safely:
   - `str` is used as is;
   - `tuple[ContentPart, ...]` joins the `TextContent.text` parts and turns each
     `ImageContent` into `[image]` (base64 never enters the digest);
   - `None` becomes `""`.
2. **Write one numbered line per tool call.** Walk the assistant messages in
   order. For each `ToolCall`, write
   `N. tool_name(<compact args>) -> <result head>`:
   - Compact args: `json.dumps` of the arguments, with every string value cut to
     `_ARG_VALUE_MAX_CHARS` plus `…`, `ensure_ascii=False`, compact separators and
     `default=str`. The whole string is then cut to `_ARGS_MAX_CHARS`.
   - Result head: whitespace collapsed to single spaces, cut to
     `_RESULT_HEAD_CHARS` plus `…`.
   - A call with no recorded result shows `(no result recorded)`.
3. **Add the last observation.** Use the last assistant message whose `content` is
   non-empty text. If there is none, use the last assistant message's
   `ReasoningItem.summary` texts. Collapse whitespace, cut the text to
   `_OBSERVATION_MAX_CHARS` and emit it as `Last observation: ...`.
4. **Bound the total.** Keep the observation, give the call lines the rest of the
   budget, and keep head and tail when the lines do not fit:
   - The first third of the call budget is filled from the first calls.
   - The other two thirds are filled from the last calls.
   - One marker sits between them: `… K tool calls omitted …`.

   Both ends matter. Early calls are the meaningful actions (typing, submitting)
   that a retry must not repeat. Late calls show the state the attempt left the
   machine in. The result is always `<= ATTEMPT_DIGEST_MAX_CHARS`.

Example output:
```
1. browser_navigate({"url":"https://app.example/login"}) -> Navigated to https://app.example/login (title: Sign in)
2. browser_type({"index":4,"text":"qa@example.com"}) -> Typed into element 4
3. browser_click({"index":7}) -> Clicked element 7
4. wait({"seconds":60}) -> Waited 60s
Last observation: The dashboard is still loading the export job.
```

**Module constants instead of settings.** These are prompt-shaping limits, like
`_STEP_DESCRIPTION_MAX_CHARS` in `orchestrator_prompts.py`, not deployment config.
A setting would also pull `django.conf` into a module that is pure on purpose. So
`example.env` and `settings.py` do not change.

### Services
- **`agents/services/sub_agent.py` `_parse_sub_task_result(agent_result)`.** Every
  return path whose status is `"fail"` sets
  `actions_digest=build_attempt_digest(agent_result.messages)`. That covers the
  explicit `RESULT: FAIL` path and the no-RESULT default-fail path. Pass paths
  leave it `""`. The signature is unchanged.
- **`agents/services/orchestrator_prompts.py`.**
  - `build_evaluate_prompt` gets a new block after `ERROR:`:

    `ACTIONS THE FAILED ATTEMPT TOOK (tool calls in order, results shortened; their
    effects may still be in place):` followed by the digest, or `(none recorded)`.

    One sentence is added to the instructions: write the `recovery_task` so that it
    does not repeat actions whose effect is already in place. Say what the attempt
    already did when that matters, because the recovery sub-agent and the retried
    step also see this log.
  - New public helper
    `format_failed_attempt_state(step_num: int, result: SubTaskResult) -> str`.
    It returns `Step {N}: an earlier attempt FAILED — {summary}`. When the digest is
    non-empty, it adds `\n  Actions that attempt took (their effects may still be in
    place; check the screen before repeating any):\n` and the digest lines indented
    by four spaces. It lives here because it is prompt wording and is shared by the
    live loop and the resume rebuild.
  - `format_attempt_lines`, `_format_result_line` and `build_verdict_prompt` are
    **unchanged**, so the digest stays out of the verdict.
- **`agents/services/orchestrator.py`.**
  - `_execute_sub_tasks`: pass the failed `result` into `_attempt_recovery` (new
    keyword `failed_result: SubTaskResult`).
  - `_attempt_recovery(..., state_description: str, failed_result: SubTaskResult,
    ...)` builds
    `failed_line = format_failed_attempt_state(step_num, failed_result)`.
    - The recovery sub-agent gets `state_description` plus a newline plus
      `failed_line` (just `failed_line` when the state is empty).
    - On a passed recovery, append `failed_line` to `progress.state_lines`, then
      the existing `Recovery for step N: ...` line. The retried step sees both.
      Later steps see them as well, the same way the recovery line is already
      permanent today. The digest is capped, and
      `ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS` bounds how many lines there are.
    - A failed recovery appends nothing new (the case stops; behaviour unchanged).
  - `_evaluate_failure`: no signature change. It already passes `sub_task_result`
    to `build_evaluate_prompt`.
  - `_rebuild_state_lines(attempts, next_step)`: track the latest failed
    `kind="step"` result per `step_num`. When a passed `recovery` attempt is
    reached, emit `format_failed_attempt_state(step_num, that_result)` before the
    `Recovery for step N` line, mirroring the live loop exactly. Update the
    docstring.
  - The second-failure path needs no new code. If the retry fails again,
    `state_description` (computed at the loop top) already contains attempt 1's
    failed line and the recovery line. The evaluate prompt then shows attempt 2's
    digest.

### Checkpoint (`agents/checkpoint.py`)
- `checkpoint_to_json` writes `"actions_digest": attempt.result.actions_digest` on
  each attempt.
- `_parse_attempt` reads it leniently, like the display-only keys: a non-string or
  missing value becomes `""`. It never raises `_MalformedCheckpoint`.
- `CHECKPOINT_VERSION` stays `1`, as the module docstring requires:
  - old checkpoints load with `actions_digest=""`, so a resumed case rebuilds the
    failed line without the digest (summary only);
  - an older parser on rollback ignores the extra key.
- Why persist: `_rebuild_state_lines` must reproduce the live state lines, which
  now include the digest after a passed recovery. Size cost is at most about 6 KB
  per failed attempt in `TestRunTestCase.execution_checkpoint`.

### Pages and forms
None. `projects/case_step_progress.py` and `projects/case_checkpoint_services.py`
keep reading only `summary`/`error`, so the digest does not reach the case page or
TestRail.

### Background work and WebSockets
None. The change runs inside the existing `execute_test_run_case` Celery task on
the `execution` queue. No new events, no controller actions, no protocol change.

### Settings / infrastructure
None (see the module-constants decision above). Deploy: restart the execution
workers.

### Data flow
1. A step's sub-agent times out. `run_tool_loop` returns TIMEOUT with the
   transcript.
2. `_parse_sub_task_result` builds the fail result with
   `actions_digest=build_attempt_digest(messages)`.
3. `_execute_sub_tasks` records the attempt (the checkpoint persists the digest),
   then calls `_evaluate_failure`. The evaluate prompt includes the digest, so the
   LLM writes an informed `recovery_task`.
4. `_attempt_recovery` runs the recovery sub-agent with the pre-step state plus
   `Step N: an earlier attempt FAILED — ... Actions that attempt took: ...`.
5. If the recovery passes, the failed line and the recovery line are appended to
   the state. The retried step sees both and checks the screen before repeating
   typed input.
6. If the run is interrupted and resumed later, `_rebuild_state_lines` reproduces
   the same lines from the checkpoint.

## Alternatives considered
- **LLM summary of the failed transcript.** Rejected: it adds a call, cost and a
  failure mode, and the summarizers were just removed for losing information.
- **Pass the full failed transcript to the recovery sub-agent.** Rejected. It can
  be hundreds of KB (tool results up to 100k chars each), the reasoning items are
  tied to the earlier model turn, and it would mix two tasks' instructions.
- **A setting for the cap.** Rejected for now (see Design). It would be easy to
  promote later if tuning proves necessary.
- **Bump `CHECKPOINT_VERSION`.** Rejected: it would make every stored checkpoint
  resume from step 1, which is what the checkpoint docstring forbids.
- **Keep only the last N calls.** Rejected. Early calls (typing, submits) are the
  ones a retry must not duplicate, so the digest keeps head and tail.

## Risks and mitigations
- **Secrets in arguments.** `browser_type`/`type` text (for example passwords from
  the Application context) now reaches the evaluate prompt and recovery prompts,
  and is persisted in `execution_checkpoint`. The same arguments are already
  written verbatim to the case log by the `[Tool Call]` debug line in
  `run_tool_loop`, already stored on the same row, and already sent to the same
  LLM provider in the original transcript. So this adds no new exposure class.
  String values are also capped at 150 chars. Not shown in the UI or TestRail.
- **Prompt bloat.** The digest is capped at 6000 chars, it is added only after a
  passed recovery, and the recovery count is bounded (default 1). The verdict and
  UI are untouched.
- **Rebuild drift.** If the live loop and `_rebuild_state_lines` disagree, a resumed
  case sees different state. Mitigation: both go through
  `format_failed_attempt_state`. Verification includes a round-trip check.
- **Misleading digest.** A tool result's head may not show success or failure. The
  wording says "effects may still be in place; check the screen", not "these
  succeeded".
- **XSS.** None: the digest is never rendered in templates.
- **Old checkpoints.** The key is optional and defaults to `""`; the version is
  unchanged.

## Implementation steps
Small, mostly sequential changes (types, then producer, then consumers). Implement
directly; no parallel sub-agents are needed.

1. Add `SubTaskResult.actions_digest: str = ""`. (files: `agents/types.py`)
2. Create `build_attempt_digest` and its constants. (files:
   `agents/attempt_digest.py`)
3. Set `actions_digest` on fail results in `_parse_sub_task_result`. (files:
   `agents/services/sub_agent.py`)
4. Add the digest block and recovery-task sentence to `build_evaluate_prompt`, and
   add `format_failed_attempt_state`. (files:
   `agents/services/orchestrator_prompts.py`)
5. Thread `failed_result` into `_attempt_recovery`, build the recovery state and
   append the failed line on a passed recovery. Mirror this in
   `_rebuild_state_lines`. (files: `agents/services/orchestrator.py`)
6. Persist the digest and parse it leniently. (files: `agents/checkpoint.py`)
7. Document the new `SubTaskResult` field and the digest flow in the agents notes,
   in one short paragraph. (files: `agents/CLAUDE.md`)
8. Run `pre-commit run --all-files` (black, isort, mypy strict).

## Verification
- `mypy .` and `pre-commit run --all-files` are clean.
- Shell smoke check of the digest (pure, so no Django setup is needed:
  `python -c` from the repo root). Build a `ChatMessage` list with:
  - an assistant turn with empty content and two tool calls (one a long
    `browser_type` text, one `take_screenshot`) and their tool results;
  - a tool result as a `(TextContent, ImageContent)` tuple;
  - a tool call with no result;
  - 200 repeated `wait` calls.

  Assert that:
  - the output is at most `ATTEMPT_DIGEST_MAX_CHARS`;
  - it contains the first and the last call and one `tool calls omitted` marker;
  - no base64 appears, and `[image]` and `(no result recorded)` are present;
  - an empty transcript gives `""`.
- Checkpoint smoke check (`python manage.py shell -c`):
  - a checkpoint whose failed step attempt carries a digest round-trips through
    `checkpoint_to_json` / `checkpoint_from_json` with the digest intact;
  - the same JSON with `actions_digest` removed (an old checkpoint) still parses,
    with `""`.
- Rebuild parity (shell): for the attempts `step 2 fail (with digest)`,
  `recovery 2 pass` and `step 2 pass`, `_rebuild_state_lines(..., next_step=3)`
  gives the lines the live loop would append, in order: step 1 line, failed line
  with the digest, recovery line, step 2 line.
- Manual live check (needs a runner and a vision worker): run a case with one step
  given a deliberately short budget, for example a slow download at the minimum
  timeout. In the case log, confirm that:
  - the evaluate decision references actions from the first attempt;
  - the recovery sub-agent's first action reflects them (it resumes observing, it
    does not restart);
  - the retry does not repeat typed input.

  Confirm the case page and TestRail comment show no digest.
- Run the `reviewer` and `verifier` agents in parallel on the diff, with the
  original bug report and this plan.

## Open questions
None blocking. Decisions taken (change them in review if wanted):
- Use module constants, not a setting, for the size caps (6000 chars total).
- Keep the digest out of the verdict, the UI, TestRail and the "continue"/"stopped"
  state lines.
- Persist the digest in the checkpoint as an optional key, with no version bump.
- Make the failed-attempt line permanent in the state after a passed recovery,
  consistent with the existing permanent recovery line.

## Changelog
- 2026-10-24: created
- 2026-10-24 (review 1): the full digest no longer stays in `state_lines` for
  every later step (about 6 KB re-sent on every later LLM call). `state_lines`
  keep the one-line `format_failed_attempt_state(..., with_actions=False)`, and
  `_Progress.retry_notes[step]` holds the version with actions, shown only to the
  recovery and to the retry of that step. `_rebuild_state_lines` became
  `_rebuild_state`, which returns both.
- 2026-10-24 (verify 2): kept on purpose: a second recovery of the same step sees
  both failed attempts' digests (the first one from the retry note, the second as
  its own note). That is at most about 12 KB, in that one recovery prompt only,
  and both attempts' actions are relevant to it.
- 2026-10-24 (review 2): accepted. A case interrupted during a recovery, before
  the recovery attempt was recorded, resumes at the failed step without that
  attempt's digest. The in-flight digest on resume is already a Non-goal, and
  the restore sub-task re-establishes the state first.
