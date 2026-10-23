# Remove the LLM context and output summarizers

Date: 2026-10-23
Plan: docs/plan/refactor-remove-llm-summarizers.md

## What changed
- `agents/services/context_summarizer.py` and `output_summarizer.py`: deleted, with the `summarize_context_if_needed` export in `agents/services/__init__.py`.
- `agents/services/agent_loop.py`:
  - `run_tool_loop` no longer rewrites the transcript; it only appends.
  - Each tool result goes through `_cap_tool_result`: plain head/tail truncation, 50/50, with a marker stating how many characters were omitted.
  - The `[Tool Result]` debug line logs a 2,000-char preview (`_log_preview`).
- `agents/services/llm_client._build_payload`: every Responses request sends `"truncation": "auto"`.
- `agents/types.py`: `ToolResult.verbatim` removed (its two uses in `machine_setup/services/setup_tools.py` too). The `TranscriptCallback` comment now says append-only.

## Why
The summarizers dated from the 8k/32k-token models. Past 20k chars, the context summarizer kept only the system prompt, the first-ever user message and the last 6 messages. The machine setup chat then forgot the user's current request, and `run_turn` saved the lossy transcript for good. Every role now runs on gpt-5.6-sol (1.05M-token window), so summarizing only lost information and added reasoning-model calls.

## Behaviour and contract changes
- The model sees raw tool output up to `AGENT_TOOL_RESULT_MAX_CHARS` (new setting, default 100000) instead of an LLM summary of anything over 2,000 chars.
- Settings `OUTPUT_SUMMARIZE_THRESHOLD`, `OUTPUT_SUMMARIZE_CHUNK_SIZE`, `CONTEXT_SUMMARIZE_THRESHOLD`, `CONTEXT_PRESERVE_LAST_MESSAGES` and `CONTEXT_SUMMARIZE_CHUNK_SIZE` are removed. Leftover lines in `.env` are ignored.
- Past the window, the API drops the oldest input items instead of returning a 400. `instructions` (the system prompt) is never dropped.
- No migration. Deploy: restart the execution and upload workers.

## Notes for future changes
- The `web_search` result summary (`tools_search.py`, `ToolContext.summarizer_config`) is a separate feature and was kept on purpose.
- Setup transcripts now grow without a ceiling, and `run_turn._persist_transcript` rewrites the whole JSON twice per round, so write cost is quadratic within a turn. Moving to append-only message rows is the fix if it shows up. Reset clears it.
- Setup conversations already damaged by a `[Context Summary]` message keep it; use Reset.
- Not yet run against the live model and runner: the long setup chat check and the large-output check in the plan's Verification still need doing.
