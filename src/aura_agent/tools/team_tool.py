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
import socket

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


def task_ref(c, ref: str):
    """'#12' or '12' — an id first, else the caller's visible tasks by number
    (per-project numbers).
    """
    ref = str(ref).lstrip("#")
    try:
        return int(ref)
    except ValueError:
        status, data = c.request("GET", "/api/v1/tasks")
        for t in data.get("tasks", []):
            if str(t.get("number")) == ref:
                return t["id"]
    return None


def remember_team(cfg: dict) -> None:
    """`aura_teams` list — every team stays; `aura_team` is the ACTIVE one. Each
    team has its own device key (team-device:<team_id>).
    """
    at = cfg.get("aura_team")
    if not at:
        return
    lst = [t for t in (cfg.get("aura_teams") or [])
           if t.get("team_id") != at["team_id"]]
    cfg["aura_teams"] = [at] + lst



_PENDING: dict = {}                                # The last checked wiring


def register(mediator: Mediator, dispatch: dict) -> None:
    from .. import teams
    pending = _PENDING

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
        name = name or teams.my_name()
        if not name:
            return "error: the user's name on the team — ask them (clarify)"
        # An Aura hub answers api/v1/info with protocol+fingerprint — our
        # connect runs that path; anything else keeps the old lanes
        from ..teamserver import tls as _TL_ch
        import json as _j2, http.client as _hc2, ssl as _ssl2
        from urllib.parse import urlparse as _up2
        u2 = _up2(addr if "://" in addr else "https://" + addr)
        try:
            _c2 = _hc2.HTTPSConnection(u2.hostname, u2.port or 7443,
                                       timeout=6,
                                       context=_ssl2._create_unverified_context())
            _c2.request("GET", "/api/v1/info")
            info2 = _j2.loads(_c2.getresponse().read() or b"{}")
            _c2.close()
        except OSError:
            info2 = {}
        if info2.get("protocol") and info2.get("fingerprint"):
            # The hidden box — the model never sees the password ('s secret
            # ask; the player's path is typed in the UI)
            asksecret = getattr(mediator, "secret_asker", None)
            if not callable(asksecret):
                return ("error: the connect needs the hub password typed in "
                        "the hidden box — no secret channel here")
            name = name or teams.my_name()
            pw = asksecret(f"password for '{name}' on {addr}: ")
            if not pw:
                return "stopped: no password was given"
            from .. import teams as _t2
            r = _t2.server_connect(addr, name, pw)
            if not r["ok"]:
                return f"not connected: {r['detail']}"
            from ..options import set_ as _opt_set
            _opt_set("teams.enabled", True, who="setup")
            return (f"connected to your own team hub; this machine's key is "
                    f"registered at it (goes to the card: the fingerprint "
                    f"{r['fingerprint'][:19]}…). The secret is nowhere on "
                    f"disk; the device key keeps the link")
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
        adopted = bool(team_w and W.sha(team_w) == W.sha(w))
        result = CK.run(W.WiredHub(w, token=token),
                        project_id=args.get("project"),
                        existing_task=args.get("existing_task"),
                        read_only=bool(adopted))
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
        if pending.get("result", {}).get("scope") == "reads":
            # H-12: the team's published copy checks reads only — its writes
            # were never proven with YOUR token; full check first
            return ("this wiring was checked on reads only (the team's "
                    "published copy) — its writes are unproven and stay off. "
                    "Check your own draft with team_wire_check (the full run "
                    "makes and closes one 'safe to delete' task), then use.")
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

    def describe_hub_start(args) -> str:
        """The card names the address the hub WILL bind — computed the tool's way
        (teams_server.start: the saved port, else the default).
        """
        name = str(args.get("team_name") or "").strip() or "our-team"
        try:
            from .. import teams_server as TS
            addr = f"https://{lan_addr()}:{TS.saved_port() or TS.DEFAULT_PORT}"
        except Exception:                        # noqa: BLE001
            addr = "this machine's LAN address"
        return (f"start the team hub '{name}' on THIS machine at {addr} — its "
                "own certificate (pinned), database and your owner device are "
                "made; you are the owner")

    def describe_team_use(args) -> str:
        """The card names both teams (the active one and the one the chat switches
        to).
        """
        want = str(args.get("team") or "").strip() or "?"
        try:
            cfg = teams.load_config()
            cur = (cfg.get("aura_team") or {}).get("team", "?")
            known = ", ".join(t.get("team", "?")
                              for t in (cfg.get("aura_teams") or []))
        except Exception:                        # noqa: BLE001
            cur, known = "?", ""
        return (f"switch this chat's team from '{cur}' to '{want}' — its "
                "projects, board and chat follow"
                + (f" (known teams: {known})" if known else ""))

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
        """Open the team's chat — it lives in the hub's page now (removed the old
        standalone LAN chat server); the person signs in there with their name
        and password ('s way in).
        """
        cfg = teams.load_config()
        url = (cfg.get("hub_url") or
               ((cfg.get("aura_team") or {}).get("url")) or "").rstrip("/")
        if not url:
            return ("no hub connected yet — ask the person whether to set up Teams "
                    "now (offer it as a choice) or give me the team's hub "
                    "address; the chat lives in its page")
        import webbrowser
        # the page is served at /team/ on the hub — the hash inside it
        webbrowser.open(f"{url}/team/#/chat")
        return (f"the team's chat opened in your browser ({url}/team/#/chat) — "
                "sign in there; the chat is one part of the team page")

    def describe_chat(args) -> str:
        return "open the team's chat (the hub's page — sign in with your name and password)"


    # ---- Aura's own team server ----

    def _own_cfg() -> dict | None:
        return teams.load_config().get("aura_team")

    def _own_client():
        cfg = _own_cfg()
        if not cfg:
            return None
        from ..teamserver import teamclient
        c = teamclient.AuraClient(cfg)
        if c.sk is None:
            return None
        return c


    def team_use(args) -> str:
        """Switch the active team — by asking (the card names both)."""
        want = str(args.get("team") or "").strip()
        cfg = teams.load_config()
        matches = [t for t in (cfg.get("aura_teams") or [])
                   if want.lower() in (t.get("team_id", "").lower(),
                                       t.get("team", "").lower())]
        if not matches:
            known = ", ".join(t.get("team", "?")
                              for t in (cfg.get("aura_teams") or [])) or "none"
            return (f"no team matches '{want}' — this Aura knows: {known}")
        cfg["aura_team"] = matches[0]
        teams.save_config(cfg)
        return (f"this chat works with '{matches[0]['team']}' now — its "
                "projects, board and chat follow")

    def team_hub_start(args) -> str:
        """Start a team hub HERE. Everything by asking (the card describes it).
        """
        import os
        from .. import teams_server as TS
        name = str(args.get("team_name") or "").strip() or "our-team"
        if TS.running() and _own_cfg():
            return ("the team hub is already running here: "
                    f"{_own_cfg()['url']} as {_own_cfg()['person']}")
        from ..teamserver import tls as TL
        from ..teamserver import teamserver_db
        from ..teamserver.teamserver_db import db_path
        username = teams.my_name() or \
            _users_words(mediator).strip() or "owner"
        cfg = {"url": "", "fingerprint": "", "device_id": "", "person": username,
               "team": name, "team_id": name}
        from ..teamserver.teamclient import new_device_key
        sk = new_device_key(name)
        cert_p, key_p, fp = TL.ensure_cert(teamserver_db.team_dir())
        conn = store_connect()
        if not db_path().exists() or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                " name='people'").fetchone() is None:
            import nacl.encoding
            from ..teamserver import store as ST
            ST.migrate(conn)
            store_bootstrap(conn, name, username,
                            sk.verify_key.encode(nacl.encoding.RawEncoder),
                            f"{socket.gethostname()[:20]}-aura", fp)
        r = TS.start()
        if not r.get("ok"):
            return f"the team server did not start: {r.get('detail')}"
        port = r["port"]
        cfg = teams.load_config()
        cfg["aura_team"] = {"url": f"https://127.0.0.1:{port}",
                            "lan": f"https://{lan_addr()}:{port}",
                            "fingerprint": fp, "device_id": _own_device_id(conn, username),
                            "person": username, "team": name, "team_id": name,
                            "role": "owner"}
        remember_team(cfg)
        teams.save_config(cfg)
        # Asked ONCE whether the hub starts with Aura here; the asked flag and
        # the answer persist in the SAME write — saving a freshly re-loaded
        # config would drop the in-memory answer (the old bug: it never stuck,
        # and it never asked again)
        c = teams.load_config()
        if not c.get("hub_autostart_asked"):
            c["hub_autostart_asked"] = True
            # The question carries the [team] mark (the chat screen heads it
            # TEAM) and goes through mediator.asker — or, in the lifecycle
            # tests' fakes, mediator.ask (same (question, options) shape). The
            # real Mediator has only.asker — the old code read the
            # never-set.ask on it, so the question never fired there.
            ask = getattr(mediator, "asker", None) or \
                getattr(mediator, "ask", None)
            if ask is not None:
                try:
                    ans = ask("[team] Start the team hub automatically when "
                              "Aura starts?",
                              ["yes — autostart", "no — start it myself"])
                    c["team.hub_autostart"] = str(ans or "").startswith("yes")
                except Exception:                    # noqa: BLE001
                    pass
            teams.save_config(c)
        shared = ("For your projects I'll share work events — file names, "
                  "test results, decisions, commits. "
                  "Never your chat with me, prompts, code or secrets. "
                  "Ask me any time to turn the reports off.")
        return (f"the team '{name}' is live on this machine at "
                f"https://{lan_addr()}:{port} (pinned fingerprint {fp[:19]}…). "
                "You are the owner. Add people at the hub's Administration › "
                "Users (an admin makes each account). " + shared
                + " " + teams.ADMIN_NOTE)

    def lan_addr():
        from ..teams import lan_address
        return lan_address()

    def store_connect():
        from ..teamserver import teamserver_db
        from ..teamserver import store as ST
        return ST.connect(teamserver_db.db_path())

    def store_bootstrap(conn, team_name, owner_name, pubkey, device_name, fp):
        from ..teamserver import store as ST
        return ST.bootstrap(conn, team_name, owner_name, pubkey, device_name,
                            cert_fp=fingerprint_of())

    def fingerprint_of():
        from ..teamserver import tls as TL
        from ..teamserver import teamserver_db
        cert = teamserver_db.team_dir() / TL.CERT_FILE
        return TL.fingerprint(cert.read_bytes()) if cert.exists() else ""

    def _own_device_id(conn, person_name):
        row = conn.execute(
            "SELECT d.id FROM devices d JOIN people p ON p.id = d.person"
            " WHERE p.name = ? ORDER BY d.created DESC LIMIT 1",
            (person_name,)).fetchone()
        return row["id"] if row else ""

    def base64_pubkey(sk):
        import base64
        import nacl.encoding
        return base64.b64encode(
            sk.verify_key.encode(nacl.encoding.RawEncoder)).decode()

    def device_name_of():
        import platform
        return platform.node()[:40] + "-aura"

    def pinned_connection(url, fp):
        import http.client
        from urllib.parse import urlparse
        u = urlparse(url if "://" in url else "https://" + url)
        from ..teamserver.teamclient import Pinned
        return Pinned(u.hostname, u.port or 7443, fp, timeout=10)

    def team_people(args) -> str:
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        status, data = c.request("GET", "/api/v1/people")
        if status != 200:
            return f"the server refused: {status}"
        lines = []
        for p in data.get("people", []):
            mark = " (web)" if p.get("web") else ""
            off = " · disabled" if p.get("disabled_at") else ""
            lines.append(f"{p['name']} — {p['role']}{mark}{off}")
        if not lines:
            return "nobody yet — add people at the hub's Administration › Users"
        # Person names are written on the hub by people — the model reads them
        # as OUTSIDE TEXT (mirrors teams_run's banner)
        from ..teams_board import OUTSIDE_BANNER
        try:
            from ..secrets_guard import sanitize_output
            return OUTSIDE_BANNER + sanitize_output("\n".join(lines),
                                                    is_external=True)
        except Exception:                        # noqa: BLE001
            return OUTSIDE_BANNER + "\n".join(lines)

    def team_revoke(args) -> str:
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        what = str(args.get("device") or "").strip()
        status, data = c.request("DELETE", f"/api/v1/devices/{what}")
        if status != 200:
            return f"the server refused: {data.get('error', {}).get('message', status)}"
        return f"revoked device {what} — it can no longer sign requests"

    def describe_revoke(args) -> str:
        return f"revoke the device {args.get('device')} — it stops working "                "until it is invited again"

    def team_rename_me(args) -> str:
        """Your name on the team = your branch name; renamed by asking, both sides
        in step.
        """
        new = str(args.get("name") or args.get("new_name") or "").strip()
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        status, data = c.request("PATCH", "/api/v1/people/me", {"name": new})
        if status != 200:
            return f"the server refused: {data.get('error', {}).get('message', status)}"
        cfg = teams.load_config()
        cfg["username"] = new
        if cfg.get("aura_team"):
            cfg["aura_team"]["person"] = new
        teams.save_config(cfg)
        return (f"you are '{new}' on the team now — your branch name for new "
                "work is that too")

    def describe_rename(args) -> str:
        new = args.get("new_name") or "(new name)"
        return (f"you'll appear as '{new}' on the team; your branch name for "
                "new work becomes that")

    def team_hub_handover(args) -> str:
        """Seal the move bundle for the receiving person's device."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        device = str(args.get("device") or "").strip()
        from urllib.parse import quote
        status, data = c.request("GET", "/api/v1/hub/bundle?device="
                                 + quote(device))
        if status != 200:
            return (f"the server refused: "
                    f"{data.get('error', {}).get('message', status)}")
        import tempfile
        from pathlib import Path
        out = Path(tempfile.mkdtemp()) / f"hub-bundle-{device}.b64"
        out.write_text(data.get("bundle", ""))
        return (f"the move bundle is sealed for {device} and written to "
                f"{out} — hand it to them (USB or chat). The old hub stops "
                "serving after they take over.")

    def describe_handover(args) -> str:
        return ("seal the team's database + certificate for "
                f"{args.get('device')} — the receiver takes over with the "
                "same fingerprint, nobody needs a new invite")

    def team_hub_take(args) -> str:
        """The receiver's side — open the bundle, restore, start."""
        from .. import teams_server as TS
        cfg = _own_cfg() or {}
        team_id = cfg.get("team_id") or "team"
        r = TS.take_over(str(args.get("bundle") or ""), team_id)
        if not r.get("ok"):
            return f"couldn't take over: {r.get('detail')}"
        return (f"this machine is the hub now (port {r.get('port')}); the "
                "fingerprint is unchanged, every device still connects")

    def describe_take(args) -> str:
        return ("take the hub over here from the move bundle — the old hub "
                "stops serving after this; the certificate moves with it, "
                "so nobody needs a new invite")

    def team_address(args) -> str:
        """Check an address notice's signature against the pinned certificate,
        then update the saved URL (card).
        """
        cfg = _own_cfg()
        if not cfg:
            return "not on a team yet"
        from ..teamserver.teamclient import verify_address_notice
        j = verify_address_notice(str(args.get("notice") or ""),
                                  cfg.get("fingerprint", ""))
        if not j:
            return ("refused: that notice's signature or fingerprint does "
                    "not match this team's server — not updating anything")
        cfg["url"] = j["u"]
        teams.save_config(cfg)
        from ..teamserver.teamserver_db import open_team
        conn = open_team()
        if conn is not None:
            from ..teamserver import log as logmod
            from ..teamserver import store as ST
            with ST.write(conn):
                src_, ev_ = logmod.server_event()
                logmod.append(conn, None, src_, ev_, "hub.moved", j["u"],
                              None, {"notice": True})
        return f"the team's address is now {j['u']} (notice verified)"

    def describe_address(args) -> str:
        return ("check the hub's signed address notice and update the "
                "saved address if it verifies")

    def team_bind(args) -> str:
        """Bind a project to THIS workspace's repository (card)."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        from ..teams_collide import repo_identity
        ident = repo_identity(str(getattr(mediator, "work_dir", "") or "."))
        slug = str(args.get("project") or "").strip()
        status, data = c.request("GET", "/api/v1/projects")
        if status != 200:
            return f"the server refused: {status}"
        pro = [p for p in data.get("projects", [])
               if p["slug"] == slug or p["name"].lower() == slug.lower()]
        if not pro:
            return f"no project '{slug}' — ask for one with team_project"
        settings = pro[0].get("settings") or {}
        import json as _j
        s = _j.loads(settings) if isinstance(settings, str) else dict(settings or {})
        repos = list((s.get("repos") or []))
        if ident and ident not in repos:
            repos.append(ident)
        if not repos:
            folders = list((s.get("folders") or []))
            if slug not in folders:
                folders.append(slug)
            s["folders"] = folders
        s["repos"] = repos
        status, data = c.request("PATCH",
                                 f"/api/v1/projects/{pro[0]['id']}",
                                 {"settings": s})
        if status != 200:
            return f"the server refused: {status}"
        return (f"'{pro[0]['name']}' is bound to this workspace's repository "
                f"{ident or '(no git — bound as a folder)'} — chats started "
                "here pick it automatically")

    def describe_bind(args) -> str:
        return (f"bind this workspace's repository to the project "
                f"'{args.get('project')}' — chats started here then work on "
                "that project without asking")

    def team_project(args) -> str:
        """A new project (card)."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        status, data = c.request("POST", "/api/v1/projects",
                                 {"name": str(args.get("name") or "").strip()})
        if status != 200:
            return f"the server refused: {data.get('error', {}).get('message', status)}"
        return (f"project '{data['project']['name']}' created with the six "
                "default statuses; you are its manager — bind a repository "
                "with team_bind")

    def describe_project(args) -> str:
        return f"create the project '{args.get('name')}' with the default statuses"

    def team_members(args) -> str:
        """Add a member with a role (card)."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        slug = str(args.get("project") or "")
        person = str(args.get("person") or "")
        role = str(args.get("role") or "member")
        status, data = c.request("GET", "/api/v1/people")
        if status != 200:
            return "the server refused the people list"
        who = [p for p in data.get("people", []) if p["name"] == person]
        if not who:
            return f"no person named '{person}' on the team"
        status, pro = c.request("GET", "/api/v1/projects")
        pro = [p for p in pro.get("projects", []) if p["slug"] == slug]
                                                                    # named fix:
                                                            # the PROJECT list
        if not pro:
            return f"no project '{slug}'"
        status, data = c.request("PUT",
                                 f"/api/v1/projects/{pro[0]['id']}/members/"
                                 f"{who[0]['id']}", {"role": role})
        if status != 200:
            return f"the server refused: {status}"
        return f"'{person}' is '{role}' on '{slug}' now"

    def describe_members(args) -> str:
        return (f"add '{args.get('person')}' to '{args.get('project')}' as "
                f"'{args.get('role') or 'member'}'")

    def team_merged(args) -> str:
        """Merged is a fact; without git this clears a collision."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        tid = int(str(args.get("task")).lstrip("#"))
        merged = bool(args.get("merged", True))
        t = c.get_task_json(tid) if hasattr(c, "get_task_json") else None
        from ..teamserver.teamclient import AuraClient as _AC
        version = (t or {}).get("version") or 1
        status, data = c.request("PATCH", f"/api/v1/tasks/{tid}",
                                 {"merged": merged, "version": version})
        if status != 200:
            return f"the server refused: {data.get('error', {}).get('message', status)}"
        return f"#{tid} marked {'merged' if merged else 'not merged'}"

    def describe_merged(args) -> str:
        return (f"mark #{str(args.get('task')).lstrip('#')} as merged (a fact "
                "— it clears the done-but-not-merged collision; the invite-"
                "free way when the project has no git)")

    def team_task_new(args) -> str:
        """By asking: a manager plans from Aura (card)."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        status, data = c.request("GET", "/api/v1/projects")
        pro = [p for p in data.get("projects", [])
               if p["slug"] == str(args.get("project") or "")]
        if not pro:
            return f"no project '{args.get('project')}'"
        payload = {"project": pro[0]["id"],
                   "title": str(args.get("title") or "").strip()}
        if args.get("for"):
            payload["assignees"] = [str(args["for"])]
        status, data = c.request("POST", "/api/v1/tasks", payload)
        if status != 200:
            return f"the server refused: {data.get('error', {}).get('message', status)}"
        t = data["task"]
        return (f"#{t['number']} '{t['title']}' created in "
                f"'{args.get('project')}'"
                + (f" — assigned to {args.get('for')}" if args.get("for")
                   else ""))

    def describe_task_new(args) -> str:
        return (f"create the task '{args.get('title')}' in "
                f"'{args.get('project')}' — assigned to "
                f"'{args.get('for') or 'you'}' so the team can see it")

    def team_task_assign(args) -> str:
        """Assign an existing task to a person (card)."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        tid = task_ref(c, str(args.get("task")))
        if tid is None:
            return f"no task {args.get('task')}"
        t = c.get_task_json(tid)
        if t is None:
            return f"no task #{tid}"
        people = [a["name"] for a in t.get("assignees", [])]
        person = str(args.get("person") or "")
        if person not in people:
            people.append(person)
        status, data = c.request("PATCH", f"/api/v1/tasks/{tid}",
                                 {"assignees": people,
                                  "version": t.get("version", 1)})
        if status != 200:
            return (f"the server refused: "
                    f"{data.get('error', {}).get('message', status)}")
        return f"#{t['number']} assigned to '{person}' — their Aura is told once"

    def describe_task_assign(args) -> str:
        return (f"assign #{str(args.get('task')).lstrip('#')} to "
                f"'{args.get('person')}' — their Aura tells them once")

    def team_map(args) -> str:
        """Read the shared map back (through a tool, not the prompt) — only when
        this chat is linked to a team project.
        """
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        link = getattr(mediator, "team_link", None) or {}
        pid = link.get("project_id") if isinstance(link, dict) else None
        if not pid:
            return "not linked to a team project"
        query = str(args.get("query") or "")
        from urllib.parse import quote
        qs = f"?query={query}" if query else ""
        status, data = c.request("GET",
                                 f"/api/v1/projects/{pid}/map{qs}")
        if status != 200:
            return f"the server refused the map: {status}"
        lines = []
        for f in data.get("files", [])[:40]:
            who = f" — {f['now_editing']} is changing it" \
                if f.get("now_editing") else ""
            lines.append(f"{f['path']} ({f['lang'] or '?'}, "
                         f"{f['size']} B): {f['summary']}{who}")
        for s in data.get("symbols", [])[:60]:
            lines.append(f"  {s['path']}:{s['line']} {s['signature']}")
        for c2 in data.get("changed_since", [])[:10]:
            lines.append(f"changed since you looked: {c2['type']} "
                         f"{c2['subject']}")
        lines.append(data.get("note", "open the real file before "
                              "changing it"))
        import json as _j
        cfgk = teams.load_config().setdefault("map_card_seen", {})
        if not cfgk.get(str(pid)):
            cfgk[str(pid)] = True
            teams.save_config(teams.load_config())
            lines.append("(first read: the map carries file names, sizes, "
                         "one-line summaries and function/class names with "
                         "signatures — never code bodies; it refreshes "
                         "when a task finishes or someone runs /team map "
                         "now.)")
        result = "\n".join(lines)
        try:
            from ..secrets_guard import sanitize_output
            result = sanitize_output(result, is_external=True)
        except Exception:                            # noqa: BLE001
            pass
        return result

    def describe_map(args) -> str:
        return ("show the team's shared code map for the linked project "
                "(ranked files, symbol names/signatures, who is changing "
                "what) — helps when starting work in a shared project")

    def team_import_vikunja(args) -> str:
        """Import a Vikunja export zip (card, dry-run first)."""
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        path = str(args.get("zip") or "").strip()
        commit = bool(args.get("do_it"))
        if not path:
            return "give the path to the Vikunja export .zip"
        from ..vikunja_import import dry_run, import_zip
        try:
            if not commit:
                counts = dry_run(path)
                return ("dry run — importing would create: " + ", ".join(
                    f"{k}: {v}" for k, v in counts.items()) +
                    ". Ask me to import it for real to go ahead.")
            from ..vikunja_import import import_into
            counts = import_into(c, path)
            return ("imported: " + ", ".join(f"{k}: {v}" for k, v in
                                             counts.items()))
        except Exception as e:                       # noqa: BLE001
            return f"the import failed: {e}"

    def describe_import(args) -> str:
        return ("read a Vikunja export zip (projects, tasks, comments, "
                "labels) into this team — dry-run summary first, nothing "
                "written until you say go")

    def team_wire_fix(args) -> str:
        """A wired operation fails → re-check, diff, fix on a yes."""
        from .. import teams_wiring as W
        from .. import keystore
        op = str(args.get("operation") or "").strip()
        if not op:
            return "name the operation that fails"
        new = args.get("wiring")
        if isinstance(new, str):
            try:
                import json as _json
                new = _json.loads(new)
            except ValueError as e:
                return f"the replacement wiring isn't JSON: {e}"
        token = keystore.get(teams.TOKEN_KEY) or ""
        info = W.repair(op, new=new if isinstance(new, dict) else None,
                        token=token)
        if not info.get("known") and not info.get("entry"):
            return (f"'{op}' isn't a wiring operation — the contract says: "
                    + ", ".join(sorted(W.KNOWN)))
        if info.get("error") and info.get("still_failing") is None \
                and not info.get("recorded"):
            return info["error"]
        lines = ["wiring '" + op + "' re-checked now: " +
                 ("STILL FAILING — " + info["error"] if info["still_failing"]
                  else ("works right now (the earlier failure is recorded: "
                        + (info["recorded"].get("error", "")[:90]
                           if info["recorded"] else "n/a") + ")"))]
        if info.get("valid_new") is False:
            lines.append("your replacement wiring is refused: "
                         + info["new_error"])
        if info.get("diffs"):
            lines.append("what the fix changes:")
            lines += ["  " + d for d in info["diffs"][:8]]
            lines.append("proven on the check task first (team_wire_check), "
                         "applied on a yes (team_wire_use)")
        return "\n".join(lines)

    def describe_wire_fix(args) -> str:
        return (f"re-check the wiring for '{args.get('operation')}' that "
                "started failing — show what the hub changed and fix it "
                "on your yes")

    def team_notices(args) -> str:
        """Set the notice targets for a project (card: who will see them; Teams =
        out only on a LAN).
        """
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        import json as _j
        from .. import teams as _T
        cfg = _T.load_config()
        at = cfg.get("aura_team") or {}
        app = str(args.get("app") or "webhook")
        ask = getattr(mediator, "secret_asker", None)
        where = (args.get("where") or "").strip()
        if not where and ask is not None:
            where = (ask("Paste the channel/webhook URL — it shows as dots, "
                         "goes to the server's secrets table, and I never read it")
                     or "").strip()
        types = [t for t in (args.get("types") or
                             ["task.finished", "task.blocked",
                              "decision.made"])]
        if not where:
            return "give the webhook/channel URL (asked in the hidden box)"
        lan_note = (" Microsoft Teams gets posts OUT only — it works on "
                    "this machine's network." if app == "teams" else "")
        # Store the target in the RIGHT project's settings (the URL is a secret
        # class of its own — the card says so; the hidden box holds it).
        status, pro = c.request("GET", "/api/v1/projects")
        plist = pro.get("projects") or []
        if not plist:
            return "no projects yet"
        pick = args.get("project")
        if pick is None and len(plist) == 1:
            pid = plist[0]["id"]
        elif pick is None:
            return ("which project? " +
                    ", ".join(p["name"] for p in plist) +
                    " — name one")
        else:
            named = [p for p in plist
                     if str(p.get("id")) == str(pick).strip()
                     or p.get("name", "").lower() == str(pick).lower()]
            if not named:
                return (f"no project named '{pick}' — try one of: "
                        + ", ".join(p["name"] for p in plist))
            pid = named[0]["id"]
        settings = {}
        for project in plist:
            if project["id"] == pid:
                settings = project.get("settings") or {}
        s = _j.loads(settings) if isinstance(settings, str) else \
            dict(settings or {})
        notices = list(s.get("notices") or [])
        notices.append({"app": app, "where": where, "types": types})
        s["notices"] = notices
        status, _ = c.request("PATCH", f"/api/v1/projects/{pid}",
                              {"settings": s})
        if status != 200:
            return f"the server refused: {status}"
        return (f"Posts {', '.join(types)} of this project to {where}. "
                "Everyone in that channel will see them — Aura's "
                "permissions don't apply there." + lan_note)

    def describe_notices(args) -> str:
        return ("post the team's key moments to {where} — "
                "everyone in that channel will see them"
                ).format(where=args.get("where"))

    def team_watch(args) -> str:
        """The lead's watch — what did the team do today? Reads the activity
        endpoints across the projects the admin can see; the data rides the
        tools as OUTSIDE TEXT (no prompt change).
        """
        c = _own_client()
        if c is None:
            return "not on your own team server yet"
        me = teams.load_config().get("aura_team") or {}
        if me.get("role") not in ("owner", "admin"):
            return "the watch is for the owner or an admin"
        status, pro = c.request("GET", "/api/v1/projects")
        if status != 200:
            return "the server refused the projects list"
        out = []
        for project in pro.get("projects", []):
            pid = project["id"]
            status, data = c.request(
                "GET", f"/api/v1/projects/{pid}/activity")
            if status != 200:
                continue
            events = data.get("events", [])
            if not events:
                continue
            out.append(f"{project.get('name', pid)}:")
            for e in events[:12]:
                out.append(f"  {e['who'] or '?'} {e['sentence']}")
        if not out:
            return "nothing has happened yet on any project."
        result = "\n".join(out)
        try:
            from ..secrets_guard import sanitize_output
            result = sanitize_output(result, is_external=True)
        except Exception:                            # noqa: BLE001
            pass
        return result

    def describe_watch(args) -> str:
        return ("show what the team did (today's key moments across the "
                "projects) — a read-only look at the shared activity")


    for tool_name, fn, radius, describe in (
            ("team_import_vikunja", team_import_vikunja, BlastRadius.WRITE,
             describe_import),
            ("team_wire_fix", team_wire_fix, BlastRadius.WRITE,
             describe_wire_fix),
            ("team_watch", team_watch, BlastRadius.READ, describe_watch),
            ("team_notices", team_notices, BlastRadius.WRITE,
             describe_notices),
            ("team_map", team_map, BlastRadius.READ, describe_map),
            ("team_bind", team_bind, BlastRadius.WRITE, describe_bind),
            ("team_project", team_project, BlastRadius.WRITE,
             describe_project),
            ("team_members", team_members, BlastRadius.WRITE,
             describe_members),
            ("team_merged", team_merged, BlastRadius.WRITE, describe_merged),
            ("team_task_new", team_task_new, BlastRadius.WRITE,
             describe_task_new),
            ("team_task_assign", team_task_assign, BlastRadius.WRITE,
             describe_task_assign)):
        spec = (ToolSpec(tool_name, radius, lambda a: None, always_ask=True,
                         describe=describe) if describe
                else ToolSpec(tool_name, radius, lambda a: None))
        mediator.register(spec)
        dispatch[tool_name] = fn


    for tool_name, fn, radius, describe in (
            ("team_hub_handover", team_hub_handover, BlastRadius.WRITE,
             describe_handover),
            ("team_hub_take", team_hub_take, BlastRadius.WRITE, describe_take),
            ("team_address", team_address, BlastRadius.WRITE,
             describe_address)):
        spec = (ToolSpec(tool_name, radius, lambda a: None, always_ask=True,
                         describe=describe) if describe
                else ToolSpec(tool_name, radius, lambda a: None))
        mediator.register(spec)
        dispatch[tool_name] = fn


    for tool_name, fn, radius, describe in (
            # Every team step shows a card.
            ("team_hub_start", team_hub_start, BlastRadius.WRITE,
             describe_hub_start),
            ("team_people", team_people, BlastRadius.READ, None),
            ("team_revoke", team_revoke, BlastRadius.WRITE, describe_revoke),
            ("team_rename_me", team_rename_me, BlastRadius.WRITE,
             describe_rename),
            ("team_use", team_use, BlastRadius.WRITE, describe_team_use)):
        spec = ToolSpec(tool_name, radius, lambda a: None,
                        always_ask=True, describe=describe) \
            if describe else ToolSpec(tool_name, radius, lambda a: None)
        mediator.register(spec)
        dispatch[tool_name] = fn

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
