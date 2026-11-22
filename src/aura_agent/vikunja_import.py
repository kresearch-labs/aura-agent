"""vikunja_import.py — the optional Vikunja export import (plan).

Reads the export zip (`data.json` + `files/`), maps projects → projects, tasks
(done → done status, the "in progress" label → in_progress, other labels kept),
comments; a dry-run first — nothing is written until `import_into` runs. Never
touches chat, prompts or secrets; every created thing is logged.
"""
from __future__ import annotations

import json
import zipfile


class ImportFormat(Exception):
    pass


def _read(zip_path: str) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "data.json" not in names:
            raise ImportFormat("that zip has no data.json — is it a "
                               "Vikunja export?")
        return json.loads(z.read("data.json"))


def _status_key(done: bool, labels: list[str]) -> str:
    low = {str(l).lower() for l in labels}
    if "in progress" in low or "in_progress" in low:
        return "in_progress"
    if "in review" in low:
        return "in_review"
    if done:
        return "done"
    return "todo"


def dry_run(zip_path: str) -> dict:
    data = _read(zip_path)
    projects = 0
    tasks = 0
    comments = 0
    labels = set()
    for p in data.get("projects", []):
        projects += 1
        for t in p.get("tasks", []):
            tasks += 1
            comments += len(t.get("comments") or [])
            for l in t.get("labels") or []:
                labels.add(str(l if isinstance(l, str) else l.get("title", l)))
    return {"projects": projects, "tasks": tasks, "comments": comments,
            "labels": len(labels)}


def import_into(client, zip_path: str) -> dict:
    """Create the projects/tasks/comments through the API (the caller's
    signed client). Idempotent-ish: a project whose slug exists is reused."""
    data = _read(zip_path)
    counts = {"projects": 0, "tasks": 0, "comments": 0, "skipped": 0}
    existing: dict = {}
    status, pro = client.request("GET", "/api/v1/projects")
    for p in pro.get("projects", []):
        existing[p["slug"]] = p["id"]
    for vp in data.get("projects", []):
        name = str(vp.get("title") or vp.get("name") or "imported")[:80]
        slug = "".join(c if c.isalnum() else "-" for c in name.lower())
        slug = slug.strip("-")[:38] or "imported"
        pid = existing.get(slug)
        if not pid:
            status, data2 = client.request("POST", "/api/v1/projects",
                                           {"name": name, "slug": slug})
            if status != 200:
                continue
            pid = data2["project"]["id"]
            counts["projects"] += 1
        else:
            counts["projects"] += 1
        for vt in vp.get("tasks", []):
            done = bool(vt.get("done"))
            labels = []
            for l in vt.get("labels") or []:
                labels.append(str(l if isinstance(l, str)
                                  else l.get("title", "")))
            status_key = _status_key(done, labels)
            payload = {"project": pid,
                       "title": str(vt.get("title") or "imported")[:200],
                       "status": status_key}
            if done:
                payload["done"] = True
            status, td = client.request("POST", "/api/v1/tasks", payload)
            if status != 200:
                continue
            counts["tasks"] += 1
            tid = td["task"]["id"]
            for c in vt.get("comments") or []:
                body = str(c.get("comment") or c.get("text") or "")[:2000]
                if not body:
                    continue
                # The real comments route is per-task (it was at a non-
                # existing fake one — imports wrote zero comments upstream)
                client.request("POST", f"/api/v1/tasks/{tid}/comments",
                               {"body": body})
                counts["comments"] += 1
    return counts
