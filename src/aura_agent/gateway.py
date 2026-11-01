"""gateway.py — Messaging delivery.

Outbound delivery of Aura messages to chat platforms, stdlib-only:
  telegram — Bot API sendMessage (AURA_TELEGRAM_BOT_TOKEN + chat_id)
  discord — webhook execute (AURA_DISCORD_WEBHOOK_URL)
  slack — webhook postMessage (AURA_SLACK_WEBHOOK_URL)
  webhook — generic JSON POST (AURA_WEBHOOK_URL)
  outbox — engagement outbox (always available, offline)

Config precedence per send: CLI flag > env > engagement/gateway.json >
~/.aura/profiles/<profile>/gateway.json. Secrets never printed (masked).

Public: deliver_message(text, channel, **cfg) -> dict(ok, detail, where) aura
gateway test — probe each configured channel aura gateway send <channel> <text>
aura gateway status — which channels are configured (masked) Delivery failures
are structured (ok=False, detail) — never raise to the operator; cron/loop
callers treat failure as data (§ silence is a bug).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CHANNELS = ("telegram", "discord", "slack", "webhook", "outbox")
_SECRET_FIELDS = {"token", "bot_token", "url", "webhook_url"}


def _mask(s: str) -> str:
    if len(s) <= 10:
        return "*" * len(s)
    return f"{s[:6]}…{s[-4:]}"


def _config(eng: Path | None) -> dict:
    """Merged channel config: files then env (env wins)."""
    cfg: dict = {}
    from .paths import profile_path
    for base in (profile_path("gateway.json").parent,
                 Path(eng) if eng else None):
        if base is None:
            continue
        p = base / "gateway.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            # tokens and webhook URLs (a webhook URL is a key) live in the
            # profile's key store; the file keeps "@keystore" for them
            from .keystore import seal, unseal
            prefix = ("gateway.json" if base == profile_path(
                "gateway.json").parent else f"gateway.json@{base.name}")
            if seal(data, prefix, _SECRET_FIELDS):
                p.write_text(json.dumps(data, indent=1), encoding="utf-8")
            cfg.update(unseal(data, prefix))
    env_map = {
        "telegram": ("AURA_TELEGRAM_BOT_TOKEN", "AURA_TELEGRAM_CHAT_ID"),
        "discord": ("AURA_DISCORD_WEBHOOK_URL",),
        "slack": ("AURA_SLACK_WEBHOOK_URL",),
        "webhook": ("AURA_WEBHOOK_URL",),
    }
    for ch, vars_ in env_map.items():
        for v in vars_:
            if os.environ.get(v):
                cfg.setdefault(ch, {})
                key = v.replace(f"AURA_{ch.upper()}_", "").lower()
                cfg[ch][key] = os.environ[v]
    return cfg


# ---------------------------------------------- flood limit + dead targets
# A flood limit keeps a looping job from spamming a channel. Nothing is lost: a
# message that isn't sent goes to the outbox.
RATE_MAX, RATE_WINDOW = 20, 600          # At most 20 messages per target per 10 minutes
OPEN_AFTER = 5                           # Consecutive failures → paused
OFF_AFTER = 25                           # → off until `aura gateway test`
COOL_FIRST, COOL_MAX = 900, 86400        # 15 min, doubling, up to a day


def _health_path() -> Path:
    from .paths import profile_path
    return profile_path("gateway_health.json")


def _health() -> dict:
    try:
        return json.loads(_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_health(h: dict) -> None:
    try:
        p = _health_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(h, indent=1), encoding="utf-8")
    except OSError:
        pass


def _target_key(channel: str, ch_cfg: dict) -> str:
    import hashlib
    ident = ch_cfg.get("url") or ch_cfg.get("chat_id") or channel
    return f"{channel}:{hashlib.sha256(str(ident).encode()).hexdigest()[:12]}"


def _blocked(key: str, now: float) -> str | None:
    """Why this target isn't sent to now (None = send)."""
    t = _health().get(key) or {}
    if t.get("off"):
        return (f"turned off after {t.get('fails')} failures in a row — "
                "`aura gateway test` turns it back on")
    if now < float(t.get("open_until") or 0):
        return (f"paused after {t.get('fails')} failures in a row — next try at "
                + time.strftime("%H:%M", time.localtime(float(t['open_until']))))
    sent = [x for x in t.get("sent") or [] if now - x < RATE_WINDOW]
    if len(sent) >= RATE_MAX:
        return f"more than {RATE_MAX} messages in {RATE_WINDOW // 60} minutes (a looping job?)"
    return None


def _record(key: str, res: dict, now: float, retry_after: float | None = None) -> None:
    h = _health()
    t = h.get(key) or {}
    if res.get("ok"):
        t = {"fails": 0, "sent": [x for x in t.get("sent") or [] if now - x < RATE_WINDOW] + [now]}
    else:
        status = int(res.get("status") or 0)
        weight = 2 if status in (404, 410) else 1           # Deleted target: dead sooner
        t["fails"] = int(t.get("fails") or 0) + weight
        if status == 429 and retry_after:
            t["open_until"] = now + min(float(retry_after), 3600)
        elif t["fails"] >= OFF_AFTER:
            t["off"] = True
        elif t["fails"] >= OPEN_AFTER:
            cool = min(COOL_MAX, COOL_FIRST * 2 ** max(0, t["fails"] - OPEN_AFTER))
            t["open_until"] = now + cool
    h[key] = t
    _save_health(h)


def reset_health(channel: str | None = None) -> None:
    """After a successful `aura gateway test`: the target is on again."""
    h = _health()
    for k in list(h):
        if channel is None or k.startswith(channel + ":"):
            h.pop(k)
    _save_health(h)


def health_lines() -> list[str]:
    out = []
    for k, t in _health().items():
        if t.get("off"):
            out.append(f"  {k}: OFF after {t.get('fails')} failures — aura gateway test")
        elif float(t.get("open_until") or 0) > time.time():
            out.append(f"  {k}: paused until " + time.strftime("%H:%M", time.localtime(float(t['open_until']))))
        elif t.get("fails"):
            out.append(f"  {k}: {t['fails']} failure(s) in a row")
    return out


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(ok=r.status < 400, status=r.status,
                        detail=r.read().decode("utf-8",
                                               errors="replace")[:200])
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        return dict(ok=False, status=e.code, retry_after=float(ra) if ra and ra.isdigit() else None,
                    detail=(e.read() or b"").decode("utf-8",
                                                    errors="replace")[:200])
    except Exception as e:
        return dict(ok=False, status=0, detail=str(e)[:200])


def deliver_message(text: str, channel: str = "outbox",
                    eng: Path | None = None, **overrides) -> dict:
    """Send text via channel. Returns {ok, detail, where} — never raises."""
    text = (text or "").strip()
    if not text:
        return dict(ok=False, detail="empty message", where=channel)
    cfg = _config(eng)
    ch_cfg = {**(cfg.get(channel) or {}), **overrides}
    if channel in CHANNELS[0:4]:
        key, now = _target_key(channel, ch_cfg), time.time()
        why = None if overrides.get("_force") else _blocked(key, now)
        if why:
            kept = deliver_message(text, "outbox", eng)
            return dict(ok=False, detail=f"not sent to {channel}: {why} — kept in the outbox",
                        where=kept.get("where"), held=True)
        res = _send(channel, text, ch_cfg)
        _record(key, res, now, res.pop("retry_after", None))
        return res

    if channel == "outbox" or channel not in CHANNELS[0:4]:
        outbox = (Path(eng) if eng else Path.cwd() / "engagement") / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        dest = outbox / f"gw-{int(time.time())}.json"
        dest.write_text(json.dumps({"channel": channel, "message": text},
                                   ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return dict(ok=True, detail="queued", where=str(dest))

    return _send(channel, text, ch_cfg)


def _send(channel: str, text: str, ch_cfg: dict) -> dict:
    if channel == "telegram":
        token = ch_cfg.get("token") or ""
        chat_id = ch_cfg.get("chat_id") or ""
        if not (token and chat_id):
            return dict(ok=False, detail="telegram needs bot token + chat_id",
                        where=channel)
        res = _post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                         {"chat_id": chat_id, "text": text[:4000]})
        return dict(**res, where=f"telegram:{_mask(chat_id)}")

    if channel in ("discord", "slack"):
        url = ch_cfg.get("url") or ""
        if not url.startswith(("http://", "https://")):
            return dict(ok=False, detail=f"{channel} needs a webhook URL",
                        where=channel)
        # discord takes {"content": ...}; slack takes {"text": ...}
        payload = ({"content": text[:2000]} if channel == "discord"
                   else {"text": text[:4000]})
        res = _post_json(url, payload)
        return dict(**res, where=f"{channel}:{_mask(url)}")

    if channel == "webhook":
        url = ch_cfg.get("url") or ""
        if not url.startswith(("http://", "https://")):
            return dict(ok=False, detail="webhook needs a URL",
                        where=channel)
        res = _post_json(url, {"source": "aura", "message": text[:4000]})
        return dict(**res, where=f"webhook:{_mask(url)}")

    return dict(ok=False, detail=f"unknown channel {channel}",
                where=channel)


def status(eng: Path | None = None) -> str:
    cfg = _config(eng)
    lines = ["gateway channels:"]
    for ch in CHANNELS:
        c = cfg.get(ch) or {}
        if ch == "outbox":
            lines.append("  ✓ outbox    always available (offline queue)")
        elif c:
            parts = [f"{k}={_mask(v)}" for k, v in sorted(c.items())
                     if isinstance(v, str)]
            lines.append(f"  ✓ {ch:9s} configured ({', '.join(parts)})")
        else:
            lines.append(f"  • {ch:9s} not configured")
    return "\n".join(lines)


def probe(eng: Path | None = None) -> dict:
    """Live test every configured channel (except outbox — no network)."""
    cfg = _config(eng)
    out = {}
    for ch in CHANNELS:
        if ch == "outbox":
            out[ch] = deliver_message("gateway probe", "outbox", eng=eng)
            continue
        if cfg.get(ch):
            # a test always tries (even a paused or turned-off target) and
            # turns it back on when it works
            out[ch] = deliver_message("gateway probe from Aura", ch, eng=eng, _force=True)
            if out[ch].get("ok"):
                reset_health(ch)
    return out
