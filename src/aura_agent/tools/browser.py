"""browser.py — a real browser, the cheap way.

Aura: - one browser per chat, kept across calls (logins and the page survive);
- a compact snapshot: interactive elements with short refs (e1, e2 …), headings
and visible text, built from the aria snapshot; actions use refs (found again
by role + name), not CSS; - after an action, only what changed (a diff of the
snapshot); - screenshots and full HTML go to the artifact store, not the chat;
- submitting (a submit/send/buy/pay/post/delete/sign-in button, Enter, page
script) asks first — autonomy.grade_of reads ref_submits below; - Playwright
stays an optional install (a graceful message without it).
"""
from __future__ import annotations

import atexit
import difflib
import json
import re
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from .artifacts import store_artifact
from ..tool_names import BROWSER_RUN

BROWSER_MISSING_MSG = (
    "browser extra not installed — run: pip install 'aura-agent[web]' "
    "&& playwright install chromium (air-gap: set PLAYWRIGHT_BROWSERS_PATH)")
INTERACTIVE = ("button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
               "menuitem", "tab", "option", "switch", "slider", "spinbutton", "listbox")
SUBMIT_WORDS = re.compile(
    r"\b(submit|send|buy|pay|purchase|order|checkout|check out|post|publish|delete|remove|"
    r"sign ?in|log ?in|sign ?up|register|confirm|subscribe|transfer|place)\b", re.I)
SNAPSHOT_CHARS = 4000
_LINE = re.compile(r'^\s*-\s+([a-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?(.*)$')


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class PlaywrightEngine:
    """The real engine (Playwright, sync API)."""

    def __init__(self, headless: bool = True):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # A window the user watches.
        self._browser = None
        self.channel = None
        try:
            from ..options import get as _opt
            order = (None,) if _opt("browser.engine") == "download" else ("msedge", "chrome", None)
        except Exception:                                # noqa: BLE001
            order = ("msedge", "chrome", None)
        for channel in order:
            try:
                self._browser = self._pw.chromium.launch(
                    headless=headless, slow_mo=0 if headless else 250,
                    **({"channel": channel} if channel else {}))
                self.channel = {"msedge": "Edge", "chrome": "Chrome"}.get(channel)
                break
            except Exception:                            # noqa: BLE001
                if channel is None:
                    raise
        self._context = self._browser.new_context()
        self.dialogs: list[str] = []          # What pop-up dialogs said
        self.opened_tab: int | None = None    # A click opened a new tab
        self.accept_next: str | None = None   # Set by op dialog (asks first)
        self._context.on("page", self._watch)
        self.page = self._context.new_page()

    def _watch(self, page):
        def on_dialog(d):
            said = f"{d.type}: {d.message[:200]}"
            if self.accept_next is not None:
                text, self.accept_next = self.accept_next, None
                d.accept(text) if text else d.accept()
                self.dialogs.append(said + " — accepted")
            else:
                d.dismiss()                     # Never confirmed silently
                self.dialogs.append(said + " — dismissed (to accept: op dialog, "
                                    "then repeat the action)")
        page.on("dialog", on_dialog)

    def goto(self, url: str, timeout_ms: int = 20000) -> int:
        r = self.page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return r.status if r else 0

    def aria(self) -> str:
        return self.page.locator("body").aria_snapshot()

    def _loc(self, role: str, name: str, nth: int):
        return self.page.get_by_role(role, name=name, exact=True).nth(nth) if name \
            else self.page.get_by_role(role).nth(nth)

    def click(self, role, name, nth):
        before = len(self._context.pages)
        self._loc(role, name, nth).click(timeout=5000)
        # A link that opens a new tab: switch to it
        self.page.wait_for_timeout(300)
        pages = self._context.pages
        if len(pages) > before:
            self.page = pages[-1]
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:                            # noqa: BLE001
                pass
            self.opened_tab = len(pages) - 1

    def fill(self, role, name, nth, value):
        self._loc(role, name, nth).fill(value, timeout=5000)

    def field_type(self, role, name, nth) -> str:
        try:
            return (self._loc(role, name, nth).get_attribute("type", timeout=2000) or "").lower()
        except Exception:                                # noqa: BLE001
            return ""

    def fill_login(self, user: str | None, password: str | None, code: str | None) -> list[str]:
        """Fill a sign-in form from the vault (site_vault.py): the visible
        password field, the user/email field, a one-time-code field."""
        done = []

        def put(css: str, value: str, what: str):
            loc = self.page.locator(css)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.fill(value, timeout=5000)
                    done.append(what)
                    return

        if user:
            put('input[autocomplete="username"], input[type="email"], input[name*="user" i], '
                'input[name*="login" i], input[name*="email" i], input[id*="user" i], '
                'input[id*="email" i], input[id*="login" i]', user, "user name")
        if password:
            put('input[type="password"]', password, "password")
        if code:
            put('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], '
                'input[name*="totp" i], input[name*="code" i]', code, "one-time code")
        return done

    def press(self, key: str):
        self.page.keyboard.press(key)

    def select(self, role, name, nth, value):
        loc = self._loc(role, name, nth)
        try:
            loc.select_option(label=value, timeout=5000)
        except Exception:                                # noqa: BLE001
            loc.select_option(value=value, timeout=5000)

    def check(self, role, name, nth, on: bool):
        self._loc(role, name, nth).set_checked(on, timeout=5000)

    def hover(self, role, name, nth):
        self._loc(role, name, nth).hover(timeout=5000)

    def upload(self, target, files: list[str]):
        loc = self._loc(*target) if target else self.page.locator("input[type=file]").first
        loc.set_input_files(files, timeout=5000)

    def wait(self, text: str | None, gone: bool, seconds: float):
        ms = int(min(30.0, max(0.1, seconds)) * 1000)
        if text:
            self.page.get_by_text(text).first.wait_for(state="hidden" if gone else "visible",
                                                       timeout=ms)
        else:
            self.page.wait_for_timeout(ms)

    def back(self):
        self.page.go_back(timeout=20000, wait_until="domcontentloaded")

    def tabs(self, action: str, index: int | None, url: str | None) -> str:
        pages = self._context.pages
        if action == "new":
            self.page = self._context.new_page()
            if url:
                self.page.goto(url, timeout=20000, wait_until="domcontentloaded")
        elif action in ("select", "close") and index is not None and 0 <= index < len(pages):
            if action == "close":
                pages[index].close()
                pages = self._context.pages
                self.page = pages[-1] if pages else self._context.new_page()
            else:
                self.page = pages[index]
                self.page.bring_to_front()
        pages = self._context.pages
        return "\n".join(f"{'*' if p is self.page else ' '} tab {i}: {p.url}"
                         for i, p in enumerate(pages))

    def evaluate(self, expression: str) -> str:
        return str(self.page.evaluate(expression))

    def screenshot(self) -> bytes:
        return self.page.screenshot(full_page=False)

    def html(self) -> str:
        return self.page.content()

    def url(self) -> str:
        return self.page.url

    def close(self):
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:                                # noqa: BLE001
            pass


ENGINE_FACTORY = None          # Tests put a fake engine here
_STATE: dict = {"engine": None, "refs": {}, "last": [], "submits": set()}


def _show() -> bool:
    """The user's choice to watch the browser (option browser.show)."""
    try:
        from ..options import get
        return bool(get("browser.show"))
    except Exception:                                    # noqa: BLE001
        return False


def _engine(headless: bool = True):
    if _STATE["engine"] is None:
        if ENGINE_FACTORY is not None:
            _STATE["engine"] = ENGINE_FACTORY()
        else:
            if not playwright_available():
                raise Violation(BROWSER_MISSING_MSG)
            _STATE["engine"] = PlaywrightEngine(headless=headless)
            atexit.register(close)
    return _STATE["engine"]


def close() -> None:
    eng = _STATE.get("engine")
    if eng is not None:
        eng.close()
    _STATE.update(engine=None, refs={}, last=[], submits=set(), vault=False)
    try:
        from .. import site_vault
        site_vault.forget_filled()
    except Exception:                                    # noqa: BLE001
        pass


def snapshot_lines(aria: str) -> tuple[list[str], dict, set]:
    """aria snapshot → compact lines, ref → (role, name, nth), submit refs."""
    lines, refs, submits, seen = [], {}, set(), {}
    n = 0
    for raw in aria.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        role, name, rest = m.group(1), (m.group(2) or "").replace('\\"', '"'), m.group(3)
        if role in INTERACTIVE:
            n += 1
            ref = f"e{n}"
            k = (role, name)
            refs[ref] = (role, name, seen.get(k, 0))
            seen[k] = seen.get(k, 0) + 1
            if role == "button" and SUBMIT_WORDS.search(name or ""):
                submits.add(ref)
            lines.append(f"{ref} {role} \"{name}\"".rstrip())
        elif role == "heading":
            lv = re.search(r"level=(\d)", rest)
            lines.append(f"{'#' * int(lv.group(1)) if lv else '#'} {name}")
        else:
            text = name or rest.lstrip(": ").strip()
            if text and role in ("text", "paragraph", "listitem", "cell", "img", "status", "alert"):
                lines.append(text[:200])
    return lines, refs, submits


def ref_submits(ref: str) -> bool:
    """For grading: does this ref submit something (asks first)?"""
    return ref in _STATE["submits"]


def _render(lines: list[str]) -> str:
    out = "\n".join(lines)
    if len(out) > SNAPSHOT_CHARS:
        out = out[:SNAPSHOT_CHARS] + f"\n… ({len(lines)} lines; the rest is in the page — op html saves it)"
    return out


def _diff(old: list[str], new: list[str]) -> str:
    """Only what changed; a near-total change (a new page) → the snapshot."""
    sm = difflib.SequenceMatcher(None, old, new)
    if sm.ratio() < 0.4:
        return "page changed:\n" + _render(new)
    ch = [f"+ {ln}" for op, _a1, _a2, b1, b2 in sm.get_opcodes() if op in ("insert", "replace")
          for ln in new[b1:b2]]
    ch += [f"- {ln}" for op, a1, a2, _b1, _b2 in sm.get_opcodes() if op in ("delete", "replace")
           for ln in old[a1:a2]]
    return ("changed:\n" + "\n".join(ch[:80])) if ch else "no visible change"


def _take(eng) -> list[str]:
    lines, refs, submits = snapshot_lines(eng.aria())
    _STATE.update(refs=refs, submits=submits)
    return lines


def _ref(act: dict):
    r = str(act.get("ref", "")).lstrip("@")
    if r not in _STATE["refs"]:
        raise Violation(f"unknown ref {r!r} — take a snapshot first (op snapshot)")
    return _STATE["refs"][r]


def _project_files(mediator, names) -> list[str]:
    """Upload only files inside the project (Playwright MCP accepts any
    path on the machine)."""
    root = Path(getattr(mediator, "work_dir", None) or mediator.root).resolve()
    out = []
    for n in names:
        p = (root / str(n)).resolve()
        if not str(n) or (root not in p.parents and p != root) or not p.is_file():
            raise Violation(f"upload: {n!r} is not a file inside the project")
        out.append(str(p))
    return out


def register(mediator: Mediator, dispatch: dict) -> None:
    def browser_run(args) -> str:
        actions = args.get("actions") or []
        if not actions:
            raise Violation("browser_run: actions required")
        if ENGINE_FACTORY is None and not playwright_available():
            return json.dumps({"status": "unavailable", "message": BROWSER_MISSING_MSG})
        eng = _engine(bool(args.get("headless", True)) and not _show())
        results = []
        for i, act in enumerate(actions):
            op = act.get("op")
            try:
                if op == "navigate":
                    status = eng.goto(act["url"], int(act.get("timeout_ms", 20000)))
                    new = _take(eng)
                    _STATE["last"] = new
                    results.append(f"[{i}] {act['url']} (HTTP {status})\n{_render(new)}")
                    continue
                if op == "snapshot":
                    new = _take(eng)
                    _STATE["last"] = new
                    results.append(f"[{i}] {eng.url()}\n{_render(new)}")
                    continue
                if op == "click":
                    if act.get("ref"):
                        eng.click(*_ref(act))
                    else:
                        raise Violation("click needs a ref from the snapshot (e3)")
                elif op == "fill":
                    # Passwords go through op login (the saved login), never
                    # through the model
                    if hasattr(eng, "field_type") and eng.field_type(*_ref(act)) == "password":
                        raise Violation("that is a password field — Aura fills it from your saved "
                                        "logins: op login (the user adds one with aura vault add <site>)")
                    eng.fill(*_ref(act), str(act.get("value", act.get("text", ""))))   # Playwright MCP says "text"
                elif op == "login":
                    from .. import site_vault as V
                    rec = V.entry_for(eng.url(), act.get("site"), act.get("user"))
                    if rec is None:
                        host = V.normal_site(eng.url())
                        results.append(f"[{i}] login: no saved login for this page's site ({host}) — "
                                       f"ask the user; they can add one with: aura vault add {host}")
                        continue
                    code = V.totp_now(rec["totp"]) if rec.get("totp") and act.get("code", True) else None
                    filled = eng.fill_login(rec.get("user"), rec.get("password"), code)
                    for v in (rec.get("password"), code):
                        V.remember_filled(v or "")
                    _STATE["vault"] = True
                    results.append(f"[{i}] login: filled {', '.join(filled) or 'nothing (no sign-in fields found)'} "
                                   f"from the saved login for {rec['site']} ({rec.get('user')}) — "
                                   "submit with click/press (asks first)")
                    continue
                elif op == "press":
                    eng.press(str(act.get("key", "Enter")))
                elif op == "select":
                    eng.select(*_ref(act), str(act.get("value", act.get("text", ""))))
                elif op in ("check", "uncheck"):
                    eng.check(*_ref(act), op == "check")
                elif op == "hover":
                    eng.hover(*_ref(act))
                elif op == "upload":
                    files = _project_files(mediator, act.get("files") or [act.get("value", "")])
                    eng.upload(_ref(act) if act.get("ref") else None, files)
                elif op == "wait":
                    eng.wait(act.get("text"), bool(act.get("gone")), float(act.get("seconds", 10)))
                elif op == "back":
                    eng.back()
                elif op == "tabs":
                    listing = eng.tabs(str(act.get("action", "list")), act.get("index"), act.get("url"))
                    new = _take(eng)
                    _STATE["last"] = new
                    results.append(f"[{i}] tabs:\n{listing}\n{_render(new)}")
                    continue
                elif op == "dialog":
                    # the NEXT dialog is accepted (with text for a prompt);
                    # dialogs are dismissed otherwise — this asks first
                    eng.accept_next = str(act.get("value") or "")
                    results.append(f"[{i}] the next dialog will be accepted")
                    continue
                elif op == "find":
                    q = str(act.get("text", "")).lower()
                    hits = [ln for ln in _STATE["last"] if q and q in ln.lower()]
                    results.append(f"[{i}] find {act.get('text')!r}: "
                                   + ("\n".join(hits[:30]) if hits else "not on this page"))
                    continue
                elif op == "eval":
                    if _STATE.get("vault"):
                        raise Violation("page scripts are off after a saved login was filled in this "
                                        "browser (they could read it back) — op close starts fresh")
                    results.append(f"[{i}] eval → {eng.evaluate(act['expression'])[:2000]}")
                    continue
                elif op == "screenshot":
                    art = store_artifact(Path(mediator.root), eng.screenshot(), f"shot-{i}.png")
                    results.append(f"[{i}] screenshot saved: {art['artifact_ref']}")
                    continue
                elif op == "html":
                    page_html = eng.html()
                    if _STATE.get("vault"):
                        # some frameworks copy a typed value into the page:
                        # a saved login must not reach a file the model reads
                        from .. import site_vault as V
                        page_html = V.scrub(page_html)
                    art = store_artifact(Path(mediator.root), page_html.encode(), f"page-{i}.html")
                    results.append(f"[{i}] page saved: {art['artifact_ref']} (read_file it)")
                    continue
                elif op == "close":
                    close()
                    results.append(f"[{i}] browser closed")
                    continue
                else:
                    raise Violation(f"browser_run: unknown op '{op}'")
                new = _take(eng)
                said = getattr(eng, "dialogs", None)
                note = ""
                if said:
                    note = "\n(a dialog appeared — " + "; ".join(said) + ")"
                    said.clear()
                if getattr(eng, "opened_tab", None) is not None:
                    note += f"\n(it opened a new tab — now in tab {eng.opened_tab}; op tabs lists them)"
                    eng.opened_tab = None
                results.append(f"[{i}] {op} {act.get('ref', act.get('key', ''))}: "
                               f"{_diff(_STATE['last'], new)}{note}")
                _STATE["last"] = new
            except Violation:
                raise
            except Exception as e:                       # noqa: BLE001
                results.append(f"[{i}] {op} failed: {str(e)[:200]}")
                break
        out = "\n".join(results)
        if _STATE.get("vault"):
            from .. import site_vault as V
            out = V.scrub(out)
        return out

    def _target(a):
        for act in a.get("actions") or []:
            if act.get("op") == "navigate" and act.get("url"):
                import urllib.parse
                return urllib.parse.urlparse(act["url"]).hostname
        return None

    mediator.register(ToolSpec(BROWSER_RUN, BlastRadius.EXECUTE, _target))
    dispatch[BROWSER_RUN] = browser_run
