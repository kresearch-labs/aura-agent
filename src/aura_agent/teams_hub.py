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


_CACHE: dict = {}


def current() -> Hub:
    """The hub this machine is connected to: a wired one when a wiring is
    in use (teams_wiring), else Vikunja."""
    from . import teams
    cfg = teams.load_config()
    key = (cfg.get("hub_kind"), cfg.get("hub_url"), cfg.get("wiring_sha"))
    if _CACHE.get("key") == key and _CACHE.get("hub") is not None:
        return _CACHE["hub"]
    if cfg.get("hub_kind") == "wired":
        from .teams_wiring import WiredHub, load_in_use
        hub: Hub = WiredHub(load_in_use())
    else:
        hub = VikunjaHub()
    _CACHE.update(key=key, hub=hub)
    return hub
