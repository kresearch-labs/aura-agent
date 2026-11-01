"""gateway_approvals.py — answer an approval from your phone.

When a run nobody is watching stops for an approval (approvals.
waiting_approver), the question also goes to the channels set in `aura setup
away`. On Telegram it comes with two buttons — Approve once / Reject — and a
reply `approve <id>` / `reject <id> <reason>` works too (a bare "yes" / "no"
only while exactly one question is open). The answer goes the same way `aura
approve|reject <id>` does (an answer file the waiting run reads), so the run's
own checks still apply: exactly once, expired is a no, and the call that runs
must be the one approved. Discord, Slack and plain webhooks only send, so their
message says how to answer on the computer.

Telegram's Bot API lets a bot read replies by polling getUpdates (no server to
open to the internet); inline buttons carry up to 64 bytes of data, enough for
the approval id; the buttons are removed once answered so a second tap can't
happen. Only the chat set up in `aura setup away` is listened to; anything else
is ignored. One listener per bot on this computer (a lock file): Telegram
allows one getUpdates reader at a time. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .secrets_guard import sanitize_output

SEND_ONLY = ("discord", "slack", "webhook")


def channels() -> dict:
    """The channels set up in `aura setup away` (profile + environment)."""
    try:
        from .gateway import _config
        cfg = _config(None)
    except Exception:                                    # noqa: BLE001
        return {}
    return {c: cfg[c] for c in ("telegram", *SEND_ONLY) if cfg.get(c)}


def approval_push_text(request: dict, can_tap: bool) -> str:
    """What the phone shows: what, where, how serious, until when, how to
    answer. Secret-shaped values are scrubbed before it leaves."""
    left = max(0, int(float(request.get("expires_ts") or 0) - time.time()))
    rid = request.get("id", "?")
    lines = [f"Aura is waiting for your OK — {request.get('grade', '?')}",
             f"{request.get('tool', '?')} → {request.get('target', '?')}"]
    if request.get("why"):
        lines.append(f"why: {str(request['why'])[:200]}")
    args = request.get("args")
    if args:
        lines.append("call: " + json.dumps(args, ensure_ascii=False)[:600])
    if left and left < 30 * 86400:
        lines.append(f"open for {left // 60} min")
    lines.append("Tap a button, or reply" if can_tap else "Answer on the computer:")
    lines.append(f"approve {rid}   ·   reject {rid} <reason>" if can_tap else
                 f"aura approve {rid}   ·   aura reject {rid} <reason>")
    return sanitize_output("\n".join(lines))


def _tg(token: str, method: str, payload: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r) or {}


def push(eng, request: dict) -> list[str]:
    """Send the question to every channel set up. Lines saying where it
    went (or why not). Never raises."""
    out = []
    for ch, cfg in channels().items():
        try:
            if ch == "telegram":
                if not (cfg.get("token") and cfg.get("chat_id")):
                    out.append("telegram: no chat set — run `aura setup away`")
                    continue
                rid = request.get("id", "")
                r = _tg(cfg["token"], "sendMessage", {
                    "chat_id": cfg["chat_id"],
                    "text": approval_push_text(request, True)[:4000],
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "Approve once", "callback_data": f"a:{rid}"[:64]},
                        {"text": "Reject", "callback_data": f"r:{rid}"[:64]}]]}})
                _sent[rid] = (str(cfg["chat_id"]), (r.get("result") or {}).get("message_id"))
                out.append("sent to telegram (tap to answer)")
            else:
                from .gateway import deliver_message
                res = deliver_message(approval_push_text(request, False), ch, eng=Path(eng))
                out.append(f"sent to {ch}" if res.get("ok") else
                           f"{ch}: not sent ({str(res.get('detail', ''))[:80]})")
        except Exception as e:                           # noqa: BLE001
            out.append(f"{ch}: not sent ({str(e)[:80]})")
    return out


_sent: dict = {}          # Approval id -> (chat id, message id) on Telegram


def parse_reply(text: str, open_ids: list[str]) -> tuple[str, bool, str] | None:
    """A chat reply → (approval id, approve?, reason) or None."""
    t = (text or "").strip()
    m = re.match(r"^/?(approve|reject|deny|yes|no|y|n)\b\s*([\w-]*)\s*(.*)$", t, re.I | re.S)
    if not m:
        return None
    word, rid, rest = m.group(1).lower(), m.group(2), m.group(3).strip()
    approve = word in ("approve", "yes", "y")
    if rid and rid in open_ids:
        return rid, approve, rest[:300]
    if word in ("yes", "no", "y", "n") or not rid:
        if len(open_ids) == 1:                           # Unambiguous only
            return open_ids[0], approve, ((rid + " " + rest).strip() if rid else rest)[:300]
        return None
    return None


def _lock_path() -> Path:
    from .paths import aura_home
    return aura_home() / "telegram-listener.lock"


def _take_lock() -> bool:
    p = _lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid():
        try:
            from .tools.process import _alive
            if _alive(pid):
                return False                             # Another Aura listens
        except Exception:                                # noqa: BLE001
            return False
    p.write_text(str(os.getpid()))
    return True


def _offset_path() -> Path:
    from .paths import aura_home
    return aura_home() / "telegram-offset"


def open_ids(own: list[str]) -> list[str]:
    """Every approval still open in this profile (other waiting runs too:
    one listener answers for all of them), this run's first."""
    ids = list(own)
    try:
        from .paths import no_project_dir, profile_dir
        nbs = sorted((profile_dir() / "projects").glob("*")) + [no_project_dir()]
        for nb in nbs:
            for f in sorted((nb / "pending").glob("*.json")):
                if f.stem not in ids:
                    ids.append(f.stem)
    except Exception:                                    # noqa: BLE001
        pass
    return ids


class Listener:
    """Reads the Telegram chat for answers while approvals are open."""

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, str(chat_id)
        self.open: list[str] = []
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def add(self, rid: str) -> None:
        if rid not in self.open:
            self.open.append(rid)
        if self.thread is None or not self.thread.is_alive():
            self.stop.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def done(self, rid: str) -> None:
        if rid in self.open:
            self.open.remove(rid)
        if not self.open:
            self.stop.set()

    def handle(self, upd: dict) -> str | None:
        """One update → the approval it answered, or None."""
        from .approvals import answer
        cb = upd.get("callback_query")
        if cb:
            chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
            data = str(cb.get("data") or "")
            if chat != self.chat_id or ":" not in data:
                return None
            kind, rid = data.split(":", 1)
            ok = rid in open_ids(self.open)
            if ok:
                answer(rid, kind == "a", "rejected from Telegram" if kind != "a" else "")
            try:
                _tg(self.token, "answerCallbackQuery", {
                    "callback_query_id": cb.get("id"),
                    "text": ("approved" if kind == "a" else "rejected") if ok
                    else "this question is closed"})
                msg = cb.get("message") or {}
                _tg(self.token, "editMessageReplyMarkup", {
                    "chat_id": chat, "message_id": msg.get("message_id"),
                    "reply_markup": {"inline_keyboard": []}})
            except Exception:                            # noqa: BLE001
                pass
            return rid if ok else None
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != self.chat_id:
            return None
        got = parse_reply(msg.get("text", ""), open_ids(self.open))
        if not got:
            return None
        rid, approve, reason = got
        answer(rid, approve, reason)
        try:
            _tg(self.token, "sendMessage", {
                "chat_id": self.chat_id,
                "text": f"{'approved' if approve else 'rejected'} {rid}"})
            sent = _sent.get(rid)
            if sent and sent[1]:
                _tg(self.token, "editMessageReplyMarkup", {
                    "chat_id": sent[0], "message_id": sent[1],
                    "reply_markup": {"inline_keyboard": []}})
        except Exception:                                # noqa: BLE001
            pass
        return rid

    def _run(self) -> None:
        if not _take_lock():
            return
        try:
            try:
                offset = int(_offset_path().read_text().strip() or 0)
            except (OSError, ValueError):
                offset = 0
            while not self.stop.is_set():
                try:
                    r = _tg(self.token, "getUpdates", {
                        "offset": offset, "timeout": 20,
                        "allowed_updates": ["message", "callback_query"]}, timeout=30)
                except urllib.error.HTTPError:
                    self.stop.wait(15)                   # A webhook set, a bad token …
                    continue
                except Exception:                        # noqa: BLE001
                    self.stop.wait(5)
                    continue
                for upd in r.get("result") or []:
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    try:
                        _offset_path().write_text(str(offset))
                    except OSError:
                        pass
                    rid = self.handle(upd)
                    if rid:
                        self.done(rid)
        finally:
            try:
                _lock_path().unlink(missing_ok=True)
            except OSError:
                pass


_LISTENER: dict = {}


def listen_for(request: dict) -> Listener | None:
    """Start (or reuse) the Telegram listener for this approval."""
    tg = channels().get("telegram") or {}
    if not (tg.get("token") and tg.get("chat_id")):
        return None
    key = (tg["token"], str(tg["chat_id"]))
    lst = _LISTENER.get(key)
    if lst is None:
        lst = _LISTENER[key] = Listener(*key)
    lst.add(str(request.get("id", "")))
    return lst
