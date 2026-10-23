# Chattier machine setup chat: narration, activity blocks, batch case reads

Date: 2026-10-23
Plan: docs/plan/feat-machine-setup-chat-ux.md

## What changed
- `agents/types.py`, `agents/services/llm_serializer.py`: `ToolParameter.items_type` / `max_items` emit `items` / `maxItems` for array parameters. The tester's tool schemas are byte-for-byte unchanged.
- `machine_setup/services/case_catalog.py`: `get_cases` returns up to 20 cases in one query, in the order asked, within a 24k-char budget, and lists missing and deferred ids. `_render_case` is shared with `get_case`.
- `machine_setup/services/setup_tools.py`: new `get_test_cases(case_ids)` tool and `_required_int_list`.
- `machine_setup/services/setup_prompt.py`, new rules:
  - COMMUNICATION: one sentence before each batch of tool calls;
  - METHOD: read the precondition listing first, then batch reads, never one case at a time;
  - PLANNING REQUESTS: read-only; answer with a numbered plan in four groups and a confirmation question.
- `templates/machine_setup/chat.html`, `includes/_transcript.html`, `_activity.html` (new), `_composer.html`:
  - `TOOL_LABELS` gives friendly per-tool summaries, live phrases and key arguments;
  - consecutive tool rows fold into collapsible activity blocks (summary, live action, step and failure counts);
  - agent replies are the main element, narration is a muted line, notices are pills;
  - a working indicator shows the live action and "Step N of M";
  - the composer stays editable during a turn, and Stop replaces Send.
- `static/css/punk-hazard.css`: the chat section is restyled (`.chat-reply`, `.chat-narration`, `.chat-activity*`, `.chat-working`, `.chat-status`, …). `.chat-msg-assistant`, `.chat-blocker` and `.chat-tool-name` are removed.

## Why
A real turn rendered as about 35 "Done" tool rows (one `get_test_case` per case) with no agent text. It read like a log, not a conversation.

## Behaviour and contract changes
- New agent tool `get_test_cases(case_ids: list[int], ≤ 20)`.
- Plan requests no longer change the machine.
- No model, migration, payload or WebSocket change. Deploy: restart the execution worker (prompt and tools) and the web (templates and static).

## Notes for future changes
- Narration and reply are told apart on the client: an assistant row followed by a tool row of the same turn is narration. A new message kind would be needed to mark it on the server.
- Reasoning summaries ("Thinking" lines) were deliberately left out: responses aren't streamed, so they wouldn't fill the wait.
- Label text never includes the text typed by `type_text`, `browser_type` or `send_command_input`. Lookups use `Object.hasOwn`, so a model-chosen tool name can't hit `Object.prototype`.
- Verified with:
  - pre-commit, the throwaway-DB smoke script and a node run of the block builder;
  - the real page rendered from a sample conversation and checked in a browser at desktop and mobile widths.
  Not yet seen with a live runner and the real model, so whether it narrates well in practice is still to confirm.
