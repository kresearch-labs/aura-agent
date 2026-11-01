---
name: security-review
description: Look for security problems in code or configuration.
license: Apache-2.0
---
1. Find entry points: request handlers, CLI args, file/URL inputs, deserialisation.
2. Check each for: injection (SQL, shell, template), path traversal, unsafe deserialisation, SSRF, missing auth checks, secrets in code or logs, weak crypto, overly broad permissions.
3. Search for known-dangerous calls (eval, exec, shell=True, pickle.loads, yaml.load, innerHTML, md5 for passwords).
4. For dependencies, note versions with known advisories if a scanner is available (pip-audit, npm audit) — run only if installed.
5. Report each issue: where, how it can be exploited, severity, fix. Never run exploit code against systems outside the project.
