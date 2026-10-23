# refactor: Remove the LLM context/output summarizers, cap tool results, send `truncation: "auto"`

Status: Done
Created: 2026-10-23
Request: Delete the context summarizer and the output summarizer entirely. In
their place, cap each tool result at a generous character limit with plain
head/tail truncation (no LLM), applied in one place in the tool loop. Send
`"truncation": "auto"` on every Responses API request so an over-long input loses
its oldest items instead of failing with a 400.

## Context
- `agents/services/context_summarizer.py` (`summarize_context_if_needed`) and
  `agents/services/output_summarizer.py` (`summarize_output`) date from the 8k/32k
  token models (`docs/specs/011.Sub-Agent Architecture.md`). Every role now runs on
  `gpt-5.6-sol` (`OPENAI_MODEL`): 1,050,000-token window, 922k input, 128k output
  (`agents/services/llm_config.py` `MAX_OUTPUT_TOKENS = 128_000`).
- The only callers are in `agents/services/agent_loop.py`. `run_tool_loop` calls
  `summarize_context_if_needed` on every iteration. `_build_tool_result_message`
  calls `summarize_output` unless `ToolResult.verbatim` is set.
  `summarize_context_if_needed` is also re-exported from `agents/services/__init__.py`
  but has no other importer.
- `run_tool_loop` is shared by the tester's sub-agents (`sub_agent.py`, via
  `_run_agent_loop`), `run_agent`, and the machine setup chat
  (`machine_setup/services/run_turn.py`).
- The context summarizer is the confirmed cause of the setup chat forgetting the
  user's current request. Past 20k chars it keeps `messages[:2]` (the system prompt
  plus the *first-ever* user message) and the last 6 messages, and replaces the
  rest with a `[Context Summary]` user message. `run_turn._on_transcript` then
  persists that shrunken transcript as `SetupConversation.llm_transcript`, so the
  loss is permanent.
- Settings: `OUTPUT_SUMMARIZE_THRESHOLD`, `OUTPUT_SUMMARIZE_CHUNK_SIZE`,
  `CONTEXT_SUMMARIZE_THRESHOLD`, `CONTEXT_PRESERVE_LAST_MESSAGES` and
  `CONTEXT_SUMMARIZE_CHUNK_SIZE` are in `auto_tester/settings.py` (lines 316-333)
  and `example.env` (lines 65-72). Nothing else reads them.
- `ToolResult.verbatim` (`agents/types.py`) is set only in
  `machine_setup/services/setup_tools.py` (`_dispatch`, two places), and read only
  in `_build_tool_result_message`. Those project-data tools already bound their
  own output: `case_catalog.py` has a 24k budget, and the application context is
  at most `PROJECT_PROMPT_MAX_LENGTH = 12000`. All of it fits well under the new
  cap.
- Tool outputs with no size bound today: `execute_command`, `wait_for_command`,
  `send_command_input` and `browser_get_page_content`
  (`agents/services/tools_controller.py`, `projects/services.py`
  `controller_*`). Screenshots never enter the agent context; they go to one-shot
  vision calls that return text.
- `agents/services/llm_client._build_payload` builds the Responses payload with
  `store: False` and `include: ["reasoning.encrypted_content"]`. The input is
  fully stateless (no `previous_response_id`), and `instructions` carries the
  system prompt.
- The `[Tool Result]` debug line (`agent_loop.py`, `_debug_log`) goes to
  `config.on_log`. For test runs that is `projects/services._build_log_callback`,
  which appends to `TestRunTestCase.logs` and broadcasts over the WebSocket. For
  setup it is `_log_tool_debug` (DEBUG only). `execute_command` and
  `wait_for_command` already log the raw command output on their own
  (`tools_controller.py`).
- **Not a target:** `agents/services/tools_search.py` has its own LLM summary of
  web search results (`[AI Summary]` header, `ToolContext.summarizer_config`). It
  is a feature of the `web_search` tool, not a context-management mechanism.
- Prompts (`prompt_parts.py`, `sub_agent_prompts.py`, `orchestrator_prompts.py`,
  `setup_prompt.py`) never mention `[AI Summary]` or `[Context Summary]`. Their
  uses of "summary" are step result summaries (`SubTaskResult.summary`) and are
  unrelated. The orchestrator works from `SubTaskResult`, not from sub-agent
  transcripts, so it does not depend on the removed behaviour.

## Goals
- No LLM call is made to summarize context or tool output. Transcripts are passed
  to the model unchanged and persisted unchanged.
- Each tool result is capped by character count in exactly one place.
- Every Responses request carries `"truncation": "auto"`.
- Dead code, settings and env entries are removed. Comments that describe
  summarization are corrected.

## Non-goals
- The `web_search` result summary (`tools_search.py`) and
  `ToolContext.summarizer_config` stay as they are. That field now feeds only
  `web_search`, and renaming it would be unrelated churn.
- No token counting and no client-side context budgeting. Overflow is left to
  `truncation: "auto"`.
- No change to the raw command-output log lines in `tools_controller.py`.
- No edits to historical docs (`docs/specs/`, `docs/impl/`, Done plans such as
  `feat-machine-setup-s0.md`). The `docs/changed/` note records the change.

## Design

### Data model
None. No migration. Existing `SetupConversation.llm_transcript` rows may contain
an old `[Context Summary]` user message. That is plain text the model reads like
any other message, so no data migration is needed. Users can Reset a
conversation that was already damaged.

### Services
**Removed**
- `agents/services/context_summarizer.py` (whole module).
- `agents/services/output_summarizer.py` (whole module).
- The `summarize_context_if_needed` import and its `__all__` entry in
  `agents/services/__init__.py`.
- `ToolResult.verbatim` and its comment in `agents/types.py`, plus the two
  `verbatim=True` arguments in `machine_setup/services/setup_tools.py`. With the
  LLM summary gone, every result goes through the same cap. The setup data tools
  are far below the cap, so marking them separately would do nothing.

**Changed: `agents/services/agent_loop.py`**
- `run_tool_loop`: drop the `summarize_context_if_needed` call. `messages` is only
  appended to and is never replaced. Update the docstring ("deadline, cancellation
  and summarization rules" becomes "deadline, cancellation and tool-result cap").
- `_build_tool_result_message(tool_result: ToolResult) -> ChatMessage`: drop the
  `summarizer_config` keyword and build the content with `_cap_tool_result`. Drop
  the now-unused `LLMConfig` import if nothing else uses it.
- New private `_cap_tool_result(content: str) -> str`:
  - If `len(content) <= settings.AGENT_TOOL_RESULT_MAX_CHARS`, return the content
    unchanged.
  - Otherwise keep the first half and the last half of the budget, joined by a
    marker that states the facts:
    `\n\n[... {omitted:,} of {total:,} characters omitted from the middle of this tool result ...]\n\n`.
  - The split is 50/50 on purpose. For command output the useful part (the error
    and `Exit code:`) is at the end. For page content it is at the start. The old
    75/25 split cut the tail of failing commands.
  - The result is at most cap plus the marker length. The function is pure, has no
    LLM call and no logging beyond one `logger.info` with the sizes.
- New module constant `_TOOL_RESULT_LOG_PREVIEW_CHARS = 2000`. The `[Tool Result]`
  debug line logs at most this many characters of the model-facing content, plus
  `… ({len} chars total)` when it is longer. Without this, every large result
  (up to 100k chars) would be appended to `TestRunTestCase.logs` and pushed over
  the run WebSocket. Today that line is at most about 2k chars, because it logs
  the summary, so this preview keeps the case log the same size. Command output
  is already logged raw elsewhere.
- `run_agent` keeps building `summarizer_config` for `web_search`.

Why a private helper and no new module: it is one small function with a single
caller. Putting it next to that caller keeps the "one place" property visible. It
is cap policy, not a generic utility, so it does not belong in `utils`.

**Changed: `agents/services/llm_client.py`**
- `_build_payload`: add `"truncation": "auto"` to the base payload, with a short
  comment (see Research below). This applies to every caller of
  `send_chat_completion`: the agent loop, the orchestrator, vision, the verifier,
  the web search summary and the refiner.

**Comment fixes**
- `agents/types.py` `TranscriptCallback` comment: remove "(which can shrink after
  a context summary)". The transcript only grows.
- `machine_setup/services/run_turn.py` line 113-114 comment: "(possibly
  summarized)" becomes "(possibly capped)".
- `machine_setup/services/setup_tools.py` module docstring: keep "return
  verbatim results" only if it is still accurate. Otherwise reword to "return
  their results directly".

### Pages and forms
None.

### Background work and WebSockets
No new tasks, events or controller actions. The controller protocol is
unchanged, so no `CLIENT_VERSION` or capability change. Case-log WebSocket
messages keep their current size because of the log preview.

### Settings / infrastructure
- Remove the five summarizer settings from `auto_tester/settings.py` and their
  block from `example.env`.
- Add the setting below in the Agent block of `settings.py`, with a comment, and
  add `AGENT_TOOL_RESULT_MAX_CHARS=100000` to `example.env` under `# Agent`:
  `AGENT_TOOL_RESULT_MAX_CHARS: int = config("AGENT_TOOL_RESULT_MAX_CHARS", default=100_000, cast=int)`
  - Why 100,000: that is about 25k tokens for prose and 35-50k for symbol-heavy
    command output. About 20 maximum-size results fit in the 922k input budget
    before `truncation: "auto"` would drop anything, and ordinary outputs are far
    below the cap. It also stays below the roughly 350k-char size at which a
    community report saw a single item being cut under `truncation: "auto"` (see
    Risks).
  - Why a setting and not a constant: it replaces env-tunable thresholds and it
    depends on the model's window, which is itself env-configured
    (`OPENAI_MODEL`). Following the pattern of `SEARCH_PAGE_MAX_LENGTH`.
- Leftover `OUTPUT_SUMMARIZE_*` and `CONTEXT_*` lines in a local or deployed
  `.env` are harmless. python-decouple reads only the keys that `settings.py`
  asks for. Operators may delete them.

### Data flow
The loop calls the LLM with the full, unmodified `messages` (the payload now
includes `truncation: "auto"`). For each tool call it runs `dispatch`, then
`_cap_tool_result(content)`, then builds a `ChatMessage(role="tool")`. It logs a
2k preview and appends the message (`on_transcript` persists the unmodified
transcript for setup). On the next iteration the whole transcript is sent again.
If the input ever exceeds the window, the API drops the oldest input items. The
`instructions` (system prompt, which also contains the task for tester and
sub-agent prompts) are not part of the input array.

### Research (OpenAI Responses API, `httpx~=0.28.1`, no SDK)
- The current API reference (`developers.openai.com/api/reference/resources/responses/methods/create`)
  defines `truncation` as `"auto" | "disabled"`, defaulting to `disabled`:
  - `auto`: "If the input to this Response exceeds the model's context window
    size, the model will truncate the response to fit the context window by
    dropping items from the beginning of the conversation."
  - `disabled`: a 400 error.
- The reference documents no restriction tied to `store` or
  `previous_response_id`. With `store: false` the "conversation" is simply the
  `input` array we send, so the oldest items there are dropped. `instructions` is
  a separate field and is not an input item.
- The setup chat's current user message is always the last item, so it survives
  truncation. A tester sub-agent's task is in both `instructions` and the first
  user item, so losing the first item does not lose the task.

## Alternatives considered
- **Keep the summarizers with raised thresholds:** the summaries still lose
  information and still cost extra LLM calls. The context summarizer's
  keep-first-user-message rule is what caused the setup bug, and any threshold
  only delays it.
- **Client-side token budgeting:** this needs a tokenizer for an unreleased model
  family, and it duplicates what `truncation: "auto"` already does.
- **Cap in each tool (`tools_controller.py`, `projects/services.py`):** that
  spreads the policy over many functions, and new tools would be uncapped by
  default. The loop is the single choke point.
- **Keep `verbatim` for setup data tools:** they are below the cap by
  construction, so the flag would mark nothing. Removing it is cleaner.

## Risks and mitigations
- **Cost and latency per call grow.** Long runs re-send the full history (the old
  limit was about 20k chars). Mitigation: transcripts are now append-only, so
  OpenAI prompt caching hits on the stable prefix. Iteration and timeout limits
  are unchanged. Watch `usage(prompt=…)` in the `LLM response` log line during
  smoke checks.
- **`truncation: "auto"` may act earlier than documented.** A community report
  ([forum](https://community.openai.com/t/ultra-aggressive-truncation-behavior/1376390),
  gpt-5.2, no staff reply) saw a single input item of 400k chars or more cut to
  about 72k tokens, while items of 350k chars or less were untouched. Mitigation:
  the 100k-char per-result cap keeps every tool item far below that size. In
  practice auto truncation should only matter near the real window.
- **Setup transcript row growth.** `SetupConversation.llm_transcript` now grows
  without bound across turns, and it is written twice per round. The
  `feat-machine-setup-s0` plan accepted "a few hundred KB". Now it can reach a few
  MB. Mitigation: this is acceptable for a TextField or JSON column, and Reset is
  the escape hatch. Revisit only if writes show up as slow.
- **Setup transcript write cost is quadratic per turn.** `run_turn._persist_transcript`
  rewrites the whole JSON twice per round, so a 1 MB transcript over 200 rounds is
  about 400 MB of UPDATE traffic. Accepted for now (review pass 1); the later fix
  is append-only message rows.
- **Server-side truncation leaving a `function_call_output` without its
  `function_call`.** The API applies the truncation itself, so it is expected to
  drop pairs consistently. It is unverified, and the failure would show as an
  AIServiceError (the case is Blocked, not failed). No client mitigation is
  planned.
- **Security:** no auth, secrets or template changes. Tool output can contain
  secrets. The log preview reduces how much of it lands in the case log compared
  with logging full results.

## Implementation steps
The change is small and the files depend on each other (removing `verbatim`
breaks `setup_tools.py`, and the new setting is read by `agent_loop.py`), so it is
implemented directly by one agent. Step 2 is the only independent part and may
run in parallel.

1. Settings first (files: `auto_tester/settings.py`, `example.env`): remove the
   five summarizer settings, and add `AGENT_TOOL_RESULT_MAX_CHARS` with its
   comment.
2. Payload (file: `agents/services/llm_client.py`): add `"truncation": "auto"` in
   `_build_payload`, with a comment.
3. Loop (file: `agents/services/agent_loop.py`): remove both summarizer imports
   and the context call. Add `_cap_tool_result` and the log preview, simplify
   `_build_tool_result_message`, and fix the docstring.
4. Delete the modules and exports (files: `agents/services/context_summarizer.py`,
   `agents/services/output_summarizer.py`, `agents/services/__init__.py`).
5. Remove `verbatim` (files: `agents/types.py`,
   `machine_setup/services/setup_tools.py`), and fix the comments in
   `agents/types.py` (`TranscriptCallback`) and
   `machine_setup/services/run_turn.py`.
6. Run `pre-commit run --all-files`.

## Verification
- Static checks:
  - `pre-commit run --all-files` (isort, black, mypy strict for the server and
    `controller_client/`) is clean.
  - `python manage.py check` passes.
  - `grep -rn "summarize_output\|summarize_context_if_needed\|context_summarizer\|output_summarizer\|OUTPUT_SUMMARIZE\|CONTEXT_SUMMARIZE\|CONTEXT_PRESERVE\|verbatim=" --include='*.py' --include='*.env' .`
    (excluding venvs) returns nothing.
- Payload: `python manage.py shell -c` builds `_build_payload(build_llm_config(), (ChatMessage(role="user", content="hi"),), ())`
  and the result contains `truncation == "auto"`.
- Cap: in the shell, `_cap_tool_result("a"*50 + "b"*200_000 + "c"*50)` starts
  with `a`, ends with `c`, has length cap plus marker, and the marker states
  200,100 total. A 1,000-char string comes back unchanged.
- Setup chat regression (the original bug):
  1. In the machine setup chat, hold a conversation of more than 10 messages with
     tool use, then ask a new, distinct request.
  2. Confirm the agent works on the new request.
  3. Confirm `SetupConversation.llm_transcript` still holds the early messages
     and contains no `[Context Summary]`.
- Large output: in a test run (or setup chat), have the agent run
  `python -c "print('x'*300000)"` and then `seq 1 200000; exit 3`.
  1. The model sees the marker, and the second result still ends with
     `Exit code: 3`.
  2. The case log's `[Tool Result]` line is at most about 2k chars with the
     "chars total" suffix.
  3. A web search still returns its `[AI Summary]` block.
- Normal regression: run one ordinary test case end to end and check that it gets
  a verdict.
- Then run the `reviewer` and `verifier` agents in parallel.

## Open questions
None.

## Changelog
- 2026-10-23: created
- 2026-10-23: review pass 1. `_log_preview` takes the capped string;
  `_build_tool_result_message` is inlined; the cap clamps the limit to at least 1.
  The quadratic setup transcript write is recorded under Risks, not fixed (no
  behaviour change wanted here). A result 1-86 chars over the cap grows by the
  marker: accepted, as planned (cap plus marker length).
