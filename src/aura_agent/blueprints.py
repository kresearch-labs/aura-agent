"""blueprints.py — ready-made scheduled jobs.

Aura's are written here in our own words, run on the same job store as the
`cronjob` tool (`aura cron watch` runs them), and each one is shown in full and
confirmed before it is added. The tasks only read and report — a scheduled run
changes nothing in the project unless the user's own fields ask it to.
"""
from __future__ import annotations

import re

BLUEPRINTS: dict[str, dict] = {
    "morning-brief": {
        "about": "a short morning summary of news on your topics",
        "fields": {"topics": "AI agents, Python", "time": "08:00"},
        "schedule": "at {time}",
        "task": ("Morning brief on: {topics}. Use web_search for the last 24 hours on each topic, "
                 "open the best 2-3 sources with web_crawl, and write a short brief: 3-5 points per "
                 "topic, each with its source link. Say plainly when a topic had nothing new."),
    },
    "watch-page": {
        "about": "check a web page and tell you when something on it changes",
        "fields": {"url": "", "what": "the price", "every": "every 6 hours"},
        "schedule": "{every}",
        "task": ("Check {url} for {what}. Read the page with web_crawl, find {what}, and compare it "
                 "with the value saved under the state key 'watch:{url}' (state tool). If it changed "
                 "(or nothing was saved yet), save the new value with state_write and report old -> "
                 "new; if it is the same, answer only 'no change'. If the page can't be read, say so."),
        "needs_site": "url",
    },
    "weekly-review": {
        "about": "what happened in this project this week: work done, results, open items",
        "fields": {"every": "every week"},
        "schedule": "{every}",
        "task": ("Weekly review of this project. Use history_search (after: 7d) for this week's "
                 "chats and their outcomes, `git log --since='7 days ago' --oneline` if it is a git "
                 "repo, and the todo list. Write: what was done, results proven, what failed or "
                 "is still open, and the next 3 steps. Change nothing."),
    },
    "test-watch": {
        "about": "run the project's tests on a schedule and report only when they fail",
        "fields": {"every": "every 6 hours"},
        "schedule": "{every}",
        "task": ("Run the project's tests with run_checks (kind test, force true). If they pass, "
                 "answer only 'tests pass'. If they fail, name the failing tests and the first "
                 "error of each. Change nothing."),
    },
    "dependency-check": {
        "about": "which of the project's dependencies are outdated or have known problems",
        "fields": {"every": "every week"},
        "schedule": "{every}",
        "task": ("Check this project's dependencies: find its dependency files, list what is "
                 "outdated (pip list --outdated, npm outdated, or the ecosystem's command) and any "
                 "known security advisories you can find with web_search. Report a short table; "
                 "install or change nothing."),
    },
    "repo-digest": {
        "about": "a daily summary of the commits in this repository",
        "fields": {"time": "18:00"},
        "schedule": "at {time}",
        "task": ("Summarize today's commits in this repository: `git log --since=midnight --stat`. "
                 "Group them by area, one line each, and point out anything risky (large deletions, "
                 "config or dependency changes). If there were none, say so. Change nothing."),
    },
    "release-watch": {
        "about": "tell you when a package or project releases a new version",
        "fields": {"package": "", "where": "PyPI", "every": "every day"},
        "schedule": "{every}",
        "task": ("Find the latest version of {package} on {where} (web_search / web_crawl the "
                 "official page). Compare with the state key 'release:{package}'. If it is new, "
                 "save it with state_write and report the version, date and the main changes from "
                 "its release notes; otherwise answer only 'no new release'."),
    },
    "research-digest": {
        "about": "new papers or articles on a topic, once a week",
        "fields": {"topic": "", "every": "every week"},
        "schedule": "{every}",
        "task": ("Weekly research digest on: {topic}. Search for papers and articles from the last "
                 "7 days (web_search; arXiv for papers), open the best few, and list each with "
                 "title, link and two sentences on what is new. Skip ones you can't open."),
    },
    "todo-sweep": {
        "about": "the TODO / FIXME notes added to the code recently",
        "fields": {"every": "every week"},
        "schedule": "{every}",
        "task": ("List the TODO, FIXME and HACK notes in this project (search_files), newest "
                 "first when git can tell (`git log -S` or blame). Group them by file and mark the "
                 "ones that look urgent. Change nothing."),
    },
    "log-errors": {
        "about": "scan a log file for new errors",
        "fields": {"path": "", "every": "every hour"},
        "schedule": "{every}",
        "task": ("Read the end of the log file {path} (read_file) and find errors and exceptions "
                 "newer than the time saved under the state key 'logs:{path}'. Save the newest "
                 "time with state_write. Report each new error once with its count; if none, answer "
                 "only 'no new errors'."),
    },
}


def fill(name: str, values: dict) -> tuple[str, str, list[str]]:
    """(task text, schedule text, missing field names) for a blueprint."""
    bp = BLUEPRINTS[name]
    vals = {k: (values.get(k) if values.get(k) not in (None, "") else d)
            for k, d in bp["fields"].items()}
    missing = [k for k, v in vals.items() if not str(v).strip()]
    if bp.get("needs_site") and vals.get(bp["needs_site"]) and \
            not re.match(r"https?://", str(vals[bp["needs_site"]])):
        missing.append(bp["needs_site"] + " (must start with http:// or https://)")
    task = bp["task"].format(**vals) if not missing else ""
    sched = bp["schedule"].format(**vals) if not missing else ""
    return task, sched, missing


def parse_values(items: list[str]) -> dict:
    """['time=08:00', 'topics=AI, chips'] → dict."""
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out
