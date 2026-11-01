---
name: reproduce-bug
description: Turn a bug report into a reliable reproduction before fixing.
license: Apache-2.0
---
1. Restate the report: expected vs actual, steps, environment.
2. Build the smallest script or test that shows the wrong behaviour; run it and see it fail.
3. If it doesn't reproduce, list what differs (version, OS, data) and ask the user — don't guess a fix.
4. Keep the reproduction as a test so the fix can be proven.
