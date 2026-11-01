"""teams_queue.py — Aura Teams: hub-off queue + sync and the security checks.

When the hub is off, updates wait in a local queue, in order, no limit; they
are sent when the hub is back. Comments are sent as they are (only ever added —
comments can't conflict). Task changes (marking done).

In order means in order: while anything waits, a new update waits behind it (it
is tried right after the queue is sent).

Tokens live in the key store, never shown; an expired token is not queued (it
would fail again) — the user is told to make a new one. The hub never controls
an Aura: Aura only ever makes outbound HTTP calls to the hub (verify: no
listening sockets in the hub-talking modules). No secrets and no code to the
hub — only the comment fields, scrubbed. Plain http is for the LAN; a
public-looking address over http gets a caution at connect time.
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import os

from . import teams_board as B
from .paths import aura_home


def _queue_path():
    d = aura_home() / "teams"
    d.mkdir(parents=True, exist_ok=True)
    return d / "queue.jsonl"


def queue_items() -> list[dict]:
    p = _queue_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def queue_len() -> int:
    return len(queue_items())


def queue_append(kind: str, task_id: int, payload: dict) -> dict:
    """Ordered, no limit: append and say where it waits."""
    item = {"kind": kind, "task_id": int(task_id), "payload": payload,
            "queued_at": datetime.datetime.now().isoformat(timespec="seconds")}
    with open(_queue_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(item) + "\n")
    return item


def queue_rewrite(items: list[dict]) -> None:
    p = _queue_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")
    os.replace(tmp, p)


# ----------------------------------------------------- sending one item ---
def _send(item: dict) -> dict:
    """Send one update. Raises HubOff / TokenRefused / BoardError."""
    kind, tid, p = item["kind"], item["task_id"], item["payload"]
    if kind == "start":
        B.attach_label(tid, B.IN_PROGRESS)
        B.add_comment(tid, p["comment"])
        return {"ok": True}
    if kind == "decision":
        B.attach_label(tid, B.DECISION)
        B.add_comment(tid, p["comment"])
        return {"ok": True}
    if kind == "comment":
        B.add_comment(tid, p["comment"])
        return {"ok": True}
    if kind == "done":
        r = B.set_done(tid, p.get("base"))
        if not p.get("comment_sent"):
            B.add_comment(tid, p["comment"])
            p["comment_sent"] = True         # Never twice, even if held
        if r.get("changed_since"):
            return {"ok": False, "held": True,
                    "detail": f"#{tid} was changed by someone else since — "
                              "the done mark was not written; you decide "
                              "(`aura teams sync` again sends it as it is "
                              "now, `aura teams queue drop " + str(tid) + "` drops it)"}
        B.detach_label(tid, B.IN_PROGRESS)
        if r.get("already"):
            return {"ok": True, "detail": f"#{tid} was already marked done"}
        return {"ok": True}
    raise B.BoardError(f"unknown queued update '{kind}'")


def _send_or_queue(kind: str, task_id: int, payload: dict) -> dict:
    """Try the hub now; unreachable → the ordered queue; an expired or
    refused token is NOT queued (it would fail again) — it is raised and
    the user is told."""
    item = {"kind": kind, "task_id": int(task_id), "payload": payload}
    if queue_len():
        s = sync()                       # What waits goes first (in order)
        if queue_len():
            queue_append(kind, task_id, payload)
            return {"ok": True, "queued": True,
                    "detail": f"waiting behind {queue_len() - 1} earlier "
                              f"update(s) — {s['detail']}"}
    try:
        out = _send(item)
    except B.HubOff:
        queue_append(kind, task_id, payload)
        return {"ok": True, "queued": True,
                "detail": f"the hub is off — this update waits in the local "
                          f"queue (position {queue_len()}); it goes out with "
                          f"the next update or `aura teams sync`"}
    if out.get("held"):
        queue_append(kind, task_id, payload)       # comment_sent is kept
        return {"ok": False, "held": True, "queued": True, "detail": out["detail"]}
    return {"ok": out["ok"], "sent": True, **({"detail": out["detail"]}
                                             if out.get("detail") else {})}


def task_start(task_id: int, summary: str, branch: str,
               planned_files: list[str]) -> dict:
    return _send_or_queue("start", task_id,
                          {"comment": B.start_comment(summary, branch, planned_files)})


def task_done(task_id: int, changed: list[str], shared, decision: str,
              commit: str, pushed: bool, proof: str,
              base: str | None = None) -> dict:
    if not (proof and proof.strip()):
        return {"ok": False,
                "detail": "no proof given — a task is marked done only with "
                          "proof (checks run and their result)"}
    if base is None:
        try:
            base = B.fingerprint(B.get_task(task_id))
        except B.HubOff:
            base = None               # Hub off: the done mark re-reads on sync
    return _send_or_queue("done", task_id,
                          {"comment": B.done_comment(changed, shared, decision,
                                                     commit, pushed, proof),
                           "base": base})


def task_decision(task_id: int, text: str) -> dict:
    return _send_or_queue("decision", task_id,
                          {"comment": B.decision_comment(text)})


# ----------------------------------------------------------------- sync ---
def sync() -> dict:
    """Send the queue in order. Comments: sent as they are (append-only).
    The done mark: re-read first — someone else changed the task meanwhile
    → NOT overwritten; the user is told and the item waits. The hub going
    off again stops the sync where it is (order kept)."""
    items = queue_items()
    if not items:
        return {"sent": 0, "held": 0, "lines": [],
                "detail": "nothing queued — the board is current"}
    kept: list[dict] = []
    sent = held = 0
    lines: list[str] = []
    for i, item in enumerate(items):
        tid, kind = item["task_id"], item["kind"]
        try:
            out = _send(item)
        except B.HubOff:
            kept += items[i:]
            lines.append("the hub went off again — the rest waits")
            break
        except B.TokenRefused as e:
            kept += items[i:]
            lines.append(str(e))
            break
        except B.BoardError as e:
            held += 1
            kept.append(item)
            lines.append(f"#{tid} {kind}: held — {e}")
            continue
        if out.get("held"):
            held += 1
            kept.append(item)
            lines.append(out["detail"])
            continue
        sent += 1
        lines.append(f"#{tid} {kind}: sent" + (f" ({out['detail']})"
                                                if out.get("detail") else ""))
    queue_rewrite(kept)
    return {"sent": sent, "held": held, "lines": lines,
            "detail": f"{sent} sent, {held} held, {len(kept)} still queued"}


def drop(task_id: int) -> int:
    """Returns how many."""
    items = queue_items()
    kept = [i for i in items if i["task_id"] != int(task_id)]
    queue_rewrite(kept)
    return len(items) - len(kept)


def queue_summary() -> str:
    items = queue_items()
    if not items:
        return "queue: empty — the board is current"
    lines = [f"queue: {len(items)} update(s) waiting for the hub, in order:"]
    for i, item in enumerate(items, 1):
        body = item["payload"].get("comment", "")[:60].replace("\n", " | ")
        lines.append(f"  {i}. #{item['task_id']} {item['kind']}: {body}")
    lines.append("they go out with the next update, or `aura teams sync`")
    return "\n".join(lines)


# -------------------------------------------------------------- security ---
def link_caution(url: str) -> str | None:
    """Plain http is for the LAN; a public-looking address over http gets a
    caution at connect time (shown once, then the user decides).
    """
    if not url.startswith("http://"):
        return None
    host = url.split("//", 1)[-1].split("/")[0]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.strip("[]")
    if host == "localhost" or host.endswith(".local") or host.endswith(".lan"):
        return None
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return None
    except ValueError:
        pass                    # A name: can't tell — say it once
    return (f"caution: {url} is plain http to a public-looking address — "
            f"fine on your own LAN; online teams should use https")


def security_review() -> list[tuple[str, bool]]:
    """The pass, as checkable statements."""
    import inspect
    from pathlib import Path

    from . import keystore, teams
    from . import teams_board, teams_collide, teams_run
    from .secrets_guard import scrub_secrets
    checks = []
    # 1. token only in the key store, never in the config file
    p = Path(teams.config_path())
    raw = p.read_text(encoding="utf-8") if p.exists() else ""
    tok = keystore.get(teams.TOKEN_KEY) or ""
    checks.append(("token in key store, never in the config file",
                   not tok or tok not in raw))
    # 2. the hub never controls an Aura: the modules that talk to the hub
    # only make calls out — none of them listens (the chat page is a
    # server for people, beside the hub, and is not one of them)
    src = "".join(inspect.getsource(m) for m in
                  (teams, teams_board, teams_collide, teams_run))
    inbound = any(w in src for w in ("HTTPServer", "serve_forever",
                                     ".listen(", ".bind(", "socketserver"))
    checks.append(("the hub never controls an Aura (no listeners)", not inbound))
    # 3. no secrets to the hub: the scrubber runs on every comment
    probe = B.done_comment(["a.py"], [], "key sk-live-aabbccddeeff00112233445566778899",
                           "abc1234", False, "tests ok")
    checks.append(("no secrets leave the machine (scrubber on every comment)",
                   "sk-live-aabbccddeeff" not in probe
                   and "sk-live-aabbccddeeff" not in scrub_secrets(probe)))
    # 4. only the fields ever leave: names, ids, summaries — the comment
    # builders take lists of names and short strings, never file contents
    checks.append(("only file names, commit ids and summaries go to the hub",
                   "read_text" not in inspect.getsource(B.done_comment)
                   and "read_text" not in inspect.getsource(B.start_comment)))
    return checks
