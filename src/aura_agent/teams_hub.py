"""teams_hub.py — the contract between Aura Teams and any hub.

Teams talks only to these operations; a hub is anything that can do them.
Vikunja (the hub Aura can install) is the first one, written here by hand;
every other app is reached through a wiring file Aura writes, checks on the
live hub and the user approves (teams_wiring.py) — no per-app code.

The task every operation returns is one shape, whatever the hub:
  {"id", "title", "description", "done", "due_date" (ISO date or None),
   "project_id", "assignees": [{"username"}], "labels": [{"title"}],
   "related_tasks": {"blocked": [{"id", "title", "done"}]}}

OPS lists the operations, which ones a hub must have, and what Teams does when
an optional one is missing (said, never silent).
"""
from __future__ import annotations

import datetime as _datetime

OPS: dict[str, tuple[bool, str]] = {
    # name: (required, what happens without it)
    "whoami": (True, ""),
    "list_tasks": (True, ""),
    "get_task": (True, ""),
    "comments": (True, ""),
    "add_comment": (True, ""),
    "set_done": (True, ""),
    "projects": (False, "new tasks can't be put on the board — work is linked to existing tasks only"),
    "create_task": (False, "new tasks can't be put on the board — work is linked to existing tasks only"),
    "labels": (False, "'in progress' and 'decision' are written as a line in a comment"),
    "mark": (False, "'in progress' and 'decision' are written as a line in a comment"),
    "unmark": (False, "'in progress' and 'decision' are written as a line in a comment"),
    "people": (False, "the chat's people list is built from who appears on the board"),
}
REQUIRED = [k for k, (req, _) in OPS.items() if req]


class Hub:
    """The contract. Every method raises teams_board.BoardError (or HubOff /
    TokenRefused) in plain words; nothing here prints."""
    kind = "hub"

    def has(self, op: str) -> bool:
        return callable(getattr(self, op, None))

    # whoami() -> str
    # list_tasks(assignee: str | None, project: int | None, done: bool | None) -> [task]
    # get_task(id) -> task
    # projects() -> [{"id", "title"}]
    # create_task(project_id, title, description) -> task (assigned to me)
    # comments(id) -> [str]
    # add_comment(id, text) -> {"comment": str}
    # labels(id) -> [str]
    # mark(id, label) / unmark(id, label)
    # set_done(id, task) -> task          (task: the whole task as read)


class VikunjaHub(Hub):
    """Vikunja v2.7.1 (pkg/routes/routes.go) — the hub Aura can install.
    The calls are the ones Teams always made (moved here unchanged)."""
    kind = "vikunja"

    def __init__(self):
        from . import teams_board as B
        self.B = B

    def whoami(self) -> str:
        return self.B._auth()[1]

    def list_tasks(self, assignee=None, project=None, done=None) -> list[dict]:
        parts = []
        if assignee:
            parts.append(f"assignees in '{assignee}'")
        if project is not None:
            parts.append(f"project = {int(project)}")
        if done is not None:
            parts.append(f"done = {'true' if done else 'false'}")
        q = self.B._filter(" && ".join(parts)) if parts else ""
        return self.B._call_all("/tasks" + ("?" + q if q else ""))

    def get_task(self, task_id) -> dict:
        return self.B._call("GET", f"/tasks/{int(task_id)}")

    def projects(self) -> list[dict]:
        return self.B._call_all("/projects")

    def create_task(self, project_id, title, description="") -> dict:
        t = self.B._call("PUT", f"/projects/{int(project_id)}/tasks",
                         {"title": title, "description": description})
        if isinstance(t, dict) and t.get("id"):
            me = (t.get("created_by") or {}).get("id")
            if me:
                self.B._call("PUT", f"/tasks/{t['id']}/assignees", {"user_id": me})
        return t

    def comments(self, task_id) -> list[str]:
        return [t for _, t in self.comment_authors(task_id)]

    def comment_authors(self, task_id) -> list[tuple[str, str]]:
        cs = self.B._call("GET", f"/tasks/{int(task_id)}/comments")
        return [((c.get("author") or {}).get("username", ""), c.get("comment", ""))
                for c in cs if isinstance(c, dict)] if isinstance(cs, list) else []

    def people(self) -> list[str]:
        """Everyone who shares a task with this account (assignees, comment authors)."""
        names = set()
        for t in self.list_tasks(done=None)[:200]:
            for a in t.get("assignees") or []:
                if a.get("username"):
                    names.add(a["username"])
        return sorted(names)

    def add_comment(self, task_id, text) -> dict:
        c = self.B._call("PUT", f"/tasks/{int(task_id)}/comments", {"comment": text})
        return c if isinstance(c, dict) else {}

    def labels(self, task_id) -> list[str]:
        return [lab.get("title") for lab in (self.B._call("GET", f"/tasks/{int(task_id)}/labels") or [])
                if isinstance(lab, dict)]

    def _label_id(self, name: str):
        for lab in self.B._call_all("/labels"):
            if lab.get("title") == name:
                return lab["id"]
        made = self.B._call("PUT", "/labels", {"title": name})
        return made.get("id") if isinstance(made, dict) else None

    def mark(self, task_id, label) -> None:
        if label in self.labels(task_id):
            return
        lid = self._label_id(label)
        if lid:
            self.B._call("PUT", f"/tasks/{int(task_id)}/labels", {"label_id": lid})

    def unmark(self, task_id, label) -> None:
        for lab in self.B._call("GET", f"/tasks/{int(task_id)}/labels") or []:
            if isinstance(lab, dict) and lab.get("title") == label:
                self.B._call("DELETE", f"/tasks/{int(task_id)}/labels/{lab['id']}")

    def set_done(self, task_id, task) -> dict:
        # Vikunja's update REPLACES the task: send it whole, only done changed
        body = dict(task)
        body["done"] = True
        return self.B._call("POST", f"/tasks/{int(task_id)}", body)



class AuraHub(Hub):
    """Our own team server, through the contract (plan). The work loop never
    changes: the SAME OPS methods as VikunjaHub. It only ever changes the
    status and adds comments on its own — the person's fields (priority, dates,
    assignees, labels) move only when the user asked.
    """
    kind = "aurahub"

    def __init__(self, cfg: dict | None = None):
        from .teamserver.teamclient import AuraClient
        from . import teams
        cfg = cfg or teams.load_config().get("aura_team")
        if not cfg:
            raise RuntimeError("not on your own team server — join or start "
                               "one first (team_hub_start), or sign in on the hub page")
        self.c = AuraClient(cfg)
        self._version_cache: dict[int, int] = {}

    def whoami(self) -> str:
        status, me = self.c.request("GET", "/api/v1/me")
        if status != 200:
            raise self._err(me, status)
        return me["person"]["name"]

    def list_tasks(self, assignee=None, project=None, done=None) -> list[dict]:
        q = []
        if project is not None:
            q.append(f"project={int(project)}")
        if assignee:
            q.append(f"assignee={assignee}")
        if done is True:
            q.append("status_type=done")
        elif done is False:
            q.append("open=1")          # Not done/canceled (an empty
                                        # status_type= filtered NOTHING out)
        qs = ("?" + "&".join(q)) if q else ""
        status, data = self.c.request("GET", "/api/v1/tasks" + qs)
        if status != 200:
            raise self._err(data, status)
        out = []
        for t in data.get("tasks", []):
            self._version_cache[t["id"]] = t.get("version", 1)
            out.append(self._shaped(t))
        return out

    @staticmethod
    def _iso_due(due) -> str | None:
        """Server `due` is the epoch clock (store.now) or a date string —
        the board shape wants an ISO date (Vikunja's due_date)."""
        if not due:
            return None
        if isinstance(due, (int, float)):
            try:
                return _datetime.datetime.fromtimestamp(
                    due, _datetime.timezone.utc).date().isoformat()
            except (OSError, OverflowError, ValueError):
                return None
        s = str(due)
        if s.startswith("0001-01-01"):
            return None
        return s[:10]

    def _shaped(self, t: dict) -> dict:
        """The task in the shape Teams already knows (the hub contract at the
        top): done/due_date/project_id, assignees as {"username"} dicts, labels
        as {"title"} dicts, server links[kind=blocked_by] as
        related_tasks.blocked [{id, title, done}].
        """
        t = dict(t)
        t.setdefault("done", t.get("status") in ("done", "canceled"))
        t["due_date"] = self._iso_due(t.get("due"))
        t["project_id"] = t.get("project", t.get("project_id"))
        t["assignees"] = [
            dict(a, username=a.get("username") or a.get("name", ""))
            if isinstance(a, dict) else {"username": str(a)}
            for a in (t.get("assignees") or [])]
        labels = [{"title": l} if isinstance(l, str) else l
                  for l in (t.get("labels") or [])]
        # On this hub 'in progress' is the STATUS — surface it as the mark so
        # the shaped task self-describes (never stored)
        if t.get("status") == "in_progress" and not any(
                isinstance(l, dict) and l.get("title") == "in progress"
                for l in labels):
            labels.append({"title": "in progress"})
        t["labels"] = labels
        blocked: list[dict] = []
        for ln in (t.get("links") or []):
            if not isinstance(ln, dict) or ln.get("kind") != "blocked_by" \
                    or ln.get("other") is None:
                continue
            other = ln.get("other")
            entry: dict = {"id": other, "title": "", "done": False}
            try:                               # Raw GET — no recursion
                st, data = self.c.request(
                    "GET", f"/api/v1/tasks/{int(other)}")
                if st == 200 and isinstance(data.get("task"), dict):
                    ot = data["task"]
                    self._version_cache[ot["id"]] = ot.get("version", 1)
                    entry = {"id": ot.get("id", other),
                             "title": ot.get("title", ""),
                             "done": ot.get("status") in ("done", "canceled")}
            except Exception:                   # noqa: BLE001
                pass
            blocked.append(entry)
        rel = dict(t.get("related_tasks") or {})
        rel["blocked"] = blocked
        t["related_tasks"] = rel
        return t

    def get_task(self, task_id) -> dict:
        status, data = self.c.request("GET", f"/api/v1/tasks/{int(task_id)}")
        if status != 200:
            raise self._err(data, status)
        t = data["task"]
        self._version_cache[t["id"]] = t.get("version", 1)
        return self._shaped(t)

    def projects(self) -> list[dict]:
        status, data = self.c.request("GET", "/api/v1/projects")
        if status != 200:
            raise self._err(data, status)
        return [{"id": p["id"], "title": p["name"], "slug": p["slug"],
                 "archived": bool(p.get("archived_at"))}
                for p in data.get("projects", [])]

    def create_task(self, project_id, title, description="") -> dict:
        me = self.whoami()
        status, data = self.c.request("POST", "/api/v1/tasks", {
            "project": int(project_id), "title": title,
            "description": description, "assignees": [me]})
        if status != 200:
            raise self._err(data, status)
        t = data["task"]
        self._version_cache[t["id"]] = t.get("version", 1)
        return self._shaped(t)

    def comments(self, task_id) -> list[str]:
        return [t for _, t in self.comment_authors(task_id)]

    def comment_authors(self, task_id) -> list[tuple[str, str]]:
        status, data = self.c.request("GET",
                                      f"/api/v1/tasks/{int(task_id)}/comments")
        if status != 200:
            raise self._err(data, status)
        return [(c["person"], c["body"]) for c in data.get("comments", [])
                if not c.get("deleted_at")]

    def people(self) -> list[str]:
        status, data = self.c.request("GET", "/api/v1/people")
        if status != 200:
            raise self._err(data, status)
        return [p["name"] for p in data.get("people", [])
                if not p.get("disabled_at")]

    def people_map(self) -> dict:
        """{person id: name} — on this hub a task's created_by is a person ID
        (int), and the 4.26 assigned-tell compares names.
        """
        status, data = self.c.request("GET", "/api/v1/people")
        if status != 200:
            raise self._err(data, status)
        return {int(p["id"]): p.get("name", "") for p in
                data.get("people", []) if p.get("id") is not None}

    def add_comment(self, task_id, text) -> dict:
        status, data = self.c.request("POST",
                                      f"/api/v1/tasks/{int(task_id)}/comments",
                                      {"body": text})
        if status != 200:
            raise self._err(data, status)
        return {"comment": text}

    def labels(self, task_id) -> list[str]:
        t = self.get_task(task_id)
        labels = [str(lab.get("title") or "") if isinstance(lab, dict)
                  else str(lab) for lab in (t.get("labels") or [])]
        labels = [l for l in labels if l]
        # On this hub 'in progress' is the STATUS, not a label — surface it as
        # the mark so every hub answers the same shape
        if t.get("status") == "in_progress" and "in progress" not in labels:
            labels.append("in progress")
        return labels

    def mark(self, task_id, label) -> None:
        """'in progress' is the STATUS in_progress, not a label; 'decision' is a
        TYPED COMMENT (added by the same flow — the marker line is the Vikunja
        fallback, never posted twice); other labels are labels.
        """
        if label == "in progress":
            self._set_status(task_id, "in_progress")
            return
        if label == "decision":
            return                    # One typed decision comment, not two
        self._patch(task_id, {"labels": self._labels_with(task_id, label)})

    def unmark(self, task_id, label) -> None:
        if label == "in progress":
            # a done/canceled status is never stomped back to todo (the
            # done flow sets the status FIRST, detaches the mark after)
            cur = self.get_task(task_id)
            if cur.get("status") in ("done", "canceled") or cur.get("done"):
                return
            self._set_status(task_id, "todo")
            return
        self._patch(task_id, {"labels": [l for l in self.labels(task_id)
                                         if l not in (label, "in progress")]})

    def set_done(self, task_id, task) -> dict:
        self._patch(task_id, {"status": "done",
                              "version": task.get("version",
                                                  self._version_cache.get(
                                                      int(task_id), 1))})
        return self.get_task(task_id)

    def _labels_with(self, task_id, label) -> list:
        labels = [l for l in self.labels(task_id)
                  if l != label and l != "in progress"]   # Status pseudo-mark
        labels.append(label)                              # Never a stored label
        return labels

    def _set_status(self, task_id, key: str) -> None:
        v = self._version_cache.get(int(task_id))
        if v is None:
            v = self.get_task(task_id).get("version", 1)
        self._patch(task_id, {"status": key, "version": v})

    def _patch(self, task_id, body: dict) -> dict:
        if "version" not in body and int(task_id) in self._version_cache:
            body = dict(body, version=self._version_cache[int(task_id)])
        status, data = self.c.request("PATCH", f"/api/v1/tasks/{int(task_id)}",
                                      body)
        if status == 409:
            from .teams_board import BoardError
            raise BoardError(data.get("error", {}).get(
                "message", "changed by someone else"))
        if status != 200:
            raise self._err(data, status)
        t = data["task"]
        self._version_cache[t["id"]] = t.get("version", 1)
        return t

    def _err(self, data, status):
        from .teams_board import BoardError
        msg = (data.get("error", {}).get("message") if isinstance(data, dict)
               else None) or f"the team server refused ({status})"
        return BoardError(msg)

_CACHE: dict = {}


def current() -> Hub:
    """The hub this machine is connected to: a wired one when a wiring is
    in use (teams_wiring), else Vikunja."""
    from . import teams
    cfg = teams.load_config()
    at = cfg.get("aura_team") or {}
    key = (cfg.get("hub_kind"), cfg.get("hub_url"), cfg.get("wiring_sha"),
           at.get("url"), at.get("device_id"), at.get("fingerprint"))
    if _CACHE.get("key") == key and _CACHE.get("hub") is not None:
        return _CACHE["hub"]
    if cfg.get("hub_kind") == "wired":
        from .teams_wiring import WiredHub, load_in_use
        hub: Hub = WiredHub(load_in_use())
    elif cfg.get("aura_team"):
        hub = AuraHub(cfg["aura_team"])
    else:
        hub = VikunjaHub()
    _CACHE.update(key=key, hub=hub)
    return hub
