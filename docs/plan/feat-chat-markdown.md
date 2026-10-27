# Markdown in the machine setup chat

Status: Done

## Request
The chat should be Markdown: the agent replies in Markdown and the page renders it.

## Plan
- `machine_setup/services/setup_prompt.py` `_REPLY` (shared by the main, help and
  result roles): ask for GitHub-flavored Markdown (lists, bold, inline code for
  paths/commands/ids, fenced blocks, small tables) and forbid HTML and images.
- `templates/machine_setup/chat.html`: load `marked@15.0.12` and
  `dompurify@3.2.6` (pinned jsDelivr, like Alpine) as blocking scripts before the
  page script, so they exist before Alpine starts. Add `renderMarkdown(text)` to
  `machineSetupChat`: `marked.parse` (gfm, breaks) then `DOMPurify.sanitize`
  with images, forms, style, svg and math forbidden; links get
  `target=_blank rel="noopener noreferrer nofollow"`.
- `templates/machine_setup/includes/_transcript.html`: reply and main-agent
  narration rows use `x-html="renderMarkdown(...)"` in a `.chat-md` container.
  User rows, notices, cards and sub-task (helper) rows stay plain `x-text`.
- `static/css/punk-hazard.css`: `.chat-md` styles (paragraph spacing, lists,
  inline code, `pre`, tables, blockquote, headings, links) using tokens.

## Security
Agent text can carry injected page content, so every rendered string goes
through DOMPurify; images are dropped (no tracking/exfiltration on render).

## Changelog
- Review pass 1: DOMPurify default list kept `background` (zero-click fetch) ->
  explicit ALLOWED_TAGS/ALLOWED_ATTR; SRI on both CDN scripts; memoised
  rendering + `x-effect`/`showMarkdown` instead of `x-html` (no re-parse or DOM
  churn on every socket event); live region announces plain text; ntfy reply
  preview strips Markdown (`turn_reply_notification._plain`).
- Review pass 2: preview regexes used `\s` across lines (ReDoS) -> `[ \t]` and
  a bounded prefix; `**`/backticks removed without leaving gaps, `__` kept
  (dunder file names).
