---
name: fix-merge-conflict
description: Resolve git merge or rebase conflicts: understand both sides, keep both intents, prove it builds.
license: Apache-2.0
---
1. Run `git status` and list the conflicted files; for each, read the whole file (read_file), not only the markers.
2. For every conflict, find what each side wanted: `git log --oneline -3 -- <file>` on both branches, or the commit messages.
3. Keep both intents where they don't contradict; when they do, stop and ask the user which wins (clarify) — never guess.
4. Remove every marker (<<<<<<<, =======, >>>>>>>) and search the project for leftovers (search_files).
5. Run the build or tests (run_checks). Don't commit or continue the rebase unless the user asked; say what is left to do.
6. A rebase gone wrong: `git reflog` shows where you were — tell the user the command to go back, don't run destructive resets unasked.
