---
name: investigate-logs
description: Find the cause of a problem in log files: narrow by time and request, follow the first error, not the loudest.
license: Apache-2.0
---
1. Ask or find when it happened and what was affected (a request id, a user, a time window).
2. Search, don't read everything: search_files for ERROR/Exception/Traceback/the request id, within the window.
3. Find the FIRST error in the chain; later errors are usually consequences.
4. Follow one request end to end by its id across services' logs.
5. Count it: how often, since when, what changed then (a deploy, a config change — git log for that time).
6. Report: what happened, the evidence (log lines with timestamps), the likely cause, and what is still unknown.
