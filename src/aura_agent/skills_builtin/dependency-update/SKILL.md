---
name: dependency-update
description: Update a project's dependencies safely.
license: Apache-2.0
---
1. List what is outdated with the project's own tool (`pip list --outdated`, `npm outdated`, …).
2. Read the changelog of each major-version jump before changing it.
3. Update one group at a time; run the tests after each.
4. Pin what the project pins; keep the lock file in sync.
5. Report: updated, skipped (and why), anything that needs the user's decision. Installing asks the user first.
