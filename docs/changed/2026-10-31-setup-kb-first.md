# Machine setup checks the knowledge base first

Date: 2026-10-31
Plan: inline (prompt-only change)

## What changed
- `machine_setup/services/setup_prompt.py` `_build_knowledge_base` (every lead role, only when the KB has ready items): new first rule — once per thread, before the first sub-task that changes the machine, open archives that look like machine material (profiles, setup notes, configs), read the root and topic folder instructions, run one combined `search_knowledge` ('or'), and use those facts over guesses and defaults.
- `_build_setup_rules` METHOD, main chat only: after reading preconditions, check the KB before the first sub-task, read_only checks included.
- `machine_setup/services/setup_task_prompt.py` `_build_knowledge_base_section`: helpers search the KB before guessing a project-specific path, setting, file or procedure and before NEEDS_USER for it, never to authorise a change the safety rules stop.

## Why
The lead searched the KB only before asking the user, so on a first S0 attempt it planned and delegated without reading an uploaded machine profile.

## Behaviour and contract changes
- none (prompt text only)

## Notes for future changes
- Help chat, result chat and the Night auto-fix keep read_only screen checks first (the auto-fix transient-fault path depends on that); only the main chat's METHOD makes the KB pass come before any sub-task.
- The sweep is bounded on purpose: the lead's transcript is replayed every round and turn, so every listing it reads costs tokens for the rest of the thread.
