"""pblocks.py — Persona inside Aura: the pblocks folder, each chat's panel,
and the hooks the run loop calls (the engine lives in pblock_engine/, the
session learner in engine/pblock_learn.py).

  - every pblock lives in one folder: <aura home>/pblocks;
  - the personal pblock is there from the start and learns how you work;
  - each chat remembers its panel (the pblocks it uses);
  - before a task the panel's cited precedents join the conversation as one
    message — Aura's own prompt never changes; nothing is added when no
    lesson applies;
  - every step and every exit is a chance to learn; new lessons are
    candidates until real outcomes confirm them.

Switches: Persona in /settings or /persona (default on), or AURA_PBLOCKS=0;
AURA_PBSTORES points at another pblocks folder.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

PACKAGES = ("numpy", "onnxruntime", "tokenizers")


PERSONAL = "personal"          # The blank pblock every user starts with


def missing_packages() -> list[str]:
    return [m for m in PACKAGES if importlib.util.find_spec(m) is None]


def stores_dir() -> Path:
    """THE pblocks folder: every pblock lives here — the personal one, trained
    ones, ones the user brings or downloads — so Aura always finds them in one
    place. AURA_PBSTORES points elsewhere.
    """
    env = os.environ.get("AURA_PBSTORES")
    if env:
        return Path(env)
    from .paths import aura_home
    return aura_home() / "pblocks"


# ------------------------------------------------------------ the library --
def all_pblocks() -> list[str]:
    """Every pblock in the folder (personal first)."""
    names = store_seats()
    return [PERSONAL] + [n for n in names if n != PERSONAL]


def purpose(name: str) -> str:
    """One line on what a pblock judges, from its template's territory."""
    if name == PERSONAL:
        return "how you work — learns your tasks and habits"
    y = stores_dir() / f"{name}.yml"
    try:
        import re
        txt = y.read_text(encoding="utf-8")
        m = re.search(r'territory:\s*"?(.+?)"?\s*(?:#.*)?$', txt, re.M)
        if m and m.group(1).strip().lower() not in ("null", "none", ""):
            return m.group(1).strip().strip('"')[:80]
    except OSError:
        pass
    return "no description"


def ensure_personal() -> None:
    """The blank personal pblock, made the first time Persona runs: an empty
    store plus its template (from the engine's blank seed)."""
    d = stores_dir()
    store = d / f"{PERSONAL}_store.jsonl"
    if store.exists():
        return
    d.mkdir(parents=True, exist_ok=True)
    store.touch()
    try:
        seed = (Path(__file__).parent / "pblock_engine" / "blank-pblock.yml").read_text(
            encoding="utf-8")
        seed = seed.replace("name: blank-pblock", f"name: {PERSONAL}")
        (d / f"{PERSONAL}.yml").write_text(seed, encoding="utf-8")
    except OSError:
        pass


def _panel_file(chat: str) -> Path:
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", chat or "default")[:120]
    return stores_dir() / "panels" / f"{safe}.json"


def _panel_state(chat: str) -> dict:
    import json
    try:
        d = json.loads(_panel_file(chat).read_text(encoding="utf-8"))
        return {"on": list(d.get("on", [])), "off": list(d.get("off", []))}
    except (OSError, ValueError):
        return {"on": [], "off": []}


def _save_panel(chat: str, st: dict) -> None:
    import json
    f = _panel_file(chat)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=1), encoding="utf-8")


def panel(chat: str) -> list[str]:
    """The pblocks in use for this chat (remembered per chat): the personal
    one unless switched off, plus the ones chosen for this chat."""
    have = set(store_seats()) | {PERSONAL}   # Personal exists from the start
    st = _panel_state(chat)
    out = [PERSONAL] if PERSONAL in have and PERSONAL not in st["off"] else []
    out += [n for n in st["on"] if n in have and n != PERSONAL and n not in st["off"]]
    return out


def chosen(chat: str) -> dict:
    """{name: on/off} for every pblock the chat has (personal included)."""
    st = _panel_state(chat)
    have = set(store_seats()) | {PERSONAL}   # Personal exists from the start
    out = {PERSONAL: PERSONAL not in st["off"]} if PERSONAL in have else {}
    for n in st["on"] + st["off"]:
        if n in have and n != PERSONAL:
            out[n] = n not in st["off"]
    return out


def switch(chat: str, name: str, on: bool) -> None:
    """Turn one pblock on or off for this chat."""
    st = _panel_state(chat)
    if on:
        st["off"] = [n for n in st["off"] if n != name]
        if name != PERSONAL and name not in st["on"]:
            st["on"].append(name)
    else:
        if name not in st["off"]:
            st["off"].append(name)
    _save_panel(chat, st)


def discard(chat: str, name: str) -> None:
    """Stop using a pblock in this chat; its files stay."""
    st = _panel_state(chat)
    st["on"] = [n for n in st["on"] if n != name]
    if name == PERSONAL and name not in st["off"]:
        st["off"].append(name)
    else:
        st["off"] = [n for n in st["off"] if n != name]
    _save_panel(chat, st)


def delete(name: str) -> list[str]:
    """Remove a pblock's files from the folder."""
    d = stores_dir()
    gone = []
    for f in list(d.glob(f"{name}_store.jsonl*")) + [d / f"{name}.yml",
                                                      d / f"{name}_heads.json", d / "weights" / f"{name}_heads.json",
                                                      d / f"{name}_embeddings.npy",
                                                      d / f"{name}_meta.jsonl"]:
        if f.exists() and f.is_file():
            f.unlink()
            gone.append(f.name)
    return gone


def add(src: str) -> list[str]:
    """Bring a pblock into the folder: a <name>_store.jsonl file, a folder
    holding one or more (with their .yml and _heads.json, in any sub-folder
    such as stores/ and weights/), or a .zip of such a folder. Returns the
    pblock names added."""
    import shutil
    import tempfile
    import zipfile
    p = Path(src).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"nothing at {p}")
    d = stores_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = None
    if p.is_file() and p.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="pblock-add-"))
        with zipfile.ZipFile(p) as z:
            for member in z.namelist():           # No paths outside the folder
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError(f"unsafe path in zip: {member}")
            z.extractall(tmp)
        p = tmp
    files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
    added = []
    for f in files:
        if f.name.endswith("_store.jsonl"):
            name = f.name[:-len("_store.jsonl")]
            shutil.copy2(f, d / f.name)
            added.append(name)
    names = {f.name for f in files}
    for f in files:                                 # Packed-only pblocks
        if f.name.endswith("_embeddings.npy"):
            name = f.name[:-len("_embeddings.npy")]
            if f"{name}_meta.jsonl" in names and name not in added:
                added.append(name)
                stub = d / f"{name}_store.jsonl"     # How Aura finds a pblock
                if not stub.exists():                # Old mtime = the packed pair wins
                    stub.touch()
                    os.utime(stub, (0, 0))
    for f in files:                                 # Templates, heads, packed pairs
        for name in added:
            if f.name == f"{name}_heads.json":          # The engine reads heads from weights/
                (d / "weights").mkdir(exist_ok=True)
                shutil.copy2(f, d / "weights" / f.name)
                continue
            if f.name in (f"{name}.yml",
                          f"{name}_embeddings.npy", f"{name}_meta.jsonl"):
                shutil.copy2(f, d / f.name)
            elif f.suffix == ".yml" and f.stem != name and len(added) == 1:
                shutil.copy2(f, d / f"{name}.yml")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return added


def pack(name: str, probes: int = 8) -> dict:
    """Write a pblock's packed form with the self-proof: the same probes must
    retrieve the same cases with the same scores (3 decimals) from both forms,
    or the packed files are removed and the JSONL store stays in use.
    """
    from .pblock_engine.store import PersonaStore
    import numpy as np
    d = str(stores_dir())
    src = PersonaStore(name, d)
    if getattr(src, "_packed", False):
        return {"ok": True, "note": "already packed"}
    probe_rows = [c for c in src.cases if c.get("embedding") is not None][:probes]

    def top(store, e):
        return [(c["id"], round(s, 3)) for c, s in store.retrieve(e, k=5)]

    before = [top(src, np.asarray(c["embedding"], dtype=np.float32)) for c in probe_rows]
    npy, meta = src.pack()
    packed = PersonaStore(name, d)
    after = [top(packed, np.asarray(c["embedding"], dtype=np.float32)) for c in probe_rows]
    if not packed._packed or before != after:
        for f in (npy, meta):
            try:
                os.remove(f)
            except OSError:
                pass
        return {"ok": False, "note": "packed results differed — kept the JSONL store"}
    jsonl = Path(d) / f"{name}_store.jsonl"
    if jsonl.exists():                       # Keep it as the old form, never newer
        os.utime(jsonl, (0, 0))
    return {"ok": True, "rows": len(packed.cases), "probes": len(probe_rows),
            "files": [os.path.basename(npy), os.path.basename(meta)]}


# ----------------------------------------------- what a pblock has learned --
def lessons(name: str, lived_only: bool = False) -> list[dict]:
    """A pblock's lessons in its own words (no model rewriting): what it
    learned, whether it worked, when, from which task, where it came from."""
    import time as _t
    from .pblock_engine.store import PersonaStore
    from .pblock_engine.quality import lesson_text
    st = PersonaStore(name, str(stores_dir()))
    out = []
    for c in st.cases:
        if lived_only and c.get("origin") == "seed":
            continue
        try:
            worked = float(str(c.get("outcome", "0")).split()[0]) >= 0.5
        except ValueError:
            worked = "ok" in str(c.get("outcome", "")).lower()
        ts = c.get("valid_from") or c.get("timestamp") or 0
        try:
            when = _t.strftime("%Y-%m-%d", _t.localtime(float(ts))) if ts else ""
        except (TypeError, ValueError, OverflowError):
            when = ""
        out.append({"id": c.get("id", ""), "text": lesson_text(c), "worked": worked,
                    "when": when, "task": c.get("task_summary", "") or "",
                    "source": c.get("origin_ref", "") or "",
                    "kind": {"seed": "trained", "lived": "learned", "user": "taught by you"}.get(
                        c.get("origin"), c.get("origin") or ""),
                    "status": c.get("status", "active")})
    return out


def recent_learned(name: str, n: int = 10) -> list[dict]:
    """The newest things a pblock learned while working (active ones)."""
    rows = [r for r in lessons(name, lived_only=True) if r["status"] == "active"]
    return sorted(rows, key=lambda r: r["when"], reverse=True)[:n]


def review_page(name: str) -> Path:
    """One local HTML page with every lesson of a pblock: search, filters,
    pages, tick lessons, a note on what you actually want, and a button that
    saves a retrain request (JSON) for `/persona apply`."""
    import html
    import json
    rows = lessons(name)
    # everything learned or taught is always on the page; a very large set of
    # TRAINED examples (Keel Guard: 115k) is trimmed so the page stays light
    MAX_TRAINED = 5000
    trained = [r for r in rows if r["kind"] == "trained"]
    if len(trained) > MAX_TRAINED:
        keep = set(id(r) for r in trained[:: max(1, len(trained) // MAX_TRAINED)][:MAX_TRAINED])
        rows = [r for r in rows if r["kind"] != "trained" or id(r) in keep]
    out = stores_dir() / "review" / f"{name}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(rows).replace("</", "<\\/")
    page = _REVIEW_HTML.replace("{{NAME}}", html.escape(name)).replace(
        "{{PURPOSE}}", html.escape(purpose(name))).replace("{{DATA}}", data)
    out.write_text(page, encoding="utf-8")
    return out


def apply_request(path: str) -> dict:
    """Do exactly what a review page's request says: retire the ticked
    lessons (kept as history, never used again) and keep the user's note as
    a confirmed lesson of that pblock."""
    import json
    from .pblock_engine.store import PersonaStore
    req = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    name = str(req.get("pblock") or "")
    if name not in all_pblocks():
        raise ValueError(f"no pblock named '{name}'")
    ids = set(str(i) for i in req.get("lessons") or [])
    note = str(req.get("note") or "").strip()
    st = PersonaStore(name, str(stores_dir()))
    retired = 0
    for c in st.cases:
        if c.get("id") in ids and c.get("status") != "retired":
            c["status"] = "retired"
            retired += 1
    added = 0
    if note:
        emb = None
        try:
            from .pblock_engine.ground import _orchestrator
            emb = _orchestrator(str(stores_dir())).embedder.embed(note)
        except Exception:                                # noqa: BLE001
            emb = None
        c = st.add_case("taught by the user while reviewing this pblock", note,
                        "1.0", confirmed=True, origin="user", embedding=emb,
                        persist=False)
        c["status"] = "active"
        added = 1
    st.save()
    return {"pblock": name, "retired": retired, "taught": added}


_REVIEW_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Persona review — {{NAME}}</title>
<style>
:root{--bg:#fbfaf7;--fg:#1f1f1f;--muted:#6b6b6b;--line:#e3e0d9;--ok:#2f7d4f;--bad:#b23a2a;--acc:#2a6f97}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#ececec;--muted:#9a9a9a;--line:#2e2e2e;--ok:#5fbf86;--bad:#e07a6b;--acc:#6fb0d6}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:22px;margin:0 0 4px} .muted{color:var(--muted)}
textarea{width:100%;box-sizing:border-box;min-height:70px;font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0}
input[type=search],select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit}
button{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--acc)}
.row{display:flex;gap:10px;padding:10px 4px;border-bottom:1px solid var(--line)}
.row .t{flex:1;min-width:0} .tag{font-size:12px;padding:1px 6px;border-radius:6px;border:1px solid var(--line)}
.ok{color:var(--ok)} .bad{color:var(--bad)} .small{font-size:13px}
.sticky{position:sticky;bottom:0;background:var(--bg);padding:12px 0;border-top:1px solid var(--line)}
</style></head><body><main>
<h1>{{NAME}}</h1><div class="muted">{{PURPOSE}}</div>
<p class="muted small">Tick the lessons you want changed. Write what you actually want instead. Then make the request and give it to Aura: <code>/persona apply &lt;the file&gt;</code>. Ticked lessons are retired (kept as history, never used again); your note becomes a lesson Aura keeps.</p>
<label for="note"><b>What do you actually want instead?</b></label>
<textarea id="note" placeholder="For example: I don't want tiny patches; fix the root cause even if the change is bigger."></textarea>
<div class="bar">
<input id="q" type="search" placeholder="search lessons" aria-label="search lessons">
<select id="f" aria-label="filter"><option value="all">all</option><option value="learned">learned while working</option><option value="trained">trained</option><option value="you">taught by you</option><option value="worked">worked</option><option value="failed">didn't work</option></select>
<label class="small"><input id="old" type="checkbox"> show retired</label>
<span id="count" class="muted small"></span></div>
<div id="list"></div>
<div class="bar"><button class="ghost" id="prev">‹ previous</button><span id="page" class="muted small"></span><button class="ghost" id="next">next ›</button></div>
<div class="sticky bar"><span id="picked" class="muted small">0 ticked</span><button id="make">Make retrain request</button></div>
</main><script>
const ROWS={{DATA}},NAME="{{NAME}}",PER=100;let page=0;const picked=new Set();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function shown(){const q=document.getElementById('q').value.toLowerCase(),f=document.getElementById('f').value,old=document.getElementById('old').checked;
return ROWS.filter(r=>(old||r.status!=="retired")&&(!q||(r.text+" "+r.task+" "+r.source).toLowerCase().includes(q))&&
(f==="all"||(f==="learned"&&r.kind==="learned")||(f==="trained"&&r.kind==="trained")||(f==="you"&&r.kind==="taught by you")||(f==="worked"&&r.worked)||(f==="failed"&&!r.worked)));}
function draw(){const s=shown(),pages=Math.max(1,Math.ceil(s.length/PER));page=Math.min(page,pages-1);
document.getElementById('count').textContent=s.length+" lessons";document.getElementById('page').textContent="page "+(page+1)+" of "+pages;
document.getElementById('list').innerHTML=s.slice(page*PER,(page+1)*PER).map(r=>`<label class="row"><input type="checkbox" data-id="${esc(r.id)}" ${picked.has(r.id)?"checked":""}><div class="t"><div>${esc(r.text)}</div><div class="small muted"><span class="${r.worked?"ok":"bad"}">${r.worked?"worked":"didn't work"}</span> · <span class="tag">${esc(r.kind)}</span>${r.when?" · "+esc(r.when):""}${r.task?" · task: "+esc(r.task):""}${r.source?" · source: "+esc(r.source):""}${r.status==="retired"?" · retired":""}</div></div></label>`).join("");
document.querySelectorAll('#list input').forEach(b=>b.onchange=()=>{b.checked?picked.add(b.dataset.id):picked.delete(b.dataset.id);document.getElementById('picked').textContent=picked.size+" ticked";});}
['q','f','old'].forEach(i=>document.getElementById(i).oninput=()=>{page=0;draw();});
document.getElementById('prev').onclick=()=>{page=Math.max(0,page-1);draw();};
document.getElementById('next').onclick=()=>{page++;draw();};
document.getElementById('make').onclick=()=>{const req={pblock:NAME,lessons:[...picked],note:document.getElementById('note').value.trim()};
if(!req.lessons.length&&!req.note){alert("Tick some lessons or write what you want first.");return;}
const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(req,null,1)],{type:"application/json"}));
a.download="persona-request-"+NAME+".json";a.click();
alert("Saved persona-request-"+NAME+".json (usually in Downloads). In Aura: /persona apply <that file>");};
draw();
</script></body></html>"""


def welcome_once() -> str:
    """The one message on first start."""
    flag = stores_dir() / ".welcomed"
    if flag.exists():
        return ""
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except OSError:
        return ""
    return ("Persona is on: Aura keeps a private record of how you work, on this "
            "computer only, and uses it to help you. Turn it off anytime with the "
            "Persona switch (/persona).")


def enabled() -> bool:
    """On unless the user turned it off (setting or AURA_PBLOCKS=0)."""
    if os.environ.get("AURA_PBLOCKS", "1") == "0":
        return False
    try:
        from . import options
        return bool(options.get("pblocks.on"))
    except Exception:                                   # noqa: BLE001
        return True


def available() -> bool:
    return enabled() and not missing_packages()


def needs_line() -> str:
    """For the settings screen: what pblocks has and what it still needs."""
    miss = missing_packages()
    if miss:
        return ("needs " + ", ".join(miss) + " (part of Aura's install — "
                "reinstall Aura to add them)")
    n = len(all_pblocks())
    return f"{n} pblock(s) in {stores_dir()} · /persona to choose"


def ground_seats(ground_text: str) -> list[tuple[str, int]]:
    """Which seats spoke in a compiled briefing, with how many lines each —
    for the sidebar's panel line. Reads the briefing's own section headers
    ("### GROUND FROM SWE_LOCALIZE PERSONA" / "[seat] ..." lines)."""
    import re
    out: list[tuple[str, int]] = []
    seat, n = None, 0
    for ln in (ground_text or "").splitlines():
        m = re.match(r"###\s+GROUND FROM\s+(.+?)\s+PERSONA", ln)
        if m or ln.startswith("###"):
            if seat and n:
                out.append((seat, n))
            seat, n = (m.group(1).lower() if m else None), 0
        elif seat and ln.strip().startswith("- "):
            n += 1
    if seat and n:
        out.append((seat, n))
    if not out:                                  # The flat "simple" profile
        counts: dict = {}
        for ln in (ground_text or "").splitlines():
            m = re.match(r"\[([A-Za-z0-9_]+)\]", ln.strip())
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        out = list(counts.items())
    return out


def store_seats() -> list[str]:
    """The seat stores this Aura has (names only — cheap, for the sidebar)."""
    d = stores_dir()
    if not d.is_dir():
        return []
    return sorted(p.name[:-len("_store.jsonl")] for p in d.glob("*_store.jsonl"))


# Aura's own refusals and notes: faults of Aura, never lessons about the work —
# its safe box, permission cards, folder guard, plan / discuss mode, loop
# guard, stop flag and its own notes to the model
AURA_FAULTS = (
    r"can't be used —", r"isn't available", r"\bplan mode\b", r"discuss mode",
    r"not allowed in plan mode", r"wasn't allowed", r"sandbox\.required",
    r"blocked by the loop guard", r"the safe box blocked it", r"awaiting_approval",
    r"operator (declined|expired)", r"banned command token", r"kill switch",
    r"\[AURA NOTE\]", r"\[LOOP GUARD\]", r"Violation", r"refused: the user",
    r"user said no", r"nobody is here to", r"needs operator approval", r"RECOMMEND: OPTION",
)


def _point_env() -> None:
    """The organs read AURA_PBSTORES; point them at Aura's one place."""
    os.environ.setdefault("AURA_PBSTORES", str(stores_dir()))
    try:
        from .pblock_engine.pblock_learn import set_host_faults
        set_host_faults(AURA_FAULTS)
    except Exception:                                   # noqa: BLE001
        pass


def ground(task: str, root: str, instance_id: str = "", chat: str = "") -> str:
    """The cited precedents of THIS chat's panel for this task, or "" (never
    raises). The panel (personal + the chat's chosen pblocks) is what the
    organs may use and learn into, through AURA_PBSEATS."""
    from .pblock_engine._log import dbg
    if not available() or not (task or "").strip():
        dbg("[pblocks] ground skipped: available=%s, task=%d chars"
            % (available(), len(task or "")))
        return ""
    try:
        _point_env()
        ensure_personal()
        use = panel(chat) if not os.environ.get("AURA_PBSEATS_FIXED") else \
            [x for x in os.environ.get("AURA_PBSEATS", "").split(",") if x]
        if not use:
            return ""
        os.environ["AURA_PBSEATS"] = ",".join(use)
        from .pblock_engine import ground as G
        orch = G._orchestrator(str(stores_dir()))
        for n in list(orch.personas):              # Only this chat's panel
            if n not in use:
                del orch.personas[n]
        text = G.compile_run_ground(task, root, instance_id=instance_id) or ""
        # a briefing with no lessons in it is noise in front of every task
        text = text if ground_seats(text) else ""
        # the person's habits that fit this task, on their own (habits.py)
        how = habits_brief(task, chat)
        return (text + ("\n\n" if text else "") + how) if how else text
    except Exception as e:                              # noqa: BLE001
        dbg(f"[pblocks] ground failed: {type(e).__name__}: {e}")
        return ""


def learn_step(session, work_dir, step: int, task: str) -> int:
    """Per-step learning: this
    step's model action + the world's responses = a lesson pair."""
    if not available() or session is None:
        return 0
    try:
        _point_env()
        if not stores_dir().is_dir():
            return 0
        from .engine.pblock_learn import learn_step_pairs
        return int(learn_step_pairs(session, str(work_dir), step, task or "") or 0)
    except Exception:                                   # noqa: BLE001
        return 0


def close_run(answer: str, task: str, ok: bool, instance_id: str,
              on_event) -> None:
    """Run-end loop closure: the
    output audit on the answer, then verdict linking (diary joins, weights,
    scars on failure). Loads only the scoped seats (AURA_PBSEATS) — loading
    every store at every exit costs minutes + GB (measured)."""
    if not available():
        return
    try:
        from .pblock_engine.ground import _CACHE
        root = _CACHE.get("root")
        if not root or not os.path.isdir(root):
            return
        # The run's one loaded orchestrator; the learners write through these
        # same store objects, so it is never stale
        from .pblock_engine.ground import _orchestrator
        orch = _orchestrator(root)
        scope = [x.strip() for x in os.environ.get("AURA_PBSEATS", "").split(",")
                 if x.strip()]
        for f in os.listdir(root):
            if not f.endswith("_store.jsonl"):
                continue
            n = f[:-len("_store.jsonl")]
            if scope and n not in scope:
                continue
            if n not in orch.personas:
                try:
                    orch.add_persona(n)
                except Exception:                       # noqa: BLE001
                    pass
        from .pblock_engine.output_audit import OutputAuditor
        aud = OutputAuditor(orch)
        av = aud.audit(answer or "", (task or "")[:120])
        fg = aud.final_gate(av)
        on_event("pblock_output_audit", {"final_gate": str(fg)[:160]})
        from .pblock_engine import verdict_link as vl
        iid = instance_id or f"run-{os.getpid()}"
        joined = vl.join(orch.diary, {iid: ok})
        tallies = vl.tally(joined)
        if tallies:
            failed_emb = {}
            if not ok:
                try:
                    failed_emb = {iid: orch.embedder.embed(task or "")}
                except Exception:                       # noqa: BLE001
                    failed_emb = {}
            for p in orch.personas.values():
                vl.apply_weights(p.store, tallies)
                if not ok:
                    vl.apply_scars(p.store, tallies, failed_emb)
        if joined:
            on_event("pblock_verdict_link", {"joined": len(joined)})
    except Exception as e:                              # noqa: BLE001
        # the closure never breaks the run — but the error is never silent
        on_event("pblock_verdict_error", {"error": str(e)[:160]})


def clean_stores(root: str | None = None) -> dict:
    """Retire step-noise rows in every seat store (kept as history, never
    deleted, never retrieved; seed rows untouched). Snapshot first, so the
    cleanup can be rolled back. Returns {seat: rows retired}."""
    if missing_packages():
        return {}
    d = Path(root) if root else stores_dir()
    out: dict = {}
    if not d.is_dir():
        return out
    from .pblock_engine.orchestrator import Orchestrator
    from .pblock_engine.quality import retire_fragments
    orch = Orchestrator(root=str(d))
    for f in sorted(d.glob("*_store.jsonl")):
        seat = f.name[:-len("_store.jsonl")]
        if seat not in orch.personas:
            orch.add_persona(seat)
        store = orch.personas[seat].store
        try:
            store.snapshot("before-clean")
        except Exception:                               # noqa: BLE001
            pass
        out[seat] = retire_fragments(store)
    return out


def learn(session, work_dir, rec, task: str) -> int:
    """End-of-run lessons as candidates (never raises; 0 when unavailable)."""
    if not available() or session is None:
        return 0
    try:
        _point_env()
        stores_dir().mkdir(parents=True, exist_ok=True)
        from .engine.pblock_learn import learn_from_session
        return int(learn_from_session(session, str(work_dir), rec, task or "") or 0)
    except Exception:                                   # noqa: BLE001
        return 0


# ------------------------------------------------- the personal pblock's habits --
# variants/personal.md: the personal pblock learns the person's habits — only
# from their own typed words and their card answers (never pasted text, tool
# output, pages or files). Direct words are trusted at once; a correction is
# worded by one short model call and waits for another task; the same card
# answer three times is a habit; "noted: … · undo" is said.
PASTED: list = []              # What the person pasted into the current message
CURRENT_TASK = {"text": ""}    # The task being worked on (card answers carry it)
_PHRASE = (
    "You turn a person's correction of an AI agent into ONE habit: how they want "
    "the agent to work (style, process, what to ask or check first, what to avoid). "
    "Reply with one short sentence in the third person that starts with \"Wants\", "
    "\"Prefers\" or \"Doesn't want\". If the message is only about this task's "
    "content (a bug, a value, a file, a fact) and says nothing about how to work, "
    "reply NONE.")


def _typed_only(text: str) -> str:
    """The person's own typed words: anything they pasted is taken out."""
    out = str(text or "")
    for chunk in PASTED:
        if chunk and len(chunk) >= 12:
            out = out.replace(chunk, " ")
    return out


def note_pasted(chunks) -> None:
    PASTED[:] = [str(c) for c in (chunks or []) if c]


def habits_brief(task: str, chat: str = "") -> str:
    """The person's habits that fit this task, or ""."""
    if not available():
        return ""
    try:
        _point_env()
        ensure_personal()
        from .pblock_engine.habits import brief
        return brief(str(stores_dir()), task, chat)
    except Exception:                                   # noqa: BLE001
        return ""


def learn_habits(session, task: str, chat: str, llm=None, on_event=None) -> int:
    """After a turn: the person's last typed message, with the agent's answer
    before it, may teach a habit. Says "noted: … · undo" for each."""
    if not available() or session is None:
        return 0
    try:
        from .notes import is_users_words, users_words
        msgs = [m for m in (getattr(session, "messages", None) or []) if isinstance(m, dict)]
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if is_users_words(msgs[i])), None)
        if idx is None:
            return 0
        said = _typed_only(users_words(msgs[idx]))
        prev = next((str(m.get("content") or "") for m in reversed(msgs[:idx])
                     if m.get("role") == "assistant" and str(m.get("content") or "").strip()), "")
        _point_env()
        ensure_personal()
        from .pblock_engine import habits as H

        def phrase(answer: str, text: str) -> str:
            if llm is None:
                return ""
            try:
                res = llm.chat([{"role": "system", "content": _PHRASE},
                                {"role": "user", "content": (
                                    "The agent's last answer (start):\n" + answer[:900]
                                    + "\n\nThe person replied:\n" + text[:600])}],
                               tools=None, temperature=0, side=True, max_tokens=60)
                return str(getattr(res, "content", "") or "").strip().splitlines()[0][:200]
            except Exception:                           # noqa: BLE001
                return ""
        if H.kind_of(said) == "praise":
            H.praise(str(stores_dir()), chat)
            return 0
        got = H.learn_message(str(stores_dir()), said, prev, task, phrase=phrase)
        for g in got:
            if on_event is not None and g.get("new"):
                on_event("notice", {"calm": True, "text": (
                    f"noted: {g['habit']}" + ("" if g.get("trusted") else
                                              " (until it shows up again)")
                    + (f" — replaces: {g['replaced'][0]}" if g.get("replaced") else "")
                    + f" · undo: /persona forget {g['id']}")})
        return len(got)
    except Exception:                                   # noqa: BLE001
        return 0


def record_choice(question: str, choice: str, on_event=None) -> None:
    """The person's answer on a card (asked at least the third time in
    different tasks → a habit)."""
    if not available() or not question or not choice:
        return
    try:
        _point_env()
        ensure_personal()
        from .pblock_engine.habits import record_choice as _rc
        g = _rc(str(stores_dir()), question, choice, CURRENT_TASK.get("text") or "")
        if g and on_event is not None:
            on_event("notice", {"calm": True, "text": (
                f"noted: you {g['habit']} · undo: /persona forget {g['id']}")})
    except Exception:                                   # noqa: BLE001
        pass


def forget_habit(case_id: str) -> bool:
    try:
        from .pblock_engine.habits import forget
        return forget(str(stores_dir()), case_id)
    except Exception:                                   # noqa: BLE001
        return False
