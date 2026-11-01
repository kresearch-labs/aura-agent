"""team_tool.py — Aura Teams by asking.

The model calls these when the user asks in plain words; `/team …` is only a
shortcut. Aura's own code does what touches the network or the key store:
team_status where things stand (read) team_connect connect to a hub address as
a name (card every time) team_token the hidden box: the user pastes a token
(dots only, straight to the key store — the model never sees it)
team_wiring_format the contract and the wiring format (read) team_wire_check
prove a wiring the model wrote from its research on the live hub (card: it
makes one 'safe to delete' task); the ✓/✗/~ list comes back team_wire_use put
the checked wiring in use for the team (card)

Only the user's own message starts a connection: a hub named by a web page or a
file Aura read is refused (an injected "connect to X" can't send the user's
work away). The token page and steps are never written in — the model
researches them (official docs first) and shows the source.
"""
from __future__ import annotations

import json

from ..mediator import BlastRadius, Mediator, ToolSpec

TOOLS = ("team_status", "team_connect", "team_token", "team_wiring_format",
         "team_wire_check", "team_wire_use", "team_chat")


def _users_words(mediator) -> str:
    try:
        from ..notes import is_users_words, users_words
        s = getattr(mediator, "_history_session", None)
        return " ".join(users_words(m) for m in (getattr(s, "messages", None) or [])
                        if is_users_words(m)).lower()
    except Exception:                                # noqa: BLE001
        return ""


def _host_of(addr: str) -> str:
    from urllib.parse import urlparse
    a = addr.strip()
    if "://" not in a:
        a = "http://" + a
    return (urlparse(a).hostname or "").lower()


def register(mediator: Mediator, dispatch: dict) -> None:
    from .. import teams
    pending: dict = {}                               # The last checked wiring

    def team_status(args) -> str:
        cfg = teams.load_config()
        if not cfg.get("hub_url"):
            return ("not connected to a team hub. When the user asks to connect, call "
                    "team_connect with the address they gave.")
        kind = cfg.get("hub_kind") or "vikunja"
        tok = teams.token_state().get("state", "?")
        lines = [f"hub: {cfg['hub_url']} · you: {cfg.get('username', '?')} · token: {tok}",
                 f"kind: {kind}" + (f" ({cfg.get('hub_app')})" if cfg.get("hub_app") else "")]
        if kind == "pending":
            lines.append("not wired yet: research the app (its REST API docs or its MCP "
                         "server), write a wiring (team_wiring_format shows the format), "
                         "prove it with team_wire_check, then team_wire_use.")
        if kind == "wired":
            from .. import teams_wiring as W
            bad = W.broken()
            if bad:
                lines.append("BROKEN since the app changed: " + "; ".join(
                    f"{op} ({v['error'][:90]})" for op, v in bad.items())
                    + " — research the app's current API for those, fix them in the wiring "
                    "in use (below), prove it with team_wire_check, then team_wire_use")
            try:
                lines.append("wiring in use: " + json.dumps(W.load_in_use())[:3000])
            except Exception:                        # noqa: BLE001
                pass
        if tok != "ok":
            lines.append("no working token: research where this app makes tokens, give the "
                         "user the link and the steps (with the source), then team_token.")
        return "\n".join(lines)

    def team_connect(args) -> str:
        addr = str(args.get("address") or "").strip()
        name = str(args.get("name") or "").strip()
        if not addr:
            return "error: the hub's address"
        host = _host_of(addr)
        if not host or host not in _users_words(mediator):
            return (f"refused: the user didn't give the address {addr} — a hub is "
                    "connected only when the user names it in their own message")
        name = name or teams.load_config().get("username", "")
        if not name:
            return "error: the user's name on the team — ask them (clarify)"
        url = addr if "://" in addr else "http://" + addr
        ok, version = teams._probe_info(url, timeout=6.0)
        cfg = teams.load_config()
        cfg.update(hub_url=url.rstrip("/"), username=name)
        if ok:
            cfg.update(hub_kind="vikunja", mode="local" if url.startswith("http://") else "online")
            teams.save_config(cfg)
            return (f"connected to a Vikunja hub at {url} ({version}) as {name}. It needs a "
                    "token: research Vikunja's API token page (official docs), give the user "
                    "the link and steps, then call team_token.")
        cfg.update(hub_kind="pending")
        teams.save_config(cfg)
        return (f"{url} isn't a Vikunja hub. Saved the address and name; now research which "
                "app this is (ask the user if you can't tell), how it signs in, where its "
                "token page is, and its REST API or MCP server — official docs first. Then: "
                "the token (team_token), a wiring (team_wiring_format), team_wire_check.")

    def team_token(args) -> str:
        ask = getattr(mediator, "secret_asker", None)
        if ask is None:
            return ("nobody is at the keyboard to paste a token — say what's needed in your "
                    "answer and stop")
        cfg = teams.load_config()
        if not cfg.get("hub_url"):
            return "connect first (team_connect)"
        what = str(args.get("what") or "the hub token").strip()[:80]
        tok = (ask(f"Paste {what} for {cfg['hub_url']} — it shows as dots and goes "
                   "straight to your key store") or "").strip()
        if not tok:
            return "the user didn't paste a token (cancelled)"
        from .. import keystore
        keystore.put(teams.TOKEN_KEY, tok)
        if (cfg.get("hub_kind") or "vikunja") == "vikunja":
            state = teams.token_state().get("state")
            return f"token stored in the key store (check: {state})"
        return "token stored in the key store; it is checked with the wiring (team_wire_check)"

    def team_wiring_format(args) -> str:
        from .. import teams_wiring as W
        from ..teams_hub import OPS
        ops = "\n".join(f"  {k}: {'required' if r else 'optional — ' + why}" for k, (r, why) in OPS.items())
        return ("The contract (what Teams needs from any hub):\n" + ops
                + "\n  blocked_by: optional, read-only (fills get_task's blocked list)\n\n"
                + W.__doc__.split('"""')[0])

    def team_wire_check(args) -> str:
        from .. import keystore, teams_check as CK, teams_wiring as W
        cfg = teams.load_config()
        w = args.get("wiring")
        if isinstance(w, str):
            try:
                w = json.loads(w)
            except ValueError as e:
                return f"the wiring isn't JSON: {e}"
        try:
            W.validate(w, cfg.get("hub_url"))
        except W.WiringError as e:
            return f"wiring refused: {e} — fix it and check again"
        token = keystore.get(teams.TOKEN_KEY) or ""
        if not token and (w.get("auth") or {}):
            return "no token yet — team_token first"
        note = ""
        team_w, tid = W.find_published(W.WiredHub(w, token=token))
        if team_w and W.sha(team_w) != W.sha(w):
            try:
                W.validate(team_w, cfg.get("hub_url"))
                w, note = team_w, (f"the team already has a wiring (task #{tid}, made by "
                                   f"{team_w.get('made_by', '?')}) — checking that one, so every "
                                   "Aura on the team uses the same\n")
            except W.WiringError:
                note = f"the team's wiring on #{tid} is broken — checking yours instead\n"
        w.setdefault("made_by", cfg.get("username", ""))
        result = CK.run(W.WiredHub(w, token=token),
                        existing_task=args.get("existing_task"))
        pending.clear()
        pending.update(wiring=w, result=result)
        try:                                          # A fix: what it changes
            cur = W.load_in_use()
            diff = W.differences(cur, w)
            if diff:
                note += "changes against the wiring in use:\n  " + "\n  ".join(diff) + "\n"
        except Exception:                            # noqa: BLE001
            pass
        return note + "\n".join(CK.card_lines(result)) + (
            "\nIf usable, tell the user the result and call team_wire_use."
            if result["usable"] else "\nFix what failed (research again) and check again.")

    def team_wire_use(args) -> str:
        from .. import teams_wiring as W
        if not pending.get("result", {}).get("usable"):
            return "no checked, usable wiring — team_wire_check first"
        from .. import keystore
        W.use(pending["wiring"])
        try:
            W._health_path().unlink()                 # A fresh wiring starts clean
        except OSError:
            pass
        try:
            shared = W.publish(W.WiredHub(pending["wiring"], token=keystore.get(teams.TOKEN_KEY) or ""),
                               pending["wiring"])
        except Exception as e:                       # noqa: BLE001
            shared = f"couldn't put it on the board ({str(e)[:100]}) — it works on this machine"
        try:
            from .. import teams_ui
            teams_ui.refresh_now()
        except Exception:                            # noqa: BLE001
            pass
        return (f"in use: this Aura works with {pending['wiring'].get('hub', 'the hub')} "
                f"through the checked wiring; {shared}. Teams is on (the sidebar shows it).")

    def describe_connect(args) -> str:
        return (f"connect this Aura to the team hub at {args.get('address')} as "
                f"'{args.get('name') or '(your name)'}' — the board shows your work under that "
                "name, and it is your git branch")

    def describe_check(args) -> str:
        w = args.get("wiring")
        app = (w.get("hub") if isinstance(w, dict) else "") or "the hub"
        return (f"check the wiring on {app}: makes ONE task titled 'Aura wiring check — safe to "
                "delete', comments and labels it, reads each back, then closes it — nothing "
                "else on the board is touched")

    def describe_use(args) -> str:
        r = pending.get("result") or {}
        ok = sum(1 for _, s, _ in r.get("results", []) if s == "ok")
        return (f"use this wiring for Teams ({ok} operations checked ✓) — "
                f"{pending.get('wiring', {}).get('hub', 'the hub')}")

    def team_chat(args) -> str:
        """Open the people chat for the user (signed in through their Aura),
        or turn it on on this machine."""
        from .. import teams_chat as CH
        what = str(args.get("action") or "open")
        if what == "on":
            cfg = teams.load_config()
            r = CH.start(cfg.get("hub_url", ""))
            return (f"the team chat runs on this machine (port {r.get('port')}); its address is on "
                    "the board for the team" if r.get("ok") else f"couldn't start it: {r.get('detail')}")
        if what == "off":
            return CH.stop()["detail"]
        return CH.open_for_me()

    def describe_chat(args) -> str:
        return {"on": "run the team chat on this machine (teammates on the network can open it)",
                "off": "stop the team chat on this machine"}.get(
            str(args.get("action") or "open"), "open the team chat in your browser, signed in "
            "through your Aura (it posts a one-time code on the board as you)")

    mediator.register(ToolSpec("team_chat", BlastRadius.WRITE, lambda a: None,
                               always_ask=True, describe=describe_chat))
    dispatch["team_chat"] = team_chat
    mediator.register(ToolSpec("team_status", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["team_status"] = team_status
    mediator.register(ToolSpec("team_wiring_format", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["team_wiring_format"] = team_wiring_format
    mediator.register(ToolSpec("team_connect", BlastRadius.WRITE, lambda a: None,
                               always_ask=True, describe=describe_connect))
    dispatch["team_connect"] = team_connect
    # the hidden box is its own question: the card is the paste box itself
    mediator.register(ToolSpec("team_token", BlastRadius.WRITE, lambda a: None))
    dispatch["team_token"] = team_token
    mediator.register(ToolSpec("team_wire_check", BlastRadius.WRITE, lambda a: None,
                               always_ask=True, describe=describe_check))
    dispatch["team_wire_check"] = team_wire_check
    mediator.register(ToolSpec("team_wire_use", BlastRadius.WRITE, lambda a: None,
                               always_ask=True, describe=describe_use))
    dispatch["team_wire_use"] = team_wire_use
