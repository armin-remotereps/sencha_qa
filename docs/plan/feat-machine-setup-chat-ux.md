# feat: Machine setup chat that talks (agent narration, batch case reads, activity blocks)

Status: Done
Created: 2026-10-23
Request: "It's not chatty, make the UI/UX better." On a real turn the user asked "Based on tests pre-condition, plan what needs to be presented on the machine". The page then showed about 35 collapsed tool rows, all marked "Done": `list_test_case_preconditions` ×2, `search_test_cases`, about 30 `get_test_case`, then `list_test_case_preconditions` ×2 again. There was no assistant text, then the blocker line "The setup agent is still working…", then the composer. It reads like a log, not a conversation. Two things should change. The agent should talk: say what it is about to do, answer planning questions with a plan and a confirmation question, and read cases in batches instead of one at a time. The transcript should read like a chat: tool calls folded into summarised activity blocks with friendly labels, a live "working" indicator, replies that stand out, and a friendlier status above the composer.

## Context

- **Shipped feature**: `docs/changed/2026-10-23-machine-setup-s0.md`, `docs/plan/feat-machine-setup-s0.md` (Done).
- **Why the agent is silent**:
  - `machine_setup/services/setup_prompt.py::_build_setup_rules` asks for text only at the end ("REPLY: When you finish, reply…"). Nothing asks the model to say anything before it calls tools.
  - It does ask for "a short plan" before "broad or risky changes", but a read-only planning request is neither, so the model just keeps reading.
  - GPT-5-family models write short user-visible "tool preambles" only when the prompt asks for them, and the prompt sets how often and in what style (OpenAI GPT-5 prompting guide). The Responses API marks those messages `phase: "commentary"`, and the final message `final_answer`.
  - `agents/services/llm_serializer.py::_parse_response` already keeps the phase and joins message text with function calls in one `ChatMessage`.
  - `run_turn._TurnRunner._record_assistant` already writes an `assistant` row for any text on a message that also has tool calls, and writes it before the tool rows. So a prompt change alone makes narration show up in the chat. No change to the server's display path is needed.
- **Why it reads cases one by one**:
  - The only full-case tool is `get_test_case(case_id)` (`setup_tools.py`, `case_catalog.get_case`, one query, each field capped at `CASE_FIELD_LIMIT = 4000`).
  - The METHOD rule says "use search_test_cases and get_test_case for details", and nothing says the precondition listing is usually enough.
  - `list_case_preconditions` shows only 5 case ids per group, and a case's steps are not needed to plan S0. The model reads full cases anyway because the prompt never says not to.
- **Array parameters are not supported today**:
  - `agents/types.ToolParameter` has `name`, `type`, `description`, `required` and `enum`.
  - `llm_serializer._serialize_tools` builds `{"type", "description", "enum"?}` typed `dict[str, str | list[str]]`, with `"strict": False`.
  - A JSON-schema `array` needs `items`. Both files are needed for the smallest change, and there are no other consumers (grep: only `_serialize_tools` reads `tool.parameters`).
- **Display data the client already has** (`machine_setup/payloads.py`, no change needed):
  - `SetupMessagePayload`: `kind`, `turn_id`, `tool_name`, `tool_status`, `tool_arguments_text` (pretty JSON, cut to 4000 chars), `text`, `screenshots`, `link_url`.
  - `SetupTurnPayload`: `status`, `iterations` (one per LLM call, bumped and broadcast in `_record_assistant`), `max_iterations`, `stop_requested`.
  - Grouping, labels, the live action and "step N of M" can all be computed on the client from these.
- **Frontend today**:
  - `templates/machine_setup/chat.html`: inline `Alpine.data('machineSetupChat')`. It holds `messages`, `upsertMessage`, `turn`, `blockers`/`blockerText`, `toolBadgeClass`/`toolBadgeLabel` and the stick-to-bottom scroll.
  - `includes/_transcript.html`: one `<details class="chat-tool">` per tool row, with the raw `tool_name`, a status badge and args/result `<pre>`.
  - `includes/_composer.html`: the blocker line `.chat-blocker` (warning colour, shows `SetupTurnActiveError.default_message` while live), the textarea, Send, and Stop while live. The "Step N of M" text sits under the composer.
  - CSS: `.chat*` block in `static/css/punk-hazard.css` (around line 2295). Tokens: `--surface`, `--surface-raised`, `--cyan-soft`, `--info-soft`, `--text-muted`, `--mono`. There is an existing `pulse` / `live-pulse` animation and a global `prefers-reduced-motion` override.
  - `aria-live="polite"` sits on the whole `.chat-log`, so every tool row update is announced.
- **Reasoning summaries**:
  - `OPENAI_REASONING_SUMMARY` defaults to `auto` (`settings.py`), and `llm_client._build_payload` requests it.
  - Summaries come back on `ChatMessage.reasoning[].summary` (also joined as `LLMResponse.reasoning_content`, which goes only to the debug log). They are already persisted inside `llm_transcript` (`agents/transcript.py`).
  - Responses are not streamed. A summary arrives in the same response as that response's text and tool calls, never while the model is still thinking.
- **Versions** (`requirements.txt`): Django ~=5.2.11, channels ~=4.3.2, celery ~=5.6.2. Alpine 3.14 (pinned CDN). Model `gpt-5.6-sol` on the Responses API. Latest `machine_setup` migration: `0001_initial`.
- Could not inspect the reported turn: the local dev database has no `machine_setup` tables (it was not migrated), so the turn came from another environment. The row shape above comes from reading `run_turn.py`.

## Goals
- The agent writes one short sentence before each batch of tool calls, saying what it is about to do and why.
- Planning requests ("plan", "what is needed", "check") get a structured plan that ends with a confirmation question. The agent changes nothing on the machine before the user confirms.
- Case reads go through the precondition listing first, then batches (`get_test_cases`, at most 20 ids), never one case per call.
- Transcript:
  - consecutive tool rows of one turn fold into one **activity block**, with a human summary, the live current action, a count and errors;
  - tools have friendly labels with their key argument;
  - agent replies are the visual hero;
  - a typing / working indicator with "step N of M" shows while the agent works;
  - the status line above the composer is friendlier, and Stop is in place.
- No payload, WebSocket or model change for the UI work. Existing conversations render with the new UI unchanged.

## Non-goals
- Streaming tokens or reasoning while the model thinks. The transport is not streamed.
- Markdown rendering. Replies stay plain text with `pre-wrap` and `x-text`. The prompt allows simple `-` / `1.` lists, which read fine as plain text.
- Building tool labels on the server, or adding new payload fields.
- Changes to the tester agent's prompt, tools or behaviour. The `ToolParameter` change leaves every existing tool schema byte-for-byte identical.
- Removing `get_test_case`. It stays, for replayed transcripts that call it and for single reads.
- Setting `parallel_tool_calls`. The API default is left alone; the batch tool fixes the case-read fan-out instead.
- Showing reasoning summaries (recommended against for now; see Design, "Reasoning summaries", and Q1).

## Design

### Data model
None. No migration. This holds only if Q1 stays "no". A new `SetupMessageKind.THINKING` would change the field's choices. That needs no database change on Postgres, but `makemigrations` would still emit an `AlterField` migration, `machine_setup/0002`, which runs no SQL.

### Agent side

**1. Array parameters** (`agents/types.py`, `agents/services/llm_serializer.py`). This is the smallest change.
- `ToolParameter` gains two optional fields with `None` defaults:
  - `items_type: str | None = None`, the JSON-schema type of the array elements;
  - `max_items: int | None = None`.
- `_serialize_tools`:
  - widen `parameter_schema` to `dict[str, object]`;
  - when `items_type` is set, emit `"items": {"type": items_type}`;
  - when `max_items` is set, emit `"maxItems": max_items`.
- Existing tools set neither field, so their schemas are unchanged. Strict mode stays off, so `maxItems` is only a hint to the model; the handler enforces the cap.

**2. Batch read** (`machine_setup/services/case_catalog.py`).
- `get_cases(project_id: int, case_ids: Sequence[int]) -> CasesBatch`. `CasesBatch` is a frozen dataclass:
  - `text: str`;
  - `found: tuple[int, ...]`;
  - `missing: tuple[int, ...]`, ids not in this project;
  - `deferred: tuple[int, ...]`, found but left out by the character budget.
- One query: `TestCase.objects.filter(project_id=..., pk__in=ids).values(<same columns as get_case>)`. Output keeps the requested order.
- Each case is rendered by a private `_render_case(row) -> str`, extracted from `get_case`, which now uses it too. Same per-field cap.
- Budget: `CASES_BATCH_CHAR_BUDGET = 24000`, about 6k tokens.
  - The first case is always included. One case is at most about 12.2k chars, so a call always makes progress.
  - After it, cases are added while they fit.
- The text ends with lines telling the model what to do next:
  - "Not found in this project: …";
  - "Not included (size limit), call get_test_cases again with: …".
- `MAX_CASES_PER_BATCH = 20` lives here too, next to the other catalog limits.

**3. Tool** (`machine_setup/services/setup_tools.py`).
- Add the `get_test_cases` definition to `_project_data_tool_definitions`. It has one parameter, `case_ids`: `type="array"`, `items_type="integer"`, `max_items=MAX_CASES_PER_BATCH`, required.
  - Description: "Get up to 20 test cases in full in one call… prefer this over get_test_case for more than one case; read as data…".
- Its handler is in `project_data_handlers`, so it gets the same `verbatim=True` and no runner pre-check.
- New private `_required_int_list(arguments, name, max_items)`. It raises `_InvalidArgumentError` when:
  - the value is not a list;
  - the list is empty or longer than 20;
  - an element is not an int, or is a bool.
  - It also removes duplicates and keeps the order.
- If every id is missing, it returns a tool error that names `search_test_cases`. This mirrors `_handle_get_case`.
- The `get_test_case` description gains "For several cases use get_test_cases."

**4. Prompt** (`machine_setup/services/setup_prompt.py`). Wording is the implementer's; these are the rules:
- `_build_project_data_tools`: list `get_test_cases(case_ids)`, at most 20 per call.
- New **COMMUNICATION** block in `_build_setup_rules`, before METHOD:
  - before each batch of tool calls, write one short sentence for the user (at most about 20 words): what you are about to do and why. Write it once per batch, not once per call, and do not repeat yourself;
  - after an important finding (something missing, a conflict, a version mismatch), say so in one line before carrying on;
  - these progress notes are not the final reply.
- **METHOD** changes:
  - read every page of `list_test_case_preconditions` first. It already groups the cases by precondition;
  - read full cases only when a precondition is unclear, and then with `get_test_cases` in batches. Never read cases one by one, and do not read test steps just to plan S0;
  - no change to the rest of METHOD.
- New **PLANNING REQUESTS** block:
  - when the user asks to plan, list, check or say what is needed, do not change the machine;
  - read-only inspection is allowed, such as checking whether a tool is installed or which version it has;
  - answer with a structured plan: numbered items grouped as Software and versions, Directories and files, Accounts, logins and configuration, and Unclear or conflicting (with case ids), each saying what is already in place;
  - end with a question asking the user to confirm, or to adjust the plan, before you start.
- **REPLY**: keep "plain text, no Markdown tables or HTML". Allow simple `-` and `1.` lists.

Prompt length grows by about 250 tokens. The tester prompt is unaffected.

### Reasoning summaries (evaluated; recommended **against** for now)
- **What it would take**:
  - a new `SetupMessageKind.THINKING`, which needs migration `0002` (`AlterField`, no SQL);
  - `_record_assistant` writes a `thinking` row (capped at about 2000 chars) from `message.reasoning[].summary` before the assistant row;
  - the client shows it muted inside the activity block. The `**Heading**` the model's summaries usually start with would serve as the live status line.
- **Cost**:
  - no extra tokens, since summaries are already requested;
  - one more row and one more broadcast per LLM call, up to 200 per turn. These rows use up the 300-row page window (`SETUP_PAGE_MESSAGE_LIMIT`) and push real replies out of view sooner.
- **Why against**:
  - Responses are not streamed. A summary arrives in the same response as the preamble sentence and the tool calls. It cannot fill the silent wait while the model thinks, which is the part that feels dead. The working indicator covers that wait.
  - After the fact, a summary mostly repeats the preamble, so it adds noise to a transcript we are trying to calm down.
  - It is model-internal text. With `OPENAI_REASONING_SUMMARY` blank (unverified org) the feature silently disappears, which makes it inconsistent across deployments.
- **Revisit** if streaming is added. Then a live "Thinking: <heading>" line would be valuable without storing anything.
- Q1 asks the user, because this is a product choice.

### Pages and forms
No new or changed URLs, forms, views or context keys.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<id>/machine-setup/` (`machine_setup:chat`) | GET | `@project_membership_required` (unchanged) | `SetupMessageForm` (unchanged) | `machine_setup/chat.html`, same context; only the template, Alpine component and CSS change | unchanged |

**Client model** (all in `chat.html`'s `machineSetupChat`):
- **Parse arguments once.** When a row arrives (`upsertMessage` and `current_state`), add a client-only `args` property: `JSON.parse(tool_arguments_text)` inside `try`, or `{}`. Text cut at 4000 chars fails to parse and falls back to `{}`; the label then shows no detail. Rows are never parsed inside a render getter.
- **`TOOL_LABELS`**: one constant object, the only place tool wording lives. Each entry has:
  - `summary(count, rows)`, the phrase for the collapsed block, e.g. "Read 30 test cases" or "Listed preconditions ×2";
  - `live(args)`, the running phrase ending in "…";
  - `detail(args)`, the key argument, cut to 80 chars on one line.

  Tools without an entry fall back to the raw tool name. Mapping:

  | Tool(s) | Summary bucket | Detail (key argument) |
  |---|---|---|
  | `get_test_case`, `get_test_cases` | "Read N test cases" (shared bucket; N counts ids) | `#12` / `#12, #13 +18` |
  | `list_test_case_preconditions` | "Listed preconditions ×N" | none |
  | `search_test_cases` | "Searched test cases ×N" | the query in quotes |
  | `get_application_context` / `update_application_context` | "Read the application context" / "Updated the application context" | — / `change_summary` |
  | `execute_command` | "Ran N commands" | `command` (first line) |
  | `wait_for_command` / `send_command_input` | "Waited for a command" / "Sent input to a command" | — (input is **not** shown) |
  | `take_screenshot` / `browser_take_screenshot` | "Looked at the screen" / "Looked at the page" ×N | `question` |
  | `click`, `double_click`, `hover`, `browser_click`, `browser_double_click`, `browser_hover` | "Clicked …" / "Hovered …" (N) | `description` |
  | `drag` | "Dragged ×N" | `start_description → end_description` |
  | `type_text` / `browser_type` | "Typed text ×N" | none / `description` only (the typed text is **never** shown; it may be a password) |
  | `key_press` | "Pressed keys ×N" | `keys` |
  | `launch_app`, `close_app`, `close_tab`, `check_app_installed` | "Opened / Closed / Closed a tab in / Checked N apps" | `app_name` |
  | `browser_navigate` | "Opened N pages" | `url` |
  | `browser_get_page_content` / `browser_get_url` | "Read the page" / "Checked the URL" | — |
  | `browser_download` / `browser_list_downloads` | "Downloaded N files" / "Listed downloads" | `url` / — |
  | `web_search` | "Searched the web ×N" | `query` |
  | `wait` | "Waited" | `seconds` + "s" |

- **`blocks` getter**: turns `messages` into a list. Each entry is `{kind: 'row', message}` or `{kind: 'activity', key: 'act-' + firstRowId, turnId, rows}`.
  - An activity block is a run of consecutive `tool` rows with the same `turn_id`. Any other row ends it.
  - Each block also gets derived fields:
    - `running`: the row whose status is running;
    - `failedCount`;
    - `summaryText`: the first 3 buckets in order of first appearance, joined with " · ", plus "+N more";
    - `screenshotCount`.
  - The key is the id of the block's first row, so it stays stable while the block grows. Alpine's keyed `x-for` reuses the element, and an expanded `<details>` stays open.
  - Cost: O(rows) per change, at most 300 rows. That is negligible.
- **Narration or reply**: an `assistant` row is **narration** when a later row of the same turn is a tool row; otherwise it is a **reply**. This comes from `blocks` and needs no server phase field.
  - While a turn runs, the assistant row is broadcast just before its tool rows, so it can show as a reply for a moment before it becomes narration. That is acceptable. A 150 ms CSS transition softens it.
- **`working` getter**: `turnLive` and the last block is not a reply. It returns:
  - the text: "Waiting for a worker…" when queued; "Stopping after the current action…" when `stop_requested`; otherwise the live action of the last block's running row ("Reading test case #12…"), or "Thinking…" when nothing is running;
  - `stepText`: "Step N of M".
- **Blocker text**: the client still computes `blockers` (and so `canSend`) as today. `TURN_ACTIVE` is left out of the *displayed* text while the working indicator shows. The other blockers (visual engine, run active, finishing the previous turn) stay, each with a short "why" and no warning colour unless the machine is not ready.

**Transcript rendering** (`includes/_transcript.html`), every dynamic value through `x-text` / `:href` only:
- **User**: right-aligned bubble (`.chat-msg-user`, unchanged look).
- **Reply** (the hero): `.chat-reply`, left-aligned and wider (max 820px), with a small agent mark, "Setup agent" and the time, slightly larger body text, and `--surface` with a left accent border in `--brand-cyan`.
- **Narration**: `.chat-narration`, one muted line with the agent mark and no bubble, directly above its activity block.
- **Activity block**: `<details class="chat-activity">` with these parts.
  - The summary line shows, left to right:
    - a status dot (`.chat-activity-dot`: running pulses, done is green, has errors is red);
    - `summaryText`;
    - the live line `.chat-activity-live` (only while running);
    - the step count ("12 steps") and a `badge-failed` "2 failed" when there are errors;
    - "3 screenshots" when there are any.
  - The body is a compact list `.chat-activity-list` of the existing per-row `<details class="chat-tool">`. Each row's summary shows the friendly label and detail (`.chat-tool-detail`, mono, ellipsis), the status badge, and the raw tool name as muted mono secondary text. The row body (arguments, result, screenshots, lightbox) is unchanged.
- **Notice**: `.chat-notice` becomes a centred, divider-style pill. It keeps its text and link. The context-update notice (`link_url` non-empty) gets an `--info-soft` tint.
- **Working indicator**: `.chat-working`, after the last block, `role="status"`. It shows the agent mark, three animated dots (`.chat-typing-dots`, still under `prefers-reduced-motion`), the `working` text and `stepText`.
- **Accessibility**: move `aria-live` off `.chat-log`, so tool row updates stop being announced. Replies and the working status are announced through the `role="status"` indicator and a visually hidden `aria-live="polite"` span that holds the text of the latest reply.

**Composer** (`includes/_composer.html`):
- The textarea stays **editable** while a turn runs, so the user can draft the next message. The placeholder changes to "The agent is working. You can send when it finishes."
- Send is replaced by Stop, as today. The "Step N of M" line moves into the working indicator.
- The status line above the textarea (`.chat-status`, which replaces `.chat-blocker`) shows only the non-turn blockers.

**Mobile (<768px)**:
- replies and narration take the full width;
- the activity summary wraps under the dot;
- the live line and details are cut with an ellipsis on one line;
- the working indicator takes the full width;
- the composer layout is unchanged.

### Background work and WebSockets
No change. The events stay `current_state`, `message`, `screenshot`, `turn`, `reset` and `agent_status`, with the same payloads. Celery, the consumer and controller actions are unchanged. `CLIENT_VERSION` and the capabilities are untouched, since nothing new is sent to the Test Runner.

### Settings / infrastructure
None. No env vars and no deploy steps beyond restarting the `execution` workers (new prompt and tool) and web (templates and static). Static files are not hashed in dev, so a hard reload may be needed after deploy.

### Data flow
1. The user sends "plan what the machine needs". The flow is unchanged: PRG, then the turn is queued and broadcast. The client shows the user bubble and the working indicator ("Waiting for a worker…").
2. The worker runs the turn. The model's first response carries a preamble ("I'll read the grouped preconditions first.") and the `list_test_case_preconditions` calls. `_record_assistant` writes and broadcasts the assistant row, then the tool rows. The client shows a narration line and an activity block whose live line reads "Listing preconditions…". The step count comes from the `turn` event.
3. The next response says "A few preconditions are unclear; reading those 8 cases in one go." and calls `get_test_cases([...])`. That gives one tool row, labelled "Read 8 test cases", where before there were 8 or 30 rows.
4. The final response has no tool calls. It is the structured plan ending with "Shall I go ahead?". It renders as the reply hero, and the indicator disappears once the `turn` event says completed.

## Alternatives considered
- **Extend `get_test_case` with an optional `case_ids`.** Rejected: one tool with two input shapes and a required-ness that depends on which is used. The model handles one clear tool per job better, and the old tool must stay for replayed transcripts anyway.
- **A comma-separated string `case_ids`, to avoid changing `ToolParameter`.** Rejected: the schema would lie, and parsing becomes guesswork. The array support is two optional fields and three lines in the serializer.
- **Build labels and summaries on the server (new payload fields).** Rejected for now: it changes the payload contract for a purely presentational concern, and the request asks for one JS mapping. The cost: the client parses `tool_arguments_text`, which is cut at 4000 chars, so very long arguments lose their detail text. That is acceptable, since the long ones (context text, heredocs) are poor labels anyway.
- **Store a `phase` on `SetupMessage` to tell narration from replies.** Rejected: the order rule (an assistant row followed by tool rows in the same turn is narration) gives the same answer with no migration.
- **Disable the textarea while a turn runs.** Rejected: drafting the follow-up during a long turn is useful, and sending is already blocked on the client and the server.
- **Reasoning summaries as "Thinking" rows.** Deferred; see the evaluation above and Q1.
- **No new abstraction**: no shared JS module and no new Alpine component file. The labels and grouping are specific to this page and stay in `chat.html`'s `extra_scripts`, like the existing component. Move them out only if a second page needs them.

## Risks and mitigations
- **Prompt adherence**: the model may still skip preambles or plan-first answers. These are prompt rules only, so verify them on the real model (Verification 1–3). The activity summary keeps the page readable even when the model stays silent.
- **Planning requests stop doing work**: "plan" now ends with a question instead of making changes. That is the intended behaviour, and it matches the existing "short plan and wait" rule. Imperative requests ("prepare the machine") are unaffected.
- **Secrets in labels**: the collapsed summaries make arguments more visible.
  - `type_text`, `browser_type` and `send_command_input` never show their text in labels.
  - `execute_command` shows the first line of the command, cut to 80 chars. A command with an inline password is already visible in the expanded arguments and the transcript today. This is the residual, as noted in the S0 change note ("Secrets in chat").
- **XSS**: every label, detail and summary is built from model or tool text. All of it goes through `x-text`, `:title` and `:href` (only `link_url`, which is server-built). No `x-html`, and no string-built HTML in `TOOL_LABELS`, which returns plain strings only. The reviewer should check this.
- **Batch output size**: one verbatim result of up to 24k chars is about 6k tokens in the transcript. `summarize_context_if_needed` bounds the growth as before. 24k is under the 30 separate `get_test_case` results it replaces.
- **Tester unaffected**: the `ToolParameter` fields default to `None` and the serializer emits nothing new for them. Check that `_serialize_tools(get_all_tool_definitions())` is identical before and after (Verification 6).
- **Render cost**: `blocks` rebuilds on every row change, at most 300 rows. This is fine. Arguments are parsed once per row update, not per render.
- **Page window**: unchanged at 300 rows. Batch reads reduce how many rows a turn produces.

## Implementation steps
Step 0 is a small foundation. Parts A, B and C then run in parallel; each owns its files exclusively. Integration is done by the orchestrator.

0. **Foundation: array parameters** (files: `agents/types.py`, `agents/services/llm_serializer.py`).
   - `ToolParameter.items_type: str | None = None` and `max_items: int | None = None`.
   - `_serialize_tools` emits `items` and `maxItems` when they are set.
   - Run mypy.
1. **Part A: agent behaviour** (files: `machine_setup/services/case_catalog.py`, `machine_setup/services/setup_tools.py`, `machine_setup/services/setup_prompt.py`).
   - In `case_catalog`: `MAX_CASES_PER_BATCH`, `CASES_BATCH_CHAR_BUDGET`, the `CasesBatch` dataclass, `get_cases(project_id: int, case_ids: Sequence[int]) -> CasesBatch`, and the `_render_case` extraction (which `get_case` then uses).
   - In `setup_tools`: the `get_test_cases` definition and handler, `_required_int_list`, and the `get_test_case` description tweak.
   - In `setup_prompt`: the COMMUNICATION and PLANNING REQUESTS blocks, and the METHOD, REPLY and tool-list edits.
   - It exposes the tool name `get_test_cases` with argument `case_ids: list[int]`. Part B's label map depends on that name.
2. **Part B: transcript UI logic and markup** (files: `templates/machine_setup/chat.html`, `templates/machine_setup/includes/_transcript.html`, `templates/machine_setup/includes/_composer.html`).
   - In the component: `TOOL_LABELS`, parsing `args` on ingest, and the `blocks`, `working`, displayed-blocker and latest-reply getters.
   - Markup for replies, narration, activity blocks, notices, the working indicator and the composer status.
   - The `aria-live` move.
   - It uses only the CSS class names listed under "Class contract" below.
3. **Part C: styles** (file: `static/css/punk-hazard.css`, the machine setup chat section only).
   - Implement the class contract with tokens only, plus the mobile rules and the reduced-motion handling for `.chat-typing-dots` and `.chat-activity-dot`.
   - Remove `.chat-blocker` once Part B no longer uses it.

   **Class contract** (Parts B and C):
   - kept: `.chat`, `.chat-log`, `.chat-row`, `.chat-meta`, `.chat-msg`, `.chat-msg-user`, `.chat-tool`, `.chat-tool-body`, `.chat-tool-label`, `.chat-pre`, `.chat-shots`, `.chat-notice`, `.chat-composer*`;
   - new: `.chat-agent-mark`, `.chat-reply`, `.chat-narration`, `.chat-activity` (with `[open]`), `.chat-activity-summary`, `.chat-activity-dot` (modifiers `.is-running`, `.is-done`, `.is-failed`), `.chat-activity-text`, `.chat-activity-live`, `.chat-activity-meta`, `.chat-activity-list`, `.chat-tool-title`, `.chat-tool-detail`, `.chat-tool-raw`, `.chat-notice-info`, `.chat-working`, `.chat-typing-dots` (three child `<span>`s), `.chat-status`;
   - `.chat-msg-assistant` is replaced by `.chat-reply`.
4. **Integration** (orchestrator):
   - check that the tool name and argument shape match between Part A and `TOOL_LABELS`;
   - check that every class used in the templates exists in CSS and that no `.chat-*` class is orphaned;
   - run `pre-commit run --all-files`, `python manage.py check` and `python manage.py makemigrations --check` (expect "No changes detected").

## Verification
- `pre-commit run --all-files` (black, isort, mypy strict); `manage.py check`; `makemigrations --check` (no migration).
- Manual, with a live runner and the real model (Daphne with the size flags, `execution` worker):
  1. Send "Based on the tests' preconditions, plan what needs to be present on the machine". Expect:
     - one narration line before each batch;
     - `list_test_case_preconditions` paged;
     - at most a few `get_test_cases` calls and no run of `get_test_case`;
     - no machine-changing commands;
     - a reply with grouped, numbered items and a closing confirmation question.
  2. Reply "go ahead". The agent starts changing the machine, still narrating each batch. Activity blocks show labels such as "Ran 3 commands", with the command as detail in the expanded rows.
  3. Send "read cases 1–40 in full". The agent makes two or three `get_test_cases` calls. The result lists "Not included (size limit)" ids when over budget, and the agent follows up with them. Unknown ids are reported. Passing 21 ids, an empty list or `[true]` returns a tool error (forced via a scripted LLM or a shell call to the handler).
  4. While a turn runs:
     - the working indicator shows "Waiting for a worker…", then the live action ("Reading test case #12…"), then "Thinking…" between calls, with the step count advancing;
     - the textarea stays editable, and Stop replaces Send;
     - after Stop, the indicator reads "Stopping after the current action…".
  5. An expanded activity block stays expanded while new rows join it. The block key is stable.
  6. Tester unchanged: in `manage.py shell`, compare `json.dumps(_serialize_tools(get_all_tool_definitions()))` with the output on `main`; they must be identical. Run one tester case end to end.
  7. A failing tool (for example a command that exits non-zero, or a missing browser) gives the block a red dot and an "N failed" badge, and the row shows Error.
  8. Reload mid-turn: `current_state` rebuilds the same blocks, narration and indicator.
  9. An old conversation from before this change renders as blocks, and its raw `get_test_case` rows get labels.
  10. XSS:
      - make the agent narrate, and run `echo '<img src=x onerror=alert(1)>'` with a URL argument of `javascript:alert(1)`;
      - everything renders as text, and the URL shows only as text in the detail (it is never bound to `href`).
  11. Secrets: `type_text` with a password shows no text in its label or the summary.
  12. Mobile width (375px): replies, activity summary wrapping, the ellipsis on the live line, the indicator and the composer.
  13. Screen reader (VoiceOver): tool updates are not announced; replies and the working status are.
  14. Reduced motion: the dots and the activity dot do not animate.
- Run the `reviewer` and `verifier` agents in parallel on the diff (verifier with this plan and the original request "It's not chatty, make the UI/UX better").

## Open questions
- **Q1 Show the model's reasoning summaries ("Thinking" lines)?** They would be stored as a new `thinking` row kind, which needs a migration that runs no SQL, and shown muted inside activity blocks. Because responses are not streamed, a summary appears at the same moment as the preamble and tool calls. It does not fill the silent wait while the model thinks, and it mostly repeats the preamble. **Assumption used in this draft: no.** This plan does not include it. Revisit if streaming is added.

## Changelog
- 2026-10-23: created (Draft).
- 2026-10-23: review pass 1: tool label lookups accept only own entries; the case renderer is typed with no Any; dead chat CSS removed.
- 2026-10-23: final review and verify clean (a stale CSS comment removed). Status set to Done.
