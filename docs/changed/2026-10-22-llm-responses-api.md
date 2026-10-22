# Move the LLM client to the Responses API

Date: 2026-10-22
Plan: docs/plan/fix-llm-responses-api.md

## What changed
- `agents/services/llm_serializer.py`: speaks the Responses API. The leading system message becomes `instructions`; images are `input_image` data URLs; tool calls and results are `function_call` / `function_call_output` keyed by `call_id`; tools are flat with `strict: false`. Parsing walks `output[]` and maps `status` / `incomplete_details` to the old `finish_reason` values (`stop`, `tool_calls`, `length`, `content_filter`).
- `agents/services/llm_client.py`: `send_chat_completion` (name and signature unchanged) posts `input`, `max_output_tokens`, `store: false`, `include: ["reasoning.encrypted_content"]` and `reasoning.summary`; logs reasoning tokens.
- `agents/types.py`: new `ReasoningItem`; `ChatMessage.reasoning` / `ChatMessage.phase`; `LLMConfig.reasoning_summary`; `LLMResponse.usage_reasoning_tokens`.
- `auto_tester/settings.py`, `example.env`, `agents/services/llm_config.py`: `OPENAI_BASE_URL` defaults to `/v1/responses`; new `OPENAI_REASONING_SUMMARY`.

## Why
gpt-5.6-sol reasons by default, and `/v1/chat/completions` rejects function tools while it reasons, so every agent call failed with HTTP 400 and cases went Blocked. Turning reasoning off (`reasoning_effort: none`) was rejected.

## Behaviour and contract changes
- **Manual step:** every `.env` must set `OPENAI_BASE_URL=https://api.openai.com/v1/responses`. A stale `/chat/completions` URL makes every call 400 (no guard, by decision).
- New setting `OPENAI_REASONING_SUMMARY` (`auto` default, `concise`, `detailed`, blank = off). Summaries need a verified OpenAI org, otherwise every call 400s. They appear as `[Thinking]` in the agent debug log.
- Reasoning effort is never sent; the model default (`medium`) applies.
- `store: false`: OpenAI keeps no copies of prompts or screenshots, and calls do not appear in its dashboard logs.

## Notes for future changes
- Reasoning is replayed statelessly: the encrypted items ride on the assistant `ChatMessage` and are re-sent before its message or function calls. Code that copies an assistant turn must keep `reasoning` and `phase`, or the model loses its chain of thought. A reasoning item with nothing after it is dropped, because the API rejects it.
- Several output message items are joined with a blank line, and keep `phase` only when they all share it.
- Function calls from an `incomplete` response are still executed, as with Chat Completions; dropping them would end the agent loop as if the task were complete.
- Verified live: a multi-turn tool loop with reasoning replay, reasoning summaries, and image input.
