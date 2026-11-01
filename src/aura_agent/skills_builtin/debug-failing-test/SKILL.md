---
name: debug-failing-test
description: Find and fix why a test fails: reproduce, narrow, fix the cause, prove it.
license: Apache-2.0
---
1. Run only the failing test first (run_checks, or the project's test command with a filter) and read the full error.
2. Read the test, then the code it calls (read_file; code_nav definition/references for names you don't know).
3. State the cause in one sentence before changing anything. If two causes are possible, add a print/assert to tell them apart.
4. Change the smallest thing that fixes the cause — not the test, unless the test itself is wrong (say so).
5. Run the failing test again, then the whole suite. Record the fix with finding_add after the passing run.
6. If it can't be fixed, report what you found and what you ruled out.
