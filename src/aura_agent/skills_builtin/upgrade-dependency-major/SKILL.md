---
name: upgrade-dependency-major
description: Upgrade a framework or library across a major version: read the migration guide, change in steps, keep tests green.
license: Apache-2.0
---
1. Find the current and target versions and read the official migration guide / changelog (web_search, web_crawl) — list the breaking changes that touch this project.
2. Search the code for each affected API (search_files) and make a checklist (todo_write).
3. Upgrade on a clean state; change one breaking area at a time, running the tests after each (run_checks).
4. Installing the new version changes the user's environment: show the command and ask first.
5. Report what changed, what was deprecated but left, and anything not covered by tests.
