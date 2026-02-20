---
name: merge-worktree
description: Clean up a worktree, merge its branch into main, and push
---

## Merge worktree logic

The user will provide a branch name (e.g. `feat/live-loging`, `debug/search-performance`).

1. Check the commits on the branch ahead of main: `git log --oneline main..<branch>`.
2. Force-remove the worktree directory (derive name the same way as the `/worktree` skill: replace `/` with `-`, sibling of main repo). Use `git worktree remove --force <path>`.
3. Merge the branch into main: `git merge <branch>`.
4. If there are merge conflicts, resolve them by keeping both sides (both branches typically add new code at the end of files or add new fields). After resolving, `git add` the fixed files and `git commit --no-edit`.
5. Delete the merged branch: `git branch -d <branch>`.
6. Push to remote: `git push`.
7. Confirm to the user what was merged and the final commit hash.
