---
name: write-tests
description: Add tests for existing code in the project's own test style.
license: Apache-2.0
---
1. Find the project's test setup (pyproject/package.json/tests folder) and copy the style of an existing test file.
2. List the behaviours to cover: the normal case, edge cases (empty, large, wrong type), error cases.
3. Write small focused tests, one behaviour each, with clear names.
4. Run them. A new test that fails on unchanged code is either a real bug (report it) or a wrong test (fix the test).
5. Report what is covered and what is still not.
