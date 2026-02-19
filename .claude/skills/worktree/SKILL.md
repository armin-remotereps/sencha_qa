---
name: worktree
description: Create a git worktree with venv and .env ready to go
---

## Worktree setup logic

The user will provide a branch name (e.g. `debug/search-performance`, `feat/new-thing`).

1. Derive the worktree directory name from the branch: replace `/` with `-` and place it as a sibling of the main repo. For example if the main repo is at `/path/to/sencha_qa` and the branch is `debug/search-performance`, the worktree goes to `/path/to/sencha_qa-debug-search-performance`.
2. Create the worktree: `git worktree add <path> <branch>`. If the branch doesn't exist yet, use `-b <branch>` to create it from the current HEAD.
3. Copy `.env` from the main repo into the new worktree.
4. Copy the `venv/` directory from the main repo into the new worktree (do NOT create a fresh venv or run pip install — copying is much faster).
5. Confirm to the user that the worktree is ready with the path.
