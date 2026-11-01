---
name: regex-help
description: Write, explain or fix a regular expression, tested against examples that must and must not match.
license: Apache-2.0
---
1. Collect examples: strings that must match and strings that must not (ask for real ones if possible).
2. Build the pattern in pieces; anchor it (^ $ or \b) when the whole thing must match.
3. Test it with a short script over all examples (run_command with Python's re, or the language it will run in — flavours differ).
4. Watch for catastrophic backtracking on nested quantifiers ((a+)+); prefer specific classes over .*.
5. Give the pattern, a plain explanation of each part, and the test results.
