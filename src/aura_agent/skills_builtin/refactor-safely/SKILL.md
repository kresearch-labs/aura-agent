---
name: refactor-safely
description: Restructure code without changing what it does.
license: Apache-2.0
---
1. Run the tests first; note the baseline (they must pass before you start).
2. Find every caller of what you change (code_nav references, search_files).
3. Change in small steps; after each step run the tests.
4. Never mix a refactor with a behaviour change — if you find a bug, report it separately.
5. Finish with the full test run and a short summary of what moved where.
