---
name: incident-postmortem
description: Write a blameless incident postmortem: timeline, impact, root cause, what went well, actions with owners.
license: Apache-2.0
---
1. Gather facts first: timestamps, alerts, logs, the changes around that time (git log, deploy records) — ask the user for what isn't in files.
2. Timeline in UTC from first sign to full recovery.
3. Impact in numbers (users, requests, minutes, money if known).
4. Root cause and contributing causes — systems and processes, never blame people.
5. What went well, what went badly, where we got lucky.
6. Actions: specific, each with an owner placeholder and a priority. Mark anything unverified.
