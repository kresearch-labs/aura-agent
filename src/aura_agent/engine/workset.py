"""workset.py — trim old tool outputs by use, in batches.

Measured before: trimming by age, one more output every step once 50% of the
window was reached, changed an early message on every call — 22% of what was
sent could come from a server's cache. Big windows never trimmed at all (1.19M
chars for a 16-call task).

  - trimming starts at a fixed budget (min(prune % of window, 64k tokens))
    and goes down to 60% of it in ONE batch — the stubs are sticky, so the
    start of the conversation stays the same until the next batch;
  - first trimmed: outputs superseded (the file was edited or read again,
    the same command / page / search ran again); then outputs unused for a
    while; age last. Never: the newest outputs, hot ones (used again
    lately or reopened), errors not yet superseded, small ones;
  - every stub says why and how to get the output back in one step
    (read_file the file, or the saved copy) — nothing is lost.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

WORK_BUDGET = 64_000          # Tokens: trimming starts by here whatever the window
LOW_FRAC = 0.60               # A batch trims down to 60% of the trigger
KEEP_NEWEST = 3               # The newest outputs are never trimmed
UNUSED_STEPS = 6              # Not used for this many steps → may go
MIN_CHARS = 800               # Smaller than this isn't worth a stub
TAG = "[TRIMMED]"

_FILE_READERS = ("read_file",)
_FILE_WRITERS = ("write_file", "edit_file")


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def resource(name: str, args: dict) -> tuple:
    """What a tool call is about — outputs about the same thing supersede
    each other."""
    if name in _FILE_READERS or name in _FILE_WRITERS:
        return ("file", str(args.get("path", "")).replace("\\", "/").lstrip("./"))
    if name in ("run_command", "process_start"):
        return ("cmd", " ".join(str(args.get("cmd", "")).split()))
    if name == "web_crawl":
        return ("url", str(args.get("url", "")).rstrip("/"))
    if name in ("search_files", "web_search", "history_search"):
        return (name, json.dumps(args, sort_keys=True))
    if name == "list_files":
        return ("list", str(args.get("path", ".")))
    return ("other", name, json.dumps(args, sort_keys=True)[:200])


def _is_error(text: str) -> bool:
    head = text[:300]
    return ('"status": "error"' in head or '"ok": false' in head
            or head.startswith("error") or '"exit_code": 1' in head
            or ('"exit_code": ' in head and '"exit_code": 0' not in head))


@dataclass
class Out:
    idx: int                 # Position in the message list
    key: str                 # Sticky identity (position + content)
    name: str
    args: dict
    res: tuple
    step: int                # Steps (assistant turns) before it
    chars: int
    error: bool
    superseded: str = ""     # Why a newer one makes it stale
    last_use: int = 0


@dataclass
class WorkSet:
    trimmed: dict = field(default_factory=dict)      # Key -> stub text
    why: dict = field(default_factory=dict)          # Key -> (label, why, reopen, chars, step)
    reopened: dict = field(default_factory=dict)     # Resource -> times
    hot_until: dict = field(default_factory=dict)    # Resource -> step
    counted: set = field(default_factory=set)        # Outputs counted as reopens
    stuck_at: int = 0
    batches: int = 0

    def thrashing(self) -> list[str]:
        """Things fetched back twice after a trim: read only what's needed."""
        return [r[1] for r, n in self.reopened.items() if n >= 2 and r[0] == "file"]

    # -------------------------------------------------------------- scan --
    @staticmethod
    def scan(messages: list[dict]) -> list[Out]:
        calls: dict[str, tuple] = {}
        outs: list[Out] = []
        step = 0
        uses: dict[tuple, int] = {}
        events: list[tuple] = []                     # (step, res, name, idx)
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == "assistant":
                step += 1
                for tc in m.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    a = _args(fn.get("arguments"))
                    nm = fn.get("name", "")
                    calls[tc.get("id", "")] = (nm, a)
                    r = resource(nm, a)
                    uses[r] = step
                    events.append((step, r, nm, i))
            elif role == "tool":
                nm, a = calls.get(m.get("tool_call_id", ""), (m.get("name", "?"), {}))
                body = str(m.get("content", "") or "")
                key = f"{i}:{hashlib.sha1(body[:2000].encode('utf-8', 'replace')).hexdigest()[:10]}"
                outs.append(Out(i, key, nm, a, resource(nm, a), step, len(body), _is_error(body)))
        for o in outs:
            later = [e for e in events if e[0] > o.step and e[1] == o.res]
            o.last_use = max([o.step] + [e[0] for e in later])
            for st, _r, nm, _i in later:
                if o.res[0] == "file":
                    if nm in _FILE_WRITERS:
                        o.superseded = f"the file was changed at step {st}"
                        break
                    if nm in _FILE_READERS:
                        o.superseded = f"read again at step {st}"
                        break
                elif o.res[0] in ("cmd", "url", "list") or o.res[0] in (
                        "search_files", "web_search", "history_search"):
                    o.superseded = f"ran again at step {st}"
                    break
        return outs

    # ----------------------------------------------------------- reopen --
    def _reopen(self, o: Out, body: str, store: Path | None) -> str:
        if o.res[0] == "file" and o.name in _FILE_READERS and o.res[1]:
            changed = " (it changed since)" if "changed" in o.superseded else ""
            return f"read_file {o.res[1]}{changed}"
        try:                                         # The output already saved?
            d = json.loads(body)
            if isinstance(d, dict) and d.get("artifact_ref"):
                return f"read_file {d['artifact_ref']}"
        except (ValueError, TypeError):
            pass
        if store is not None:
            from ..tools.artifacts import store_artifact
            ref = store_artifact(store, body.encode("utf-8", "replace"),
                                 f"{o.name or 'tool'}.txt")["artifact_ref"]
            return f"read_file {ref}"
        return "run it again"

    @staticmethod
    def _label(o: Out) -> str:
        if o.res[0] in ("file", "url", "list"):
            return f"{o.name} {o.res[1]}"
        if o.res[0] == "cmd":
            return f"{o.name} `{o.res[1][:80]}`"
        return f"{o.name} {json.dumps(o.args, sort_keys=True)[:80]}"

    # ------------------------------------------------------------ apply --
    def apply(self, messages: list[dict], est, trigger: int,
              store: Path | None = None) -> tuple[list[dict], dict]:
        """messages with the sticky stubs in place, plus a new batch when
        over `trigger` tokens. est(messages) -> tokens."""
        outs = self.scan(messages)
        step_now = max([o.step for o in outs] + [0])
        # A chat rewritten under us (compaction, restore).
        by_idx = {o.idx: o.key for o in outs}
        if any(by_idx.get(int(k.split(":")[0])) != k for k in self.trimmed):
            self.__init__()
        # an output trimmed, then fetched again → hot for a while, so it
        # isn't trimmed straight back (anti-thrash); twice → read less
        trim_step = {}
        for k in self.trimmed:
            r, st = self.why[k][5], self.why[k][4]
            trim_step[r] = min(trim_step.get(r, st), st)
        for o in outs:
            if o.key in self.trimmed or o.key in self.counted or o.res not in trim_step \
                    or o.step <= trim_step[o.res]:
                continue
            self.counted.add(o.key)
            self.reopened[o.res] = self.reopened.get(o.res, 0) + 1
            self.hot_until[o.res] = o.step + 2 * UNUSED_STEPS
        out = self._stubbed(messages, outs)
        report = {"batch": 0, "trimmed": 0, "freed": 0}
        now = est(out)
        if now <= trigger or (self.stuck_at and now < self.stuck_at + trigger // 10):
            return out, report
        target = int(trigger * LOW_FRAC)
        cpt = max(1.0, sum(len(str(m.get("content", ""))) for m in out) / max(1, now))
        # the newest outputs are kept — but in a small window they alone can
        # fill the budget, and then every step trims one (measured on 32k):
        # keep them only while they take at most 40% of the target
        newest, kept_chars = set(), 0
        for o in reversed(outs):
            if len(newest) >= KEEP_NEWEST or (newest and kept_chars + o.chars
                                              > 0.4 * target * cpt):
                break
            newest.add(o.key)
            kept_chars += o.chars

        def rank(o: Out):
            if o.superseded:
                return (0, o.step)
            if step_now - o.last_use >= UNUSED_STEPS:
                return (1, o.last_use)
            return (2, o.step)
        cands = sorted((o for o in outs if o.key not in self.trimmed
                        and o.key not in newest and o.chars >= MIN_CHARS
                        and not (o.error and not o.superseded)
                        and self.hot_until.get(o.res, -1) < step_now),
                       key=rank)
        freed_chars = 0
        for o in cands:
            left = now - int(freed_chars / cpt)
            if left <= target:
                break
            # Outputs trimmed only for their age may still be in use — those go
            # only down to halfway between the trigger and the low mark (to the
            # trigger only, it trimmed one output every step again: 26% cache
            # reuse, measured)
            if rank(o)[0] == 2 and left <= (trigger + target) // 2:
                break
            body = str(messages[o.idx].get("content", ""))
            why = o.superseded or (f"not used since step {o.last_use}"
                                   if step_now - o.last_use >= UNUSED_STEPS
                                   else f"from step {o.step}")
            how = self._reopen(o, body, store)
            stub = (f"{TAG} {self._label(o)} · {o.chars:,} chars · {why} · "
                    f"reopen: {how}")
            freed_chars += o.chars - len(stub)
            self.trimmed[o.key] = stub
            self.why[o.key] = (self._label(o), why, how, o.chars, step_now, o.res)
            report["trimmed"] += 1
        if report["trimmed"]:
            self.batches += 1
            report["batch"] = self.batches
            report["freed"] = freed_chars
            out = self._stubbed(messages, outs)
        after = est(out)
        self.stuck_at = after if after > trigger else 0
        return out, report

    def _stubbed(self, messages: list[dict], outs: list[Out]) -> list[dict]:
        keys = {o.idx: o.key for o in outs}
        res = []
        for i, m in enumerate(messages):
            k = keys.get(i)
            res.append({**m, "content": self.trimmed[k]} if k in self.trimmed else m)
        return res

    # ----------------------------------------------------------- report --
    def listing(self) -> list[str]:
        if not self.trimmed:
            return ["nothing trimmed"]
        out = [f"{len(self.trimmed)} outputs trimmed in {self.batches} batches "
               "(the start of the conversation changes only at a batch):"]
        for k in self.trimmed:
            label, why, how, chars, _st, _res = self.why[k]
            out.append(f"  {label[:70]} · {chars:,} chars · {why} · reopen: {how}")
        return out
