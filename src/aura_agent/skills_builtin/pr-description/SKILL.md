---
name: pr-description
description: Write a pull-request description from the branch's changes.
license: Apache-2.0
---
1. Compare with the base branch: `git log base..HEAD --oneline` and `git diff base...HEAD --stat`.
2. Sections: Summary (what and why, 2–4 lines), Changes (bullets by area), How it was tested (commands run, results), Risks / follow-ups.
3. Link issues mentioned in commits. Keep it factual — only claim tests that were actually run.
4. Show it; open the PR only if the user asked (it publishes).
