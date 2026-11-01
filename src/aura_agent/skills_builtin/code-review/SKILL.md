---
name: code-review
description: Review a change for bugs, risks and missing tests before it is merged.
license: Apache-2.0
---
1. Get the change: `git diff` (or the files named). Read every changed hunk with its surrounding code.
2. For each hunk ask: can this crash, lose data, leak a secret, break an existing caller (code_nav references), or behave differently on another OS?
3. Check error paths, empty/None inputs, off-by-one, resource cleanup, concurrency.
4. Check tests cover the new behaviour; name the missing cases.
5. Report findings ordered by severity: file:line, what goes wrong, a concrete fix. Say plainly when you found nothing serious — don't invent issues.
