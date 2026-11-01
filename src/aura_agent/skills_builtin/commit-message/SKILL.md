---
name: commit-message
description: Write a clear commit message from the staged changes.
license: Apache-2.0
---
1. Read `git diff --staged` (or `git diff` if nothing is staged).
2. First line: imperative, ≤ 72 characters, what the change does ("Fix login timeout on slow networks").
3. Body: why it was needed and anything a reviewer must know; wrap at 72.
4. Follow the project's convention if `git log --oneline -20` shows one (e.g. "feat:", "fix:").
5. Show the message; commit only if the user asked you to.
