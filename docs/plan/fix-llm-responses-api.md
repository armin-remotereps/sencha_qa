# fix: Move the LLM client to /v1/responses so gpt-5.6-sol can reason while calling tools

Status: Done
Created: 2026-10-22
Request: Every agent call fails with HTTP 400 from OpenAI: "Function tools with
reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions. To use
function tools, use /v1/responses or set reasoning_effort to 'none'." We never send
`reasoning_effort`. The model's default reasoning (medium) is enough to trigger the
error. The user rejected `reasoning_effort='none'`: the model must keep reasoning
while it uses function tools. So the hand-rolled httpx client has to switch from Chat
Completions to the Responses API. The public call, history model and callers should
change as little as possible. Reasoning items must carry across tool-call turns, and
settings and `example.env` must stay in sync.

## Context
- `agents/services/llm_client.py`
  - `_build_payload` sends `model`, `messages`, `max_completion_tokens`, `tools` and
    `tool_choice="auto"`.
  - `send_chat_completion(config, messages, tools=())` POSTs to
    `config.endpoint_url`. It maps transport errors to `AIServiceError` (`TIMEOUT`,
    `CONNECTION`), HTTP >= 400 to `_status_error`, and parse errors to
    `BAD_RESPONSE`. It logs `finish_reason` and usage.
- `agents/services/llm_serializer.py` is Chat Completions specific:
  - `_serialize_messages` writes `role`/`content`, `tool_calls[{id, type, function{name, arguments}}]` and `tool_call_id`.
  - Images are written as `image_url` data URLs.
  - `_serialize_tools` writes the nested `{"type":"function","function":{...}}` shape.
  - `_parse_response` reads `choices[0].message`, `reasoning_content` (never actually returned by OpenAI), `finish_reason` and `usage.prompt_tokens/completion_tokens`.
- `agents/services/llm_config.py`: `build_llm_config()` sets `MAX_OUTPUT_TOKENS = 128_000`, which matches the documented gpt-5.6-sol output ceiling.
- `agents/types.py`:
  - `ChatMessage(role, content, tool_calls, tool_call_id)`.
  - `ToolCall(tool_call_id, tool_name, arguments)`.
  - `LLMConfig(model, api_key, endpoint_url, max_tokens)`.
  - `LLMResponse(message, finish_reason, usage_prompt_tokens, usage_completion_tokens, reasoning_content)`.
  - `ContentPart = TextContent | ImageContent`.
- `agents/exceptions.py`: `AIServiceError` / `AIServiceErrorKind`. An `AIServiceError` is
  an `InfrastructureError`, so the case is marked Blocked (`docs/changed/2026-10-22-blocked-result.md`).
- Settings:
  - `auto_tester/settings.py:234-248` has `OPENAI_BASE_URL`, which defaults to the full `.../v1/chat/completions` endpoint. The name says "base URL" but it holds a whole endpoint URL.
  - It also has `OPENAI_MODEL` (default `gpt-5.6-sol`) and `OPENAI_REQUEST_TIMEOUT=900`.
  - `example.env:26` matches. The developer's local `.env:23` also still points to `/v1/chat/completions`.
  - `docker-compose.yml` services load `env_file`, so they follow `.env`.
- Callers of `send_chat_completion` (all checked):

  | Caller | Tools | Images | History | Reads |
  |---|---|---|---|---|
  | `agents/services/agent_loop.py:203` (also used by `sub_agent.py`) | yes | no | yes: appends `response.message`, then `role="tool"` messages | `reasoning_content` (only for the `[Thinking]` debug log), `message.content`, `message.tool_calls` |
  | `agents/services/orchestrator.py:173`, `:407`, `:467` | no | no | `:467` is multi-turn, but it rebuilds the assistant turn as `ChatMessage(role="assistant", content=raw_text)` | `message.content` |
  | `agents/services/vision_qa.py`, `action_verifier.py` (x2), `controller_element_finder.py` | no | yes, user message `TextContent` + `ImageContent` | no | `message.content` |
  | `agents/services/browser_element_finder.py`, `tools_search.py`, `output_summarizer.py`, `context_summarizer.py`, `projects/prompt_refiner.py` | no | no | no | `message.content` |
  | `agents/services/__init__.py` | - | - | - | re-exports the name |

  - No caller reads `finish_reason` or `usage_*`. Only `llm_client` logs them.
  - Tool results are always `str`, because `summarize_output` returns `str`.
  - Nothing persists or serialises `ChatMessage` lists. `sub_agent._extract_last_assistant_text` and `projects/services.py:2790` only read `role`/`content`.
- `agents/services/context_summarizer.py`:
  - Keeps `messages[:2]` (system and user) and replaces the middle with one `role="user"` summary.
  - Keeps the tail intact. It never splits an assistant tool-call message from its tool results (`_split_messages`).
  - `_estimate_message_size` counts content and tool-call arguments.

## Root cause
Confirmed by the provider error text and the model page:
- gpt-5.6-sol reasons by default (`reasoning.effort` default `medium`).
- On `/v1/chat/completions`, OpenAI rejects function tools combined with reasoning on this model.
- `agent_loop` always sends tools, so every agent and sub-agent iteration gets a 400. That surfaces as `AIServiceError(REJECTED)`, and the case is marked Blocked.
- Tool-less callers (summarizer, vision, orchestrator) are not affected by this rule. They move too, so one client serves one protocol.

## Goals
- Every LLM call goes to `POST /v1/responses` with the model's reasoning enabled, including the calls that send function tools.
- Reasoning items returned with function calls are replayed on the next turn (stateless, `store: false`). This follows OpenAI's recommendation for best tool-calling quality with reasoning models.
- Callers keep calling `send_chat_completion(config, messages, tools)` with the same `ChatMessage` history and the same `LLMResponse` fields. No caller other than `agent_loop` should need a code change, and `agent_loop` should need none either.
- Error mapping (`AIServiceErrorKind`, Blocked behaviour, no URL or key in user-facing text) is unchanged.
- `OPENAI_BASE_URL` defaults to `https://api.openai.com/v1/responses`. `example.env` matches.

## Non-goals
- Streaming, background mode, `previous_response_id`, the Conversations API, server-side compaction.
- Strict function schemas (we send `strict: false`; see Design).
- Renaming `send_chat_completion` or `LLMResponse` fields across callers.
- Built-in OpenAI tools (web search, computer use). The agent keeps its own tools.
- Per-role reasoning effort. One model and one setting, as in b70fefe.
- Supporting non-OpenAI Chat-Completions-compatible endpoints. After this change the client speaks only the Responses protocol.

## Design

### Library facts relied on
Checked 2026-10-22 against developers.openai.com: API reference `responses/create`, the reasoning guide, the function-calling guide, the migrate-to-responses guide and the GPT-5.6 model page. Client is `httpx~=0.28.1` (no SDK), so only raw JSON shapes matter.

- **Request: `input`** is an array of items:
  - Message items `{"role": "user"|"assistant"|"system"|"developer", "content": str | [parts]}`. Assistant messages may carry `phase` (`commentary`/`final_answer`). For gpt-5.3-codex and later models, `phase` "should be preserved and resent".
  - Input parts: `{"type":"input_text","text"}` and `{"type":"input_image","image_url": "<https or data: URL>", "detail": "low"|"high"|"auto"|"original"}` (default `auto`).
  - `{"type":"function_call","call_id","name","arguments": <JSON string>}`.
  - `{"type":"function_call_output","call_id","output": <string>}`.
  - `{"type":"reasoning","id","summary":[{"type":"summary_text","text"}],"encrypted_content"}`.
- **Request: other fields:**
  - `instructions`: a system/developer message placed first.
  - `tools`: flat `{"type":"function","name","description","parameters","strict"}`. If `strict` is omitted, the API attempts strict normalisation and falls back. `strict: false` forces non-strict, which matches today's Chat Completions behaviour.
  - `tool_choice`: `"auto"` | `"none"` | `"required"` | ...
  - `max_output_tokens`: counts reasoning and visible tokens.
  - `reasoning: {"effort": none|low|medium|high|xhigh|max, "summary": auto|concise|detailed}`. gpt-5.6-sol supports `none..max` but not `minimal`. The default is `medium`.
  - `store`: defaults to **true**, meaning responses are retained for at least 30 days.
  - `include: ["reasoning.encrypted_content"]`: still accepted. With `store:false`, encrypted content is returned by default anyway.
  - `parallel_tool_calls`.
- **Response:**
  - `status`: `completed|failed|in_progress|cancelled|queued|incomplete`.
  - `incomplete_details.reason`: `max_output_tokens|content_filter|max_messages|steered`.
  - `error {code, message, ...}`.
  - `output[]` items:
    - `reasoning` (as above, with `encrypted_content`).
    - `message` with `role:"assistant"`, `phase`, and `content[]` of `{"type":"output_text","text"}` or `{"type":"refusal","refusal"}`.
    - `function_call` with `id`, `call_id`, `name`, `arguments`, `status`.
  - `usage`: `input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`, `total_tokens`.
  - `output_text` is an SDK convenience and **not** a raw JSON field, so we must walk `output[]`.
- **Function calling with reasoning:**
  - Pass back the reasoning items that came with the function calls, together with the outputs.
  - When several consecutive calls were made, pass back all reasoning, `function_call` and `function_call_output` items since the last `user` message.
  - Stateless (`store:false`) use means preserving and replaying every returned reasoning item.
- **Reasoning summaries** need a verified OpenAI organisation. Asking for `reasoning.summary` on an unverified org returns 400 `unsupported_value` on `reasoning.summary` (LiteLLM #16032, community reports). Effort alone does not need verification.

### Chosen state strategy: stateless replay (`store: false` + encrypted reasoning items)
Recommended over `previous_response_id`, because:
- We already rebuild history ourselves on every call. `context_summarizer` rewrites the middle of the history, and the orchestrator's evaluate conversation rebuilds assistant turns. A `previous_response_id` chain breaks as soon as the history is edited, and still bills all prior input.
- `previous_response_id` needs `store: true`. That keeps screenshots of the customer's application, prompts and TestRail text on OpenAI for 30+ days. Stateless keeps today's data footprint. It also works if the org is or becomes ZDR, where `store:false` is enforced.
- No new state (response ids) to keep across Celery task retries or sub-agents.

### Data model (in-memory types, `agents/types.py`; no DB changes, no migrations)
- New frozen dataclass `ReasoningItem`:
  - `item_id: str`
  - `encrypted_content: str`
  - `summary: tuple[str, ...]` (summary texts; empty when summaries are off)

  It is opaque to everyone except the serializer.
- `ChatMessage` gains two optional fields with defaults, so every existing constructor call stays valid:
  - `reasoning: tuple[ReasoningItem, ...] | None = None`: set only on assistant messages parsed from a response.
  - `phase: str | None = None`: the assistant message `phase` echoed back, as the docs ask for gpt-5.3+.
- `LLMResponse` gains `usage_reasoning_tokens: int = 0`, for the log line and for proving the fix (see Verification).
  - Existing fields keep their names: `usage_prompt_tokens` <- `input_tokens`, `usage_completion_tokens` <- `output_tokens`.
  - `reasoning_content` <- joined summary texts, or `None`.
- `ToolCall.tool_call_id` now carries the Responses `call_id`, not the item `id`. The field name stays, and `tool_registry` / `agent_loop` already treat it as opaque.
- `LLMConfig` gains `reasoning_summary: str | None` (no effort field, see Decisions), filled from settings by `build_llm_config()`. They live on the config, not read from settings inside the serializer, so the request builder stays a pure function of its inputs.
- Why no raw-dict replay: storing `response.output` verbatim as `tuple[dict[str, object], ...]` would be the most literal reading of the docs. The typed `ReasoningItem` plus `phase` keeps `ChatMessage` the single typed source of truth. It also avoids two copies of the same tool calls (typed `tool_calls` and raw items) that could drift. The cost is that we re-emit items in canonical order (reasoning -> message -> function calls). That is the order the model produces within one response.

### Services
Same modules; no new service modules. The serializer and client remain the only protocol-aware code.

- `agents/services/llm_serializer.py` (rewritten for the Responses shapes, same private function names where sensible):
  - `_serialize_input(messages) -> tuple[str | None, list[MessageDict]]` replaces `_serialize_messages`. It returns `(instructions, input_items)`:
    - **Leading `system` message** -> `instructions` (string content; if the content is multimodal, its text parts are joined). Any later `system` message, of which none exist today, -> a `{"role":"developer"}` message item.
    - **`user` message** -> `{"role":"user","content": str}`. Multimodal content -> `[{"type":"input_text"...}, {"type":"input_image","image_url":"data:<media_type>;base64,<data>"}]`. `detail` is omitted (`auto`, same as today's Chat Completions default).
    - **`assistant` message**, in this order:
      1. Each `ReasoningItem` as `{"type":"reasoning","id","summary":[...summary_text],"encrypted_content"}`.
      2. A message item `{"role":"assistant","content": text}` (plus `phase` when set) if the content is non-empty.
      3. One `{"type":"function_call","call_id","name","arguments": json.dumps(args)}` per tool call.

      If the message has neither content nor tool calls, emit nothing, and drop its reasoning items too. A reasoning item without its following item is rejected by the API, and an empty assistant message has no valid encoding.
    - **`tool` message** -> `{"type":"function_call_output","call_id": tool_call_id,"output": content}`. The content is always `str` today. Multimodal tool output is out of scope.
  - `_serialize_tools(tools)` -> flat `{"type":"function","name","description","parameters":{"type":"object","properties",...,"required"},"strict": False}`. `strict: False` keeps today's semantics. The schemas have optional params and no `additionalProperties:false`, so letting the API normalise them to strict could change argument behaviour.
  - `_parse_response(data) -> LLMResponse` (or a small parse-result shape the client turns into `LLMResponse`). It walks `output[]`:
    - `reasoning` -> `ReasoningItem` (skip it if `encrypted_content` is missing); collect summary texts.
    - `message` -> join the `output_text` texts, record `phase`, and treat `refusal` text as content so callers see it.
    - `function_call` -> `ToolCall(call_id, name, json.loads(arguments))`. Bad JSON gives `{}`, as today.
    - Unknown types are ignored.

    It builds `ChatMessage(role="assistant", content=text or None, tool_calls=... or None, reasoning=..., phase=...)`.
  - **`finish_reason` mapping**, which keeps Chat Completions vocabulary for log continuity:
    - `completed` with function calls -> `"tool_calls"`.
    - `completed` otherwise -> `"stop"`.
    - `incomplete` + `max_output_tokens` -> `"length"`.
    - `incomplete` + `content_filter` -> `"content_filter"`.
    - Other `incomplete` -> `"incomplete:<reason>"`.
    - `failed` / `cancelled`, or a non-null top-level `error` -> raise `ValueError` with `error.code`/`error.message`. The client maps it to `AIServiceError(BAD_RESPONSE, "AI service returned an unusable response: ...")`, truncated as today.
    - A missing `output` list -> `ValueError`, as with missing `choices` today.
- `agents/services/llm_client.py`:
  - `_build_payload` returns:
    - `model`, `input`, `instructions` (when present), `max_output_tokens = config.max_tokens`
    - `store: False`, `include: ["reasoning.encrypted_content"]`
    - `reasoning: {"summary": ...}` only when `config.reasoning_summary` is set
    - `tools` + `tool_choice: "auto"` only when tools are given

    `parallel_tool_calls` is not sent: the API default allows several calls, and `agent_loop` already loops over all of them.
  - `send_chat_completion` keeps its name and signature. The response log adds `reasoning_tokens`; the status shows through the mapped `finish_reason`.
  - `_status_error` / `_truncate` are unchanged. Responses error bodies use the same `{"error":{...}}` envelope, and we only show truncated text.
  - The comment on the `except` that mentions "chat-completions contract" is updated.
- `agents/services/llm_config.py`: `build_llm_config()` adds `reasoning_summary` from settings. `MAX_OUTPUT_TOKENS` stays 128_000, the documented ceiling, and is used as `max_output_tokens`.
- `agents/services/context_summarizer.py`: no change. Reasoning travels inside the assistant `ChatMessage`, so `_split_messages` already keeps it with its tool calls and results. After summarisation the summary is a `user` message and the tail is "since the last user message", which is exactly what the docs ask us to replay. `_estimate_message_size` does not count `encrypted_content`. That is acceptable: the threshold is a heuristic on visible context, and only the tail's reasoning survives a summarisation.
- `agents/services/agent_loop.py`, `orchestrator.py` and the other callers: no change. `response.reasoning_content` keeps feeding the `[Thinking]` log when summaries are enabled.
- No new domain exceptions.

### Pages and forms
None. No URL, view, form or template changes.

### Background work and WebSockets
- No new Celery tasks, queues, consumers or controller actions. LLM calls still run inside the existing `execution`/`upload` worker tasks, synchronously.
- No controller protocol change, so `CLIENT_VERSION` and capabilities are untouched.

### Settings / infrastructure
In `auto_tester/settings.py` (python-decouple) and `example.env`:
- `OPENAI_BASE_URL` default becomes `https://api.openai.com/v1/responses`. The name is kept for minimal churn; it holds the full endpoint URL. Update the comment to say it must be a Responses endpoint.
- No reasoning-effort setting (Decisions Q4).
- New `OPENAI_REASONING_SUMMARY: str` with default `auto` (Decisions Q1), validated with `decouple.Choices` (`"" | auto | concise | detailed`); blank = off. The `auto` value adds the model's thinking summary to the agent debug log, but needs a verified OpenAI organisation, otherwise every call returns 400.
- `example.env` gets the new URL and the new key with short comments.
- The developer's `.env` (line 23) and any deployed `.env` must be changed to `/v1/responses` by hand. Call this out in the `docs/changed` note. The implementer must not print or commit `.env`.

### Data flow
1. A caller builds `tuple[ChatMessage, ...]` and calls `send_chat_completion(config, messages, tools)`.
2. `_build_payload` -> `_serialize_input`: leading system -> `instructions`; the rest -> `input` items (assistant turns expand to reasoning + message + function_call items; tool turns -> function_call_output). Tools are flattened.
3. httpx POSTs to `OPENAI_BASE_URL` (`/v1/responses`) with `store:false`.
4. On 2xx, `_parse_response` walks `output[]`. It returns an assistant `ChatMessage` that carries content, tool calls, encrypted reasoning items and phase, plus the mapped `finish_reason` and usage. On >= 400, `_status_error` behaves as today.
5. `agent_loop` appends `response.message` and the `tool` results. On the next iteration step 2 replays the reasoning items in front of their function calls, so the model continues its chain of thought across tool turns.

## Alternatives considered
- **`reasoning_effort: "none"` on Chat Completions**: rejected by the user. The model must reason.
- **`previous_response_id` with `store: true`**: less payload, but it conflicts with our client-side history rewriting (context summarisation, orchestrator rebuilds). It also adds 30-day retention of screenshots and prompts at OpenAI, plus response-id state. Rejected; see the strategy above.
- **Stateless without replaying reasoning**: simplest, but OpenAI explicitly recommends passing reasoning items back with function outputs for quality and cache hits. The extra code is small. Rejected.
- **Raw replay of `response.output` dicts on `ChatMessage`**: the most literal option, but it duplicates the typed tool calls and leaks provider JSON into the domain type. Typed `ReasoningItem` is chosen.
- **Adopting the official `openai` SDK**: it would give `output_text` and typed items, but adds a dependency and rewrites error mapping and timeouts. The current httpx client is small and already handles errors safely. Not worth it for this fix.
- **Renaming `send_chat_completion`** (e.g. `create_response`): clearer, but it touches 12 modules for no behaviour change. Keep the name; a rename can be a separate refactor plan (see Q3).
- **Keeping Chat Completions for tool-less callers**: it would need two protocols and two URLs. One client and one protocol is simpler.

## Risks and mitigations
- **Stale `.env` still pointing at `/v1/chat/completions`**: every call would 400 (`messages` missing / unknown `input`), and every case would be marked Blocked.
  - Mitigation: the changed note and the plan list the manual `.env` edit, and step 6 verifies it on the dev machine.
  - Optional (see Q2): `build_llm_config()` could refuse an endpoint ending in `/chat/completions` with a clear `AIServiceError(REJECTED)` message.
- **Reasoning summary on an unverified org returns 400 on every call**: the default is `auto` (the org is verified); set it blank for an unverified org. The setting comment warns about it.
- **Replay rejected by the API** (for example, "reasoning item provided without its required following item", or item ids not found with `store:false`):
  - Our serializer always emits reasoning immediately before its message or function_call items, and drops orphaned reasoning.
  - Function-call and message items are sent without item `id`s. Only reasoning items carry their `id`, which the schema requires, plus `encrypted_content`.
  - Smoke step 3 exercises a multi-turn tool loop. If the API still rejects replayed reasoning, the documented fallback is to drop reasoning replay (stateless without reasoning). That is a one-line serializer change, recorded in the Changelog.
- **Payload size and cost**: encrypted reasoning adds input tokens each turn. Bounded: only the tail survives summarisation, and the context is 1.05M tokens. We accept this for quality.
- **Long requests**: `medium` reasoning can take minutes. `OPENAI_REQUEST_TIMEOUT=900` already covers it, and the request stays in the Celery worker, not a web request.
- **Strict normalisation**: avoided with `strict: false`.
- **Behaviour drift in image calls**: `input_image` with `detail` omitted is `auto`, the same as before. If element-finding accuracy changes, `detail:"high"`/`"original"` is a follow-up and is not in this plan.
- **Secrets**: no change to how the API key is sent or logged. `_status_error` still hides auth error bodies and never includes the URL.
- **Typing**: `MessageDict` is `dict[str, object]`. Parsing must narrow with `isinstance` (no `Any`, no `cast` without narrowing) to stay mypy-strict clean.

## Implementation steps
Parallelism: **not recommended.** `llm_serializer.py` and `llm_client.py` share the payload contract line by line, and both depend on the new `types.py` fields. The settings, `example.env` and config edits are a few lines. One implementer, in order:

1. Types (files: `agents/types.py`): add `ReasoningItem`; add `ChatMessage.reasoning` and `ChatMessage.phase` with `None` defaults; add `LLMConfig.reasoning_summary`; add `LLMResponse.usage_reasoning_tokens = 0`. Fix `LLMConfig` construction sites (only `build_llm_config`; grep to confirm).
2. Settings (files: `auto_tester/settings.py`, `example.env`, `agents/services/llm_config.py`): change the `OPENAI_BASE_URL` default to `/v1/responses`; add `OPENAI_REASONING_SUMMARY` (default `auto`, blank = omit) with comments; wire them into `build_llm_config()`.
3. Serializer (files: `agents/services/llm_serializer.py`): Responses input/tools serialization and output parsing as specified, with the `finish_reason` mapping and `failed`/`error` handling.
4. Client (files: `agents/services/llm_client.py`): new payload (`input`, `instructions`, `max_output_tokens`, `store`, `include`, `reasoning`, flat tools), updated log lines (status and reasoning tokens) and comments. Keep `send_chat_completion`'s signature and error mapping.
5. Grep for leftovers (files: none expected): `max_completion_tokens`, `choices`, `chat/completions`, `image_url"` nested dicts, `"function": {` in the server code. The `docs/` history files stay as they are.
6. `pre-commit run --all-files` (black, isort, mypy strict for the server and `controller_client/`, which is untouched).

## Verification
No automated tests (project rule). Manual checks with the dev stack (`docker compose up db redis searxng -d`, Daphne, `execution` and `upload` workers), after editing `.env` to `OPENAI_BASE_URL=https://api.openai.com/v1/responses`:
1. `python manage.py check` and `mypy .` are clean.
2. Tool-less text call: in `python manage.py shell -c`, call `send_chat_completion(build_llm_config(), (ChatMessage("system", "Reply OK"), ChatMessage("user", "ping")))`. Expect non-empty `message.content`, `finish_reason == "stop"` and `usage_reasoning_tokens > 0` in the log, which shows reasoning is on.
3. The original failure: run one real test case (Runs -> a draft run with one browser case, Start). In the worker log, every `LLM response` line shows HTTP 200, `finish_reason=tool_calls` on the intermediate turns, `reasoning_tokens > 0`, and no 400 "Function tools with reasoning_effort". From the second iteration on, the request carries reasoning items (temporary debug only, not committed). The case ends Passed or Failed, not Blocked from an AI error.
4. Vision path: a desktop case that uses `find_element` (controller connected, Visual engine ready) gets a sensible element from `controller_element_finder` / `action_verifier`. This shows that `input_image` data URLs work.
5. Context summarisation: temporarily lower the threshold, or use a long case, and confirm a summarised history with a tool-call tail is still accepted (no "reasoning item ... without its required following item").
6. Orchestrator evaluate (multi-turn, no tools): a two-step case reaches a verdict.
7. Error mapping: a bad `OPENAI_API_KEY` in a shell session gives `AIServiceError(AUTH)` with no body. A stale `/chat/completions` URL gives `REJECTED` with a truncated body, and no URL appears in the message.
8. Optional: set `OPENAI_REASONING_SUMMARY=auto` on a verified org and confirm `[Thinking]` lines appear in the run debug log.
9. Run the `reviewer` and `verifier` agents in parallel (workflow step 3), giving the verifier the original request and this plan's path.

## Decisions (answers to the open questions, 2026-10-22)
- Q1 Reasoning summaries: **on**. The org is verified. `OPENAI_REASONING_SUMMARY` defaults to `auto`; blank disables it (for unverified orgs).
- Q2 Stale-URL guard: **none**. The manual `.env` edit is called out in the changed note.
- Q3 Keep the name `send_chat_completion`.
- Q4 Reasoning effort: **no setting**. `reasoning.effort` is never sent; the model default (`medium`) applies. `LLMConfig` gains only `reasoning_summary`, and `OPENAI_REASONING_EFFORT` is dropped from the design above.
- Q5 `store: false`, with encrypted reasoning replayed client-side.

## Changelog
- 2026-10-22: created (Draft).
- 2026-10-22: open questions answered (see Decisions); Status In progress.
- 2026-10-22: review fixes: several message items are joined with a blank line and keep a phase only when they share one; a tool message without `tool_call_id` raises instead of sending an empty `call_id`; `_finish_reason` takes `has_tool_calls`; `OPENAI_REASONING_SUMMARY` validated with `Choices`.
- 2026-10-22: rejected review findings: (a) logging input-item count and raw status: `finish_reason` already carries non-completed statuses, plan text updated instead; (b) dropping function calls from an `incomplete` response: same as Chat Completions behaviour before, and an empty turn would end the agent loop as if finished, which is worse; (c) provider `error.message` in user-facing text: same truncated handling as existing 4xx bodies.
