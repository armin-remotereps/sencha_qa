# Markdown in the machine setup chat

Date: 2026-10-28
Plan: docs/plan/feat-chat-markdown.md

## What changed
- `machine_setup/services/setup_prompt.py` `_REPLY` (main, case-help and case-result roles): the agent now replies in GitHub-flavored Markdown (lists, bold, inline code, fenced blocks, small tables; no HTML, no images).
- `templates/machine_setup/chat.html`: loads `marked@15.0.12` and `dompurify@3.2.6` (jsDelivr, SRI-pinned, blocking, before Alpine). `renderMarkdown` (memoised, bounded) sanitizes to an explicit tag/attribute allowlist; `showMarkdown` writes the row only when its source changed; `latestReplyText` announces plain text.
- `templates/machine_setup/includes/_transcript.html`: reply and main-agent narration rows render Markdown (`.chat-md`); narration is now a `<div>`. User rows, notices, cards and sub-task (helper) rows stay plain `x-text`.
- `static/css/punk-hazard.css`: `.chat-md` styles (lists, code, `pre`, tables, quotes, headings, links).
- `machine_setup/services/turn_reply_notification.py`: the ntfy reply preview strips Markdown (`_plain`).

## Why
Agent replies were plain text; Markdown makes paths, commands, lists and tables readable in the chat.

## Behaviour and contract changes
- No URL, payload, socket or model change. The page now needs jsDelivr for marked and DOMPurify (like Alpine).

## Notes for future changes
- Agent text can carry prompt-injected web content: keep DOMPurify's allowlist free of anything that loads a resource (img, `background`, `style`, `src`). Never `x-html` other fields.
- Bumping marked/DOMPurify means recomputing the `integrity` hashes.
- Sub-task helpers still report plain text (`setup_task_prompt.py`); their rows are not rendered as Markdown.
- Verified: pre-commit (black/isort/strict mypy) on changed files, `manage.py check`, a browser smoke test of the renderer with XSS/exfiltration payloads, two reviewer + verifier passes. Not yet seen with the real model in a live chat.
