"""teams_run.py — Aura Teams inside Aura's own work.

The commands in `aura teams …` are for people; this is what Aura itself does on
every task once this machine is connected to a hub. Nothing here runs when it
isn't (one config read, no hub call).

The loop calls three things:
  start(task) before the model is called — link the work to a board
  before_write(path) before a file is changed — the planned files were only
                     an estimate, so a file in someone else's open or
                     unmerged work is asked about when it is reached
  finish(...) after the run — with proof, ask whether the task is
                     finished: commit locally, ask to push, the done update

One chat continues one task: the link is remembered per chat until the task is
done.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from . import teams, teams_board as B, teams_collide as C, teams_queue as Q
from .paths import aura_home

Ask = Callable[..., object]
# a file name may hold more dots (food.test.mjs, app.config.ts): the match
# runs to the last extension, never stopping inside the name
PATHISH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.[A-Za-z0-9]{1,8})"
                     r"(?![\w/]|\.[A-Za-z0-9])")
YES_START, NO_START = "start", "don't start"


def _work_perm_probe(project_id) -> "bool | None":
    """Has this machine (its person on the hub) an Aura-work lane in this
    project? None = can't tell (hub off) — never blocks the work."""
    if not project_id:
        return None
    try:
        from . import teams as _T
        at = _T.load_config().get("aura_team")
        if not at:
            return None
        from .teamserver.teamclient import AuraClient
        c = AuraClient(at)
        if c.sk is None:
            return None
        st, d = c.request("GET",
                          f"/api/v1/me/permissions?project={int(project_id)}")
        if st != 200:
            return None
        perms = set(d.get("permissions") or [])
        board_writes = {"add_work_packages", "edit_work_packages",
                        "move_work_packages", "manage_work_package_relations",
                        "aura_work"}
        return (len(perms & board_writes) > 0) or "aura_work" in perms
    except Exception:                                # noqa: BLE001
        return None


def link_project_for(chat_key: str) -> "int | None":
    """The project an Aura's reports land in — the chat's own link, never a
    process-wide value. None when the chat isn't linked (and that chat's writes
    stay silent).
    """
    return (_load().get(chat_key) or {}).get("project_id")


def link_task_for(chat_key: str) -> "int | None":
    """The board task this chat's work is for (None when not linked)."""
    return (_load().get(chat_key) or {}).get("task")


def active() -> bool:
    """Teams is wired here when the work loop should run. Off until setup (:
    nothing linked, nothing reported) — then purely config below, never a hub
    call. Pure config read — never a hub call (nothing Vikunja-shaped is even
    imported). Our own server: signing in writes `aura_team` with the address
    and team id and never sets a legacy hub_url.
    """
    cfg = teams.load_config()
    if not teams.enabled():
        return False                     # FIRST: even
                                         # an old aura_team can't flip it on
    at = cfg.get("aura_team") or {}
    if at.get("url") and at.get("team_id") and cfg.get("hub_kind") != "wired":
        return True                                  # Joined = connected
    return bool(cfg.get("hub_url") and cfg.get("username")
                and cfg.get("hub_kind") != "pending")       # Any-hub: wired first


# ------------------------------------------------------- per-chat link ---
def _links_path() -> Path:
    d = aura_home() / "teams"
    d.mkdir(parents=True, exist_ok=True)
    return d / "links.json"


def _load() -> dict:
    try:
        return json.loads(_links_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(all_links: dict) -> None:
    _links_path().write_text(json.dumps(all_links, indent=1), encoding="utf-8")


def planned_from(*texts: str) -> list[str]:
    """Files the request and the task name — the start estimate."""
    out: list[str] = []
    for t in texts:
        for m in PATHISH.findall(t or ""):
            ext = m.rsplit(".", 1)[1]
            if m in out or m.lower().startswith(("http", "www.")) \
                    or not re.search(r"[A-Za-z]", ext) or len(m) < 4 \
                    or re.fullmatch(r"v?\d+(\.\w+)+", m):
                continue                    # A version, "e.g", a link
            out.append(m.replace("\\", "/"))
    return out[:30]



def _report(kind: str, project, subject: str, data: dict) -> None:
    """Loop events ride the report queue; swallowed — reporting must never break
    the run.
    """
    try:
        from . import teams_report as R
        ev = R.from_loop(kind, project, subject, data)
        if ev is not None:
            R.queue_report(ev)
    except Exception:                            # noqa: BLE001
        pass


def _project_id(task_id) -> int | None:
    """The project the task belongs to (for the report events' project
    chain); None when unknown — events still carry no project then."""
    try:
        from . import teams_hub
        h = teams_hub.current()
        t = h.get_task(task_id) if h and hasattr(h, "get_task") else None
        return t.get("project") if isinstance(t, dict) else None
    except Exception:                            # noqa: BLE001
        return None


class TeamsRun:
    def __init__(self, chat_key: str, ask: Ask | None,
                 tell: Callable[[str], None], work_dir: Path | str,
                 emit: Callable[[dict], None] | None = None):
        self.key = chat_key
        self.ask_fn = ask
        self._tell = tell
        # the chat screen draws team cards from these (kind, task, details);
        # without it every line goes to `tell` as plain text, as before
        self.emit = emit
        self.work_dir = Path(work_dir)
        self.repo = str(self.work_dir) if C.is_repo(str(self.work_dir)) else None
        self.me = teams.my_name()
        self.link: dict = _load().get(chat_key, {})

    # ------------------------------------------------------------ helpers
    def tell(self, text: str, kind: str = "note", **data) -> None:
        """One team event: a team card on the chat screen, plain text elsewhere."""
        if self.emit is not None:
            self.emit({"kind": kind, "text": text, "task": self.link.get("task"),
                       "title": self.link.get("title", ""), **data})
        else:
            self._tell(text)

    def _keep(self) -> None:
        links = _load()
        if self.link:
            links[self.key] = self.link
        else:
            links.pop(self.key, None)
        _save(links)

    def ask(self, question: str, options: list[str]) -> str | None:
        """The chosen option, or None (Enter alone / nobody to ask)."""
        if self.ask_fn is None:
            return None
        # a team question is headed TEAM on the chat screen
        ans = self.ask_fn(("[team] " if self.emit is not None else "") + question, options)
        if isinstance(ans, (list, tuple)):
            ans = ans[0] if ans else None
        ans = str(ans).strip() if ans else None
        if ans and ans not in options:          # Their own words: match a start
            low = ans.lower()
            ans = next((o for o in options if o.lower().startswith(low[:3])), ans)
        return ans

    def _project_repos(self, t: dict) -> list:
        """The project's bound repositories — for 4.0e prefixing."""
        try:
            from . import teams as _T
            at = _T.load_config().get("aura_team")
            if not at:
                return []
            from .teamserver.teamclient import AuraClient
            c = AuraClient(at)
            if c.sk is None:
                return []
            pid = t.get("project_id") or t.get("project")
            status, data = c.request("GET", f"/api/v1/projects/{int(pid)}")
            if status != 200:
                return []
            p = data.get("project", {})
            s = p.get("settings") or {}
            if isinstance(s, str):
                import json as _j
                s = _j.loads(s)
            return list((s or {}).get("repos") or [])
        except Exception:                                # noqa: BLE001
            return []

    def _rel(self, path: str) -> str:
        """Repository-relative (plan): to the repo root when this is a git
        repository, else to the work dir; `/` separators, no `./`; `..` and
        absolute paths never leave this function. 4.0e: a project bound to two
        or more repositories prefixes the path with this repository's short
        name, so the same relative path in two clones never reads as the same
        file.
        """
        try:
            from .teams_collide import rel_to_repo
            rel = rel_to_repo(path, str(self.work_dir))
        except Exception:                        # noqa: BLE001
            p = Path(path)
            try:
                p = (self.work_dir / p).resolve().relative_to(
                    self.work_dir.resolve())
            except (ValueError, OSError):
                pass
            rel = str(p).replace("\\", "/")
        try:
            repos = list((self.link or {}).get("repos") or [])
            if len(repos) > 1 and getattr(self, "repo", None):
                from .teams_collide import repo_identity
                ident = repo_identity(str(self.repo))
                short = (ident or repos[0]).rsplit("/", 1)[-1]
                if short and not rel.startswith(short + "/"):
                    rel = f"{short}/{rel}"
        except Exception:                        # noqa: BLE001
            pass
        return rel

    @property
    def task_id(self) -> int | None:
        return self.link.get("task")

    # ------------------------------------------------------------ link
    def _pick(self, request: str) -> dict | None:
        """Ask which board task this is. Returns the task, {} for "not on
        the board", None for no answer (asked again next time)."""
        m = B.match_request(request, chat_key=self.key)
        if m["by_id"]:
            if m["match"]:
                return m["match"]
            self.tell(f"#{m['by_id']} is not on the board (or not shared with you)")
        cands = m["candidates"]
        if m["match"]:
            t = m["match"]
            a = self.ask(f"This looks like #{t['id']} {t.get('title', '')} — is it that one?",
                         ["yes", "no — another of my tasks", "no — it isn't on the board"])
            if a == "yes":
                return t
            if a is None:
                return None
            if a.startswith("no — it isn't"):
                return self._offer_create(request)
            cands = []
        mine = cands or B.my_tasks(done=False)
        if mine:
            labels = [f"#{t['id']} {t.get('title', '')}" for t in mine[:9]]
            a = self.ask("Which of your tasks is this?" if cands else
                         "Which of your open tasks is this?",
                         labels + ["it isn't on the board"])
            if a is None:
                return None
            if a in labels:
                return mine[labels.index(a)]
        return self._offer_create(request)

    def _offer_create(self, request: str) -> dict | None:
        a = self.ask("This work isn't on the board. Add it as a task so the team "
                     "can see it?", ["yes, add it", "no, work without the board"])
        if a is None:
            return None
        if not a.startswith("yes"):
            return {}
        projects = B.my_projects()
        if not projects:
            self.tell("no project on the hub is shared with you — working "
                      "without the board")
            return {}
        if len(projects) == 1:
            proj = projects[0]
        else:
            labels = [p.get("title", f"project {p['id']}") for p in projects[:9]]
            pa = self.ask("Which project?", labels)
            if pa not in labels:
                return None
            proj = projects[labels.index(pa)]
        title = request.strip().splitlines()[0][:120] if request.strip() else "work"
        t = B.create_task(proj["id"], title)
        self.tell(kind="created", text=f"added #{t['id']} {t.get('title', '')} to {proj.get('title', '')}, "
                  "assigned to you")
        return t

    # ------------------------------------------------------------- start
    def start(self, request: str) -> dict:
        """{"go": bool, "why": str, "context": str} — context is the board
        task for the model, as outside text, the first time only."""
        go = {"go": True, "why": "", "context": ""}
        if not self.link.get("due_told"):
            # Once per chat — the user's own tasks due soon or overdue
            try:
                for line in B.due_tells():
                    self.tell(line, "due")
                self.link["due_told"] = True
                self._keep()
            except B.BoardError:
                pass                    # The hub being off is said below
        if self.link.get("off_board"):
            return go
        context = ""
        try:
            if self.task_id:
                cur = B.get_task(self.task_id)
                if cur.get("done"):
                    self.tell(f"#{self.task_id} is marked done on the board — "
                              "this is new work")
                    self.link = {"due_told": True}
            if not self.task_id:
                if self.ask_fn is None:
                    self.tell("nobody here to ask which board task this is — "
                              "the work isn't linked, so the team won't see it", "off")
                    return go
                t = self._pick(request)
                if t is None:
                    self.tell("not linked to a board task this time (no answer)", "off")
                    return go
                if not t:
                    self.link = {"off_board": True, "due_told": True}
                    self._keep()
                    self.tell("working without the board: the team won't see this "
                              "work, and the collision check can't cover it", "off")
                    return go
                self.link = {"task": t["id"], "title": t.get("title", ""),
                             "project_id": t.get("project_id") or
                             t.get("project"),
                             "files": [], "decisions": [], "told": [],
                             "accepted": [], "started": False,
                             "repos": self._project_repos(t),
                             "due_told": self.link.get("due_told", False)}
                self._keep()
                context = self._context(t)
                # The linked project's Aura-work tick — a role that gives the
                # machine no work lane means: no check, no reports, and told
                # ONCE (the chat still runs its own)
                if self.link.get("work_ok") is None:
                    self.link["work_ok"] = ("#no" if _work_perm_probe(
                        self.link.get("project_id")) is False else True)
                if self.link["work_ok"] is False or \
                        self.link["work_ok"] == "#no":
                    if not self.link.get("work_told"):
                        self.link["work_told"] = True
                        self._keep()
                        self.tell("this project's role gives me no Aura work "
                                  "— no collision check, no reports. Someone "
                                  "with the role takes it from here",
                                  "off")
                    return {"go": True, "context": "",
                            "why": "linked, but the project's role carries "
                                   "no Aura work"}
            read = B.read_task(self.task_id)
            new_blocked = []
            for line in read["tells"]:
                if line not in self.link["told"]:
                    self.tell(line, "blocked" if "blocked" in line else "due")
                    self.link["told"].append(line)
                    if line.startswith("blocked by unfinished"):
                        new_blocked.append(line)
            # work that has to come first isn't done: the person decides, the
            # same way as for a file clash (telling alone let it start anyway)
            if new_blocked:
                a = self.ask(f"#{self.task_id} is {new_blocked[0]}. Start anyway?",
                             [YES_START, NO_START])
                if a != YES_START:
                    self._keep()
                    return {"go": False, "context": "",
                            "why": f"not started — #{self.task_id} is {new_blocked[0]}"}
            t = read["task"]["data"]
            planned = planned_from(request, t.get("description") or "")
            check = C.collision_check(self.task_id, planned, self.repo, self.me)
        except B.TokenRefused as e:
            self.tell(str(e), "warn")
            return self._hub_off_choice(str(e))
        except B.BoardError as e:
            return self._hub_off_choice(str(e))
        if not check["can_check"]:
            return self._hub_off_choice("; ".join(check["lines"]))
        self.link["check"] = {"others": check["others"], "pieces": check["pieces"]}
        new = [ln for ln in check["lines"] if ln not in self.link["told"]]
        for ln in check["lines"]:
            if ln not in self.link["told"]:
                self.link["told"].append(ln)
        if check["hits"] and any(ln for ln in new if not ln.startswith("git fetch")):
            self.tell(f"#{self.task_id} overlaps others' work", "collision",
                      lines=[ln for ln in new if not ln.startswith("git fetch")])
            a = self.ask(f"#{self.task_id} overlaps others' work (above). Start anyway?",
                         [YES_START, NO_START])
            if a != YES_START:
                self._keep()
                return {"go": False, "context": "",
                        "why": f"not started — #{self.task_id} overlaps others' work "
                               "and you chose not to start (or didn't answer)"}
        elif not self.link["started"]:
            self.tell("no collisions with others' work" if not new else "; ".join(new), "check")
        if not self.link["started"]:
            if not self._branch():
                self._keep()
                return {"go": False, "context": "",
                        "why": "not started — you chose to stay off your branch"}
            summary = request.strip().splitlines()[0][:100] if request.strip() else ""
            r = Q.task_start(self.task_id, summary or self.link["title"],
                             self.me if self.repo else "", planned)
            self.tell(f"#{self.task_id} marked in progress" +
                      (" (queued — the hub is off)" if r.get("queued") else ""), "started",
                      branch=self.me if self.repo else "", planned=planned[:8],
                      queued=bool(r.get("queued")))
            self.link["started"] = True
            self.link["files"] = sorted(set(self.link["files"]) | set(planned))
            # The shared map refires when main moved — START lane (the finish
            # lane's version runs at finish; both self-rate through upload's
            # ancestor/no-move gate)
            if self.repo:
                pid2 = self.link.get("project_id")
                if pid2:
                    try:
                        from . import teams as _T2
                        from . import teams_map as _MAP2
                        if _T2.load_config().get("aura_team"):
                            _MAP2.upload(_T2.load_config()["aura_team"],
                                         str(self.repo), int(pid2))
                    except Exception:                # noqa: BLE001
                        pass
            _report("start", self.link.get("project_id") or
                    _project_id(self.task_id), summary or
                    self.link["title"],
                    {"task": self.task_id, "branch": self.me if self.repo
                     else "", "planned_files": planned[:8]})
        self._keep()
        go["context"] = context
        return go

    def _hub_off_choice(self, why: str) -> dict:
        """The check can't run now; the user decides whether to start."""
        if self.link.get("hub_off_ok"):
            return {"go": True, "why": "", "context": ""}
        a = self.ask(f"The collision check can't run now ({why[:160]}). Start anyway?",
                     [YES_START, NO_START])
        if a == YES_START:
            if self.link:
                self.link["hub_off_ok"] = True
                self._keep()
            return {"go": True, "why": "", "context": ""}
        return {"go": False, "context": "",
                "why": "not started — the collision check couldn't run and you "
                       "chose not to start (or didn't answer)"}

    def _branch(self) -> bool:
        if not self.repo:
            return True
        name = self.me
        try:
            cur = C._git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        except C.GitError:
            cur = ""
        if cur == name:
            return True
        # A card before the branch switch — the switch is not ready when
        # somebody's three files still float uncommitted
        changed = C._git(self.repo, "status", "--short")
        n = len([l for l in changed.splitlines() if l.strip()])
        if self.ask_fn is None:
            return False                     # Nobody at the keyboard → stay
        a = self.ask_fn(
            f"Switch to your branch '{name}'? "
            + (f"{n} changed file{'s' if n != 1 else ''} come along"
               if n else "nothing floats here"),
            ["switch", "stay here"])
        if a != "switch":
            self.tell(f"staying on '{cur}' — your work; nothing moved",
                      "branch")
            return False
        try:
            C.ensure_branch(self.repo, self.me)
            self.tell(f"on your branch '{self.me}'", "branch")
            return True
        except C.GitError as e:
            a = self.ask(f"{e}. Work on the current branch instead?",
                         ["yes, the current branch", "no, stop"])
            return bool(a and a.startswith("yes"))

    def _context(self, t: dict) -> str:
        """The task for the model — hub text is OUTSIDE text: the shared banner
        teams_collide.file_hit also uses, plus the sanitizer's external wrap.
        """
        from .secrets_guard import sanitize_output
        body = (f"Board task #{t['id']}: {t.get('title', '')}\n"
                f"{(t.get('description') or '').strip()[:4000]}")
        return B.OUTSIDE_BANNER + sanitize_output(body, is_external=True)

    # ------------------------------------------------------ during the run
    def before_write(self, path: str) -> str | None:
        """None = go ahead; otherwise why not (the model is told)."""
        if not self.task_id:
            return None
        rel = self._rel(path)
        if rel in self.link.get("accepted", []):
            return None
        hit = C.file_hit(rel, self.link.get("check") or {})
        if not hit:
            return None
        a = self.ask(f"{hit}. Change it anyway?", ["yes, change it", "no"])
        if a and a.startswith("yes"):
            self.link.setdefault("accepted", []).append(rel)
            self._keep()
            return None
        return (f"the user chose not to change {rel} now: {hit}. Do the rest "
                "of the task without changing it, and say so in your answer.")

    # ------------------------------------------------------------- decide
    def decision(self, text: str, why: str | None = None) -> str:
        if not self.task_id:
            return "this chat isn't linked to a board task — nothing recorded"
        r = Q.task_decision(self.task_id, text)
        # The decision and WHY ride the linked project's report — the event
        # carries both (the server's data allow-list admits `why` separately)
        _report("decision", self.link.get("project_id") or
                _project_id(self.task_id), text[:60],
                {"task": self.task_id, "text": text,
                 **({"why": why} if why else {})})
        self.link.setdefault("decisions", []).append(text)
        self._keep()
        return (f"decision recorded on #{self.task_id}" +
                (" (queued — the hub is off)" if r.get("queued") else ""))

    # ------------------------------------------------------------- finish
    def finish(self, changed: list[str], proof: str | None, failed: bool) -> None:
        """After a run. 's no-work lane goes home first: when the project's role
        stops the Aura from working, the run collected nothing and posts
        nothing.
        """
        link0 = self.link if isinstance(self.link, dict) else {}
        if link0.get("work_ok") == "#no" or link0.get("off_board"):
            return
        if not self.task_id:
            return
        rels = [self._rel(c) for c in changed]
        self.link["files"] = sorted(set(self.link.get("files", [])) | set(rels))
        self._keep()
        if not rels or failed:
            return
        if not proof:
            self.tell(f"#{self.task_id} stays in progress — no proof yet "
                      "(nothing checked the work after the last change)")
            return
        a = self.ask(f"Is #{self.task_id} {self.link.get('title', '')} finished? "
                     f"(proof: {proof})", ["yes — mark it done", "not yet"])
        if not (a and a.startswith("yes")):
            return
        # this task's files that are really there to commit: changed or made
        # (on disk), or removed (git still knows them). A planned file the
        # work never touched must not stop the commit.
        files = [f for f in self.link["files"]
                 if (self.work_dir / f).exists()
                 or (self.repo and C.tracked(self.repo, f))]
        commit, pushed = "", False
        if self.repo:
            try:
                commit = C.commit_local(self.repo, files,
                                        f"#{self.task_id} {self.link.get('title', '')}") or ""
            except C.GitError as e:
                self.tell(f"#{self.task_id} not marked done: the commit failed ({e})", "warn")
                return
            if commit:
                self.tell(f"committed {commit} on '{self.me}'", "commit")
                p = self.ask(f"Push '{self.me}' now?", ["yes, push", "not now"])
                if p and p.startswith("yes"):
                    r = C.push_branch(self.repo, self.me)
                    pushed = r["pushed"]
                    self.tell("pushed" if pushed else r["detail"], "push" if pushed else "warn")
        check = self.link.get("check") or {}
        shared = [f for f in files if C.file_hit(f, check)]
        r = Q.task_done(self.task_id, files, shared,
                        "; ".join(self.link.get("decisions", [])) or "none",
                        commit, pushed, proof)
        if r.get("held"):
            self.tell(r["detail"])
            return
        if r.get("queued"):
            self.tell(f"#{self.task_id} done — queued until the hub is back", "done", queued=True)
        elif r.get("ok"):
            self.tell(f"#{self.task_id} marked done on the board", "done")
        else:
            self.tell(r.get("detail") or f"#{self.task_id}: could not mark done", "warn")
            return
        # The task's model usage rides the finished report (visible only under
        # activity-detail; never a per-person ranking)
        from . import teams_usage as _UU
        # the CHAT's identity — never the task's: the loop links/records
        # usage under the session stem (cli sets loop.chat_id to it), and
        # self.key IS that stem for this chat
        usage = _UU.snapshot(self.key) or {}
        # Real per-task seconds + cost when the run's receipt knows them
        try:
            from . import usage_report as _UR
            import time as _tm
            wd = getattr(self, "work_dir", None)
            recs = _UR._receipts([Path(wd)]) if wd else []
            latest = max(recs, key=lambda r: float(r.get("started_ts") or 0),
                         default=None)
            if latest:
                usage = dict(usage)
                usage["seconds"] = round(max(
                    float(usage.get("seconds") or 0.0),
                    _tm.time() - float(latest.get("started_ts") or 0)), 1)
                if isinstance(latest.get("cost_usd"), (int, float)):
                    usage["cost_usd"] = round(float(latest["cost_usd"]), 6)
        except Exception:                            # noqa: BLE001
            pass
        _UU.clear(self.key)                          # Usage is per task
        _report("finish", self.link.get("project_id") or
                _project_id(self.task_id),
                self.link.get("title", ""),
                {"task": self.task_id, "changed": rels, "commit": commit,
                 "pushed": pushed, "proof": proof,
                 "usage": usage if isinstance(usage, dict) else {}})
        # Refresh the project's shared map (swallowed — reporting must never
        # break the run)
        if commit and self.repo:
            try:
                from . import teams as _T
                from . import teams_map as _MAP
                at = _T.load_config().get("aura_team")
                if at:
                    pid = self.link.get("project_id")
                    if pid:
                        _MAP.upload(at, str(self.repo), int(pid))
            except Exception:                        # noqa: BLE001
                pass
        self.link = {"due_told": True}
        self._keep()
