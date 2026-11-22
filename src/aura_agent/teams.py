"""teams.py — Aura Teams: the hub (Vikunja) and the per-machine connection.

The hub is Vikunja: a separate, unmodified program, downloaded from its
official GitHub release, checksum checked against the sha256 digest the release
API publishes for the asset. Never bundled, never forked. - It is only ever
offered — downloaded and started on an explicit yes. - Its licence (AGPL-3.0)
and source link are shown when offering it. Say "works with Vikunja"; never use
its logo as an endorsement. - The hub's data lives in Aura's folder; it listens
on the LAN; sign-up on. - New versions are offered on the hub machine only — on
a yes. - Moving the hub is Vikunja's own dump/restore (shown, not reinvented).
- The hub token lives in the key store, never in the config file. - Username is
one question at install (setup, "You" step; `aura teams connect --user` can set
it too); it is the git branch name. - Several Aura windows on one machine are
the same person's work. - The hub never controls an Aura. Aura works with the
hub off.

Everything here is stdlib. Vikunja is touched only over its HTTP API and as a
downloaded program run in its own process.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .paths import aura_home

HUB_RELEASES_API = "https://api.github.com/repos/go-vikunja/vikunja/releases/latest"
HUB_PROJECT_URL = "https://github.com/go-vikunja/vikunja"
HUB_LICENCE = "AGPL-3.0"
HUB_LICENCE_URL = "https://github.com/go-vikunja/vikunja/blob/main/LICENSE"
DEFAULT_PORT = 3456
TOKEN_KEY = "teams-hub-token"
USER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


# ---------------------------------------------------------------- config ---
def _teams_dir() -> "os.PathLike":
    d = aura_home() / "teams"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path():
    return _teams_dir() / "teams.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        return {}


def my_name(cfg: dict | None = None) -> str:
    """This machine's name on the team: older hubs keep it as `username`,
    Aura's own hub as the signed-in person (`aura_team.person`)."""
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("username") or (cfg.get("aura_team") or {}).get("person") or "")


def save_config(cfg: dict) -> None:
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- release ---
def enabled() -> bool:
    """Teams is off until it's set up: the one function the whole build asks. The
    flag is `teams.enabled` (options, default off) — it flips only when the
    setup step finishes setting up this machine's hub or connecting to one
    (`aura setup teams`).
    """
    from .options import get            # Default is False; no guessing
    try:
        return bool(get("teams.enabled"))
    except Exception:                                # noqa: BLE001
        return False                     # Can't know → behave as not set up


def _machine_key() -> tuple[str, str]:
    """(os-key, arch-key) in Vikunja release-asset naming."""
    s = sys.platform
    osk = ("windows" if s.startswith("win")
           else "darwin" if s == "darwin" else "linux")
    m = platform.machine().lower()
    arch = ("arm64" if m in ("arm64", "aarch64")
            else "386" if m in ("i386", "i686", "x86")
            else "amd64")
    return osk, arch


def latest_release(timeout: float = 20.0) -> dict:
    """The newest official release. Returns the full asset table so the
    offer can be honest about what would be downloaded."""
    req = urllib.request.Request(HUB_RELEASES_API,
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "aura-teams"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8"))
    osk, arch = _machine_key()
    # real names (v2.7.1): vikunja-v2.7.1-linux-amd64-full.zip,
    # …-darwin-10.15-arm64-full.zip, …-windows-4.0-amd64.exe-full.zip — the
    # OS may carry a minimum version, Windows ones say .exe
    pat = re.compile(rf"^vikunja-{re.escape(j['tag_name'])}-{osk}(?:-[\d.]+)?-"
                     rf"{arch}(?:\.exe)?(-full)?\.zip$")
    pick = None
    for a in j.get("assets", []):
        if pat.match(a["name"]):
            # prefer the -full.zip build (self-contained single binary)
            if pick is None or ("-full" in a["name"] and "-full" not in pick["name"]):
                pick = a
    return {
        "tag": j.get("tag_name"),
        "published": j.get("published_at"),
        "url": HUB_PROJECT_URL,
        "asset": pick["name"] if pick else None,
        "download_url": pick["browser_download_url"] if pick else None,
        "size": pick.get("size") if pick else None,
        "digest": (pick.get("digest") or "").split(":", 1)[-1] if pick else None,
    }


def offer_text(rel: dict) -> str:
    """What we tell the user before anything is downloaded."""
    size = f"{rel['size'] / 1e6:.0f} MB" if rel.get("size") else "size unknown"
    return (
        f"Team hub: Vikunja {rel.get('tag')} — works with Vikunja "
        f"({HUB_LICENCE}, source: {HUB_PROJECT_URL}).\n"
        f"Would download the official {rel.get('asset')} ({size}) and check "
        f"its sha256 against the release's published digest.\n"
        f"It runs as its own program, unmodified, data in Aura's folder. "
        f"Download and start it? (yes/no)"
    )


def download_release(rel: dict, dest_dir=None, timeout: float = 600.0) -> dict:
    """Download the official asset, verify sha256 against the published
    digest, unpack the single binary. Refuses anything that fails the
    checksum. Returns where the binary landed."""
    if not rel.get("download_url"):
        raise RuntimeError("no matching release asset for this machine")
    if not rel.get("digest"):
        # Checksum checked — a release without a published digest is not
        # downloaded at all (nothing to check it against)
        raise RuntimeError("the release publishes no sha256 digest for "
                           f"{rel.get('asset')} — not downloading it")
    dest = _teams_dir() / "hub"
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / rel["asset"]
    tmp = zip_path.with_suffix(".part")
    req = urllib.request.Request(rel["download_url"],
                                 headers={"User-Agent": "aura-teams"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != rel["digest"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"checksum mismatch: got {got}, release says {rel['digest']} — "
            f"not keeping the file")
    os.replace(tmp, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        # the zip ships a versioned binary (e.g. vikunja-v2.7.1-linux-amd64),
        # a .sha256 file, config.yml.sample and the LICENSE — pick the binary
        members = [n for n in z.namelist()
                   if os.path.basename(n).startswith("vikunja")
                   and not n.endswith(".sha256")]
        if not members:
            raise RuntimeError("release zip has no vikunja binary inside")
        z.extract(members[0], dest)
    src_bin = dest / members[0]
    bin_path = dest / ("vikunja.exe" if os.name == "nt" else "vikunja")
    os.replace(src_bin, bin_path)
    if os.name != "nt":
        bin_path.chmod(0o755)
    meta = {"tag": rel["tag"], "asset": rel["asset"],
            "sha256": got, "digest": rel.get("digest"),
            "installed": time.strftime("%Y-%m-%d %H:%M"),
            "binary": str(bin_path)}
    (dest / "RELEASE.json").write_text(json.dumps(meta, indent=2) + "\n",
                                       encoding="utf-8")
    return meta


def installed_release() -> dict | None:
    p = _teams_dir() / "hub" / "RELEASE.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        return None


def binary_path() -> "os.PathLike | None":
    meta = installed_release()
    if not meta:
        return None
    p = _teams_dir() / "hub" / "vikunja.exe" if os.name == "nt" \
        else _teams_dir() / "hub" / "vikunja"
    return p if p.exists() else None


# ------------------------------------------------------------------- run ---
def _pid_file():
    return _teams_dir() / "hub" / "hub.pid"


def _alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                               capture_output=True, text=True, timeout=10)
            return str(pid) in r.stdout
        os.kill(pid, 0)
        return True
    except Exception:                                # noqa: BLE001
        return False


def lan_address() -> str:
    """This machine's address on the LAN — what teammates type. A UDP
    socket only picks the outgoing interface; nothing is sent, nothing
    listens."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _hub_env(port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> dict:
    """Vikunja's settings, as its own VIKUNJA_* variables. The public
    address is the one teammates use: Vikunja's web page talks to its API
    through it (config-raw.json, service.publicurl), so `localhost` would
    break the page on every other machine."""
    hub = _teams_dir() / "hub"
    (hub / "data" / "files").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "VIKUNJA_SERVICE_INTERFACE": f"{host}:{port}",
        "VIKUNJA_DATABASE_TYPE": "sqlite",
        "VIKUNJA_DATABASE_PATH": str(hub / "data" / "vikunja.db"),
        "VIKUNJA_FILES_BASEPATH": str(hub / "data" / "files"),
        "VIKUNJA_SERVICE_ENABLEREGISTRATION": "true",
        "VIKUNJA_SERVICE_PUBLICURL": f"http://{lan_address()}:{port}/",
    })
    return env


def hub_port() -> int:
    """The port this machine's hub runs on: the one it was started with
    last (a restore or an update starts it there again), else 3456."""
    return int(load_config().get("hub_port") or DEFAULT_PORT)


def start_hub(port: int | None = None, host: str = "0.0.0.0") -> dict:
    """Start the team hub on the LAN; the team page — incl. chat — is part of it,
    the sign in is the account's name and password.
    """
    binp = binary_path()
    if not binp:
        return {"ok": False, "detail": "no hub binary installed — offer it first"}
    port = int(port or hub_port())
    cfg = load_config()
    if cfg.get("hub_port") != port:
        cfg["hub_port"] = port
        save_config(cfg)
    pf = _pid_file()
    if pf.exists():
        try:
            old = int(pf.read_text().strip() or 0)
        except ValueError:
            old = 0
        if old and _alive(old):
            return {"ok": True, "pid": old, "base": f"http://127.0.0.1:{port}",
                    "address": f"http://{lan_address()}:{port}",
                    "detail": f"already running (pid {old})"}
    hub = _teams_dir() / "hub"
    proc = subprocess.Popen([str(binp)], env=_hub_env(port, host), cwd=str(hub),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    pf.write_text(str(proc.pid), encoding="utf-8")
    base = f"http://127.0.0.1:{port}"
    for _ in range(45):                              # Wait for first answer
        time.sleep(1.0)
        if not _alive(proc.pid):
            pf.unlink(missing_ok=True)
            return {"ok": False, "detail": "hub process died on start"}
        try:
            with urllib.request.urlopen(f"{base}/api/v1/info", timeout=3) as r:
                info = json.loads(r.read().decode("utf-8"))
        except Exception:                            # noqa: BLE001
            continue
        return {"ok": True, "pid": proc.pid, "base": base,
                "address": f"http://{lan_address()}:{port}",
                "version": info.get("version"),
                "listen": f"{host}:{port}",
                # Chat rides the hub page at team/#/chat (— the old standalone
                # LAN chat server is gone)
                "chat": "in the team page"}
    return {"ok": False, "pid": proc.pid, "base": base,
            "detail": "started, but the hub did not answer within 45s — "
                      "ask me again in a minute and I'll look."}


def stop_hub() -> dict:
    pf = _pid_file()
    if not pf.exists():
        return {"ok": True, "detail": "not running (no pid file)"}
    pid = int(pf.read_text().strip() or 0)
    if pid and _alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=15)
            else:
                os.kill(pid, 15)
            time.sleep(1.0)
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "detail": f"could not stop pid {pid}: {e}"}
    pf.unlink(missing_ok=True)
    return {"ok": True,
            "detail": f"stopped pid {pid}" if pid else "not running"}


def hub_status() -> dict:
    st = {"binary": bool(binary_path()),
          "release": (installed_release() or {}).get("tag"),
          "pid_file": _pid_file().exists()}
    cfg = load_config()
    if cfg.get("hub_url"):
        st["hub_url"] = cfg["hub_url"]
        st["reachable"], st["version"] = _probe_info(cfg["hub_url"])
    # Mirror (WIR-08): the status/refresh paths this module owns are the
    # periodic wake — throttled, and a failed push never breaks status
    try:
        mirror_now()
    except Exception:                                # noqa: BLE001
        pass
    return st


MIRROR_EVERY = 300.0


def mirror_now(force: bool = False, throttle: float | None = None) -> dict:
    """'one source of truth for tasks' (the outside hub is the truth; our server
    shows those tasks READ-ONLY) — the producer side (WIR-08): with a wired
    outside hub in use beside our own team server, push the hub's tasks to
    every mirrored project's read-only mirror (api_mirror PUT). The admin's
    Aura pushes; everyone reads. Never raises; throttled because the status
    path calls it often.

    The sidebar's periodic wake (teams_ui._refresh) and the queue flusher live
    outside this module — when those become editable, call teams.mirror_now
    there too (see the change notes for the lines).
    """
    out = {"ok": False, "pushed": 0, "projects": [], "detail": ""}
    cfg = load_config()
    try:
        at = cfg.get("aura_team") or {}
        if cfg.get("hub_kind") != "wired" or not at.get("url"):
            out["detail"] = "no wired outside hub beside our own server"
            return out
        every = MIRROR_EVERY if throttle is None else throttle
        last = float(cfg.get("mirror_ts") or 0)
        if not force and time.time() - last < every:
            out.update(ok=True, detail="the mirror is fresh enough")
            return out
        from . import keystore
        from .teams_wiring import WiredHub, load_in_use
        from .teamserver.teamclient import AuraClient
        hub = WiredHub(load_in_use(), token=keystore.get(TOKEN_KEY) or None)
        c = AuraClient(at)
        if c.sk is None:
            out["detail"] = "this machine's device key is missing"
            return out
        status, data = c.request("GET", "/api/v1/projects")
        if status != 200:
            out["detail"] = f"the server refused the projects list ({status})"
            return out

        def _tasks_from(p) -> str:
            s = p.get("settings") or {}
            if isinstance(s, str):
                try:
                    s = json.loads(s or "{}")
                except ValueError:
                    s = {}
            v = str((s or {}).get("tasks_from") or "")
            return v if v.startswith("wired:") else ""

        targets = [(p, _tasks_from(p)) for p in data.get("projects", [])]
        targets = [(p, src) for p, src in targets if src]
        if not targets:
            out.update(ok=True, detail="no mirrored projects on our server")
            return out
        tasks = hub.list_tasks(done=None)
        rows_source = []
        for t in tasks:
            first = (t.get("assignees") or [{}])[0]
            rows_source.append({"key": str(t.get("id")), "title": str(t.get("title", "")),
                         "status": "closed" if t.get("done") else "open",
                         "project": t.get("project") or t.get("project_id"),
                         "assignee": first.get("username", "")
                                     if isinstance(first, dict) else str(first)})
        errors = []
        for p, src in targets:
            # The settings' tasks_from expects wired:<outside-project-id>;
            # tasks filter to that one.
            their = src[len("wired:"):]
            own_rows = ([r for r in rows_source
                         if str(r.get("project", "")) == their] if their
                        else rows_source)
            status, resp = c.request("PUT", f"/api/v1/projects/{p['id']}/mirror",
                                     {"source": src, "tasks": own_rows})
            if status == 200:
                out["pushed"] += int(resp.get("tasks", 0))
                out["projects"].append(p.get("name") or p.get("slug") or p["id"])
            else:
                errors.append(f"{p.get('name', p['id'])}: "
                              f"{(resp.get('error') or {}).get('message', status)}")
        cfg["mirror_ts"] = time.time()
        save_config(cfg)
        out["ok"] = not errors
        said = ("mirrored " + str(out["pushed"]) + " task(s) to "
                + ", ".join(map(str, out["projects"]))) if out["projects"] else ""
        if errors:
            said += ("; refused: " if said else "refused: ") + "; ".join(errors)
        out["detail"] = said
        return out
    except Exception as e:                           # noqa: BLE001 — status never breaks
        out["detail"] = f"mirror push failed: {str(e)[:120]}"
        return out


def wiring_health_lines() -> list[str]:
    """sidebar/status 'wiring problems': a failing operation — and a RENAMED FIELD
    (told at once, per 'a renamed field is told at once'), distinct from an op
    the hub simply lacks ('not known on this hub', row 9). Always the offer to
    re-wire. [] when nothing to tell.
    """
    try:
        cfg = load_config()
        if cfg.get("hub_kind") != "wired":
            return []
        from . import teams_wiring as W
        bad = W.broken()
    except Exception:                                # noqa: BLE001
        return []
    out = []
    for k, v in sorted(bad.items()):
        if v.get("what") == "field" or "." in k:
            op, _, field = k.partition(".")
            out.append(f"wiring: the hub's {op} answers lost '{field}' — the app "
                       f"renamed it ({v.get('error', '')[:80]}); ask me to re-check "
                       "and fix the wiring")
        else:
            out.append(f"wiring: {k} fails ({v.get('error', '')[:90]}) — ask me to "
                       "re-check and fix the wiring")
    return out


# ---------------------------------------------------------------- connect ---
def _probe_info(base: str, timeout: float = 6.0):
    """(reachable, version) for /api/v1/info. Vikunja answers it without
    any login (routes.go: the no-auth group), so it proves the hub is
    there — never that a token is good."""
    url = base.rstrip("/") + "/api/v1/info"
    req = urllib.request.Request(url, headers={"User-Agent": "aura-teams"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
            return True, j.get("version")
    except Exception as e:                           # noqa: BLE001
        return False, str(e)[:120]


def _probe_token(base: str, token: str, timeout: float = 6.0) -> str:
    """"ok" | "refused" | "unreachable: …" — one call that needs the token:
    the user's tasks, one item (the same permission Aura uses most)."""
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/tasks?per_page=1",
        headers={"User-Agent": "aura-teams", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return "ok"
    except urllib.error.HTTPError as e:
        return "refused" if e.code in (401, 403) else f"unreachable: hub said {e.code}"
    except Exception as e:                           # noqa: BLE001
        return f"unreachable: {str(e)[:120]}"


# ------------------------------------------------- moving the hub ---
def hub_dump(out_dir=None) -> dict:
    """Moving the hub is Vikunja's OWN dump: this only runs its command with
    our data paths and says where the file landed."""
    binp = binary_path()
    if not binp:
        return {"ok": False, "detail": "no hub binary installed"}
    out_dir = Path(out_dir) if out_dir else _teams_dir() / "hub"
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(binp), "dump", "-f", "move.zip"],
                       capture_output=True, text=True, timeout=300,
                       env=_hub_env(hub_port()), cwd=str(out_dir))
    f = out_dir / "move.zip"
    if r.returncode != 0 or not f.exists():
        return {"ok": False,
                "detail": (r.stdout + r.stderr).strip().splitlines()[-1][:150]}
    return {"ok": True, "file": str(f),
            "detail": f"dump written: {f} ({f.stat().st_size // 1024} KB) — "
                      f"I can restore it from {f} — just ask."}


def hub_restore(dump_file) -> dict:
    """Vikunja's own restore, unmodified. It asks the user to type
    "Yes, I understand" and compares the line exactly (pkg/modules/dump/
    restore.go: ReadString, no trimming) — so the answer goes in as bytes:
    text mode would send CR LF on Windows and be refused. A second Enter
    answers its "make changes to the config" pause, when it asks. The hub
    is stopped first and started after."""
    binp = binary_path()
    if not binp:
        return {"ok": False, "detail": "no hub binary installed"}
    stop_hub()
    dump_file = Path(dump_file)
    if not dump_file.exists():
        return {"ok": False, "detail": f"no such dump: {dump_file}"}
    work = _teams_dir() / "hub"
    r = subprocess.run([str(binp), "restore", str(dump_file)],
                       input=b"Yes, I understand\n\n", capture_output=True,
                       timeout=300, env=_hub_env(hub_port()), cwd=str(work))
    said = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0 or "Done restoring dump" not in said:
        last = said.strip().splitlines()[-1:] or ["no output"]
        return {"ok": False, "detail": "restore failed: " + last[0][:150]}
    start = start_hub()
    return {"ok": start.get("ok", False),
            "detail": "restored and the hub is back up at "
                      + start.get("base", "?")}


def _normalize_url(u: str) -> str:
    u = u.strip()
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "http://" + u
    return u.rstrip("/")


def check_username(name: str) -> str:
    """One question, one rule: lowercase, digits, dashes — it becomes the
    git branch name. Returns the cleaned name or raises ValueError."""
    n = name.strip().lower().replace(" ", "-").replace("_", "-")
    n = re.sub(r"[^a-z0-9-]", "", n)
    if not USER_RE.match(n):
        raise ValueError(
            "username must be letters, digits and dashes (it is the branch "
            "name), e.g. asha or ravi-2")
    return n


def server_connect(addr: str, name: str, password: str,
                   device_name_fn=None) -> dict:
    """Plan: connect to OUR hub — the address is the user's own words (the caller
    proves that); the hub's certificate is the one the join flow's open fetch
    reported, announced with the answer; username and password go in the hidden
    box, are used ONCE at the password-check inside POST api/v1/aura/device,
    and are never stored anywhere. What survives: the fresh device key for this
    team.
    """
    import base64, json
    from urllib.parse import urlparse
    import http.client, ssl
    from .teamserver import teamclient as TC
    from .teamserver.tls import fingerprint as _fp
    addr = addr if "://" in addr else "https://" + addr
    u = urlparse(addr)
    if not u.hostname:
        return {"ok": False, "detail": "that address doesn't parse"}
    host, portno = u.hostname, int(u.port or 7443)
    try:                                  # Learn WHAT hub answers here
        c = http.client.HTTPSConnection(host, portno, timeout=8,
                                        context=ssl._create_unverified_context())
        c.request("GET", "/api/v1/info")
        r = c.getresponse()
        info = json.loads(r.read() or b"{}")
        c.close()
    except OSError as e:
        return {"ok": False, "detail": f"the hub didn't answer: {e}"}
    if "fingerprint" not in info or not info.get("protocol"):
        return {"ok": False,
                "detail": f"that isn't an Aura team hub "
                          f"(certificate-side answers differ)"}
    fp = info["fingerprint"]
    # pinned from here on: the hub must answer with THIS certificate
    team_label = str(info.get("team") or u.hostname)
    try:
        sk_obj = TC.new_device_key(team_label)
        import nacl.encoding
        pub = base64.urlsafe_b64encode(
            bytes(sk_obj.verify_key.encode(nacl.encoding.RawEncoder))
            ).decode().rstrip("=")
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "detail": f"the device key for this team didn't "
                                      f"come up: {e}"}
    body = json.dumps({
        "name": name, "password": password, "pubkey": pub,
        "device_name": (device_name_fn() if device_name_fn else "this machine"),
    }).encode()
    try:
        c = TC.Pinned(host, portno, fp, timeout=8)
        c.request("POST", "/api/v1/aura/device", body=body,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
    except OSError as e:
        return {"ok": False, "detail": f"the hub didn't answer: {e}"}
    except TC.CertMismatch as e:
        return {"ok": False, "detail": f"the hub's certificate changed "
                                      f"while I was registering — {e}"}
    if r.status != 200:
        return {"ok": False,
                "detail": data.get("error", {}).get("message",
                                                    f"the hub said {r.status}")}
    me = data.get("me") or {}
    if me.get("must_change"):
        return {"ok": False,
                "detail": "change your password on the hub page first"}
    cfg = load_config()
    cfg["aura_team"] = {"url": addr.rstrip("/"), "alt": [], "fingerprint": fp,
                        "device_id": data["device_id"], "person": me.get("name", name),
                        "team": info.get("team", ""), "team_id": team_label,
                        "role": me.get("team_role", "user")}
    teams_cfgkind = cfg.get("hub_kind")
    if teams_cfgkind is None or teams_cfgkind != "mirror":
        cfg["hub_kind"] = None                     # Our own hub needs no kind
    save_config(cfg)
    return {"ok": True, "team": info.get("team", ""), "person": me.get("name", name),
            "fingerprint": fp,
            "detail": (f"connected — you are {me.get('name', name)} on "
                       f"'{info.get('team', team_label)}'; this machine's key "
                       f"is registered ({data['device_id'][:8]}…)")}


def connect(base_url: str, username: str | None, token: str) -> dict:
    """Check the hub, check the token, store: token in the key store, the rest in
    teams.json. Nothing secret is ever written to the config. The username
    comes from setup; passing one here sets it.
    """
    base = _normalize_url(base_url)
    name = username or load_config().get("username") or ""
    if not name:
        return {"ok": False,
                "detail": "You don't have a team username yet — tell me the name you'd like "
                          "and I'll set it."}
    name = check_username(name)
    ok, ver = _probe_info(base)
    if not ok:
        return {"ok": False,
                "detail": f"hub not reachable at {base}/api/v1/info ({ver})"}
    said = _probe_token(base, token)
    if said == "refused":
        return {"ok": False,
                "detail": "token refused by the hub — make a new API token "
                          "in Vikunja (Settings → API tokens; tasks, task "
                          "comments, labels and projects) and try again"}
    if said != "ok":
        return {"ok": False, "detail": "could not check the token ("
                                       + said.split(": ", 1)[-1] + ")"}
    from . import keystore
    stored = keystore.put(TOKEN_KEY, token)
    if not stored:
        return {"ok": False, "detail": "could not store the token in the "
                                       "key store — nothing saved"}
    cfg = load_config()
    cfg.update({"hub_url": base, "username": name,
                "mode": mode_for(base)})
    save_config(cfg)
    return {"ok": True, "hub": base, "username": name, "mode": mode_for(base),
            "version": ver, "token": "stored in the key store"}


def mode_for(url: str) -> str:
    """An https hub is the online version (you connect, you don't host); plain
    http is the local/LAN hub, which this machine may run.
    """
    return "online" if url.startswith("https://") else "local"


ADMIN_NOTE = ("You hold the hub. Watching the whole team's work is the lead's "
              "job, whoever that is — your own Aura can monitor it for you if "
              "you ask it (ask me to show everyone's open work).")


def token_state() -> dict:
    """Is the stored token still good? Vikunja tokens can expire — when the hub
    says no, say so and ask for a new one. On OUR OWN team server there is no
    hub token at all: this machine's device key is the sign-in — said so, in
    words by hub kind (: what to ASK, never a command).
    """
    cfg = load_config()
    at = cfg.get("aura_team") or {}
    if not cfg.get("hub_url"):
        if at.get("url"):
            try:
                from .teamserver.teamclient import load_device_key
                have = load_device_key(
                    at.get("team_id") or at.get("team") or "") is not None
            except Exception:                        # noqa: BLE001
                have = False
            return {"state": "ok" if have else "no-token", "hub": at.get("url"),
                    "username": at.get("person") or cfg.get("username", ""),
                    "detail": ("signed in with this machine's device key" if have
                               else "this machine's device key is missing — ask "
                                    "me to re-join the team")}
        return {"state": "not-connected",
                "detail": "not connected to a team hub yet — ask me to connect "
                          "you ('connect to the hub at …')"}
    from . import keystore
    token = keystore.get(TOKEN_KEY)
    if not token:
        return {"state": "no-token", "hub": cfg["hub_url"],
                "detail": "no hub token in the key store"}
    if cfg.get("hub_kind") in ("wired", "pending"):
        # any app (teams_wiring): the token is proven by the wiring's own
        # whoami — Vikunja's token probe means nothing there
        if cfg.get("hub_kind") == "pending":
            return {"state": "ok", "hub": cfg["hub_url"], "username": cfg.get("username", ""),
                    "detail": "stored — checked with the wiring"}
        try:
            from .teams_wiring import WiredHub, load_in_use
            who = WiredHub(load_in_use(), token=token).whoami()
            return {"state": "ok", "hub": cfg["hub_url"], "username": cfg.get("username", ""),
                    "detail": f"the hub knows it as {who}"}
        except Exception as e:                       # noqa: BLE001
            refused = "refused the token" in str(e)
            return {"state": "refused" if refused else "unknown", "hub": cfg["hub_url"],
                    "username": cfg.get("username", ""), "detail": str(e)[:160]}
    said = _probe_token(cfg["hub_url"], token)
    if said == "ok":
        return {"state": "ok", "hub": cfg["hub_url"],
                "username": cfg.get("username")}
    if said != "refused":
        return {"state": "unreachable", "hub": cfg["hub_url"],
                "username": cfg.get("username"),
                "detail": "the hub can't be reached now ("
                          + said.split(": ", 1)[-1] + ")"}
    return {"state": "expired", "hub": cfg["hub_url"],
            "detail": "the hub refused the token (it may have expired) — "
                      "make a new token in Vikunja and tell me — I'll connect again"}


def status_line() -> str:
    """Sidebar wording, BY HUB KIND (the audit's Vikunja-era strings fix): on our
    own team server the lines show the address and who holds the hub — what to
    ASK, never a command (; 'the hub starts with this machine's Aura'). Vikunja
    text appears only when the hub kind actually is Vikunja; a wired hub shows
    its app and wiring health.
    """
    cfg = load_config()
    at = cfg.get("aura_team") or {}
    kind = cfg.get("hub_kind") or ("vikunja" if cfg.get("hub_url") else "")
    st = hub_status()
    ts = token_state()
    lines = []
    if at.get("url"):
        try:
            from . import teams_server as TS
            here = bool(TS.running())
        except Exception:                            # noqa: BLE001
            here = False
        host = ("the hub starts with this machine's Aura" if here
                else "the hub runs on a teammate's machine")
        lines.append(f"team server: {at['url']} — team "
                     f"'{at.get('team') or at.get('team_id', '?')}' ({host})")
        lines.append(f"you: {at.get('person') or cfg.get('username', '?')} — "
                     f"{ts.get('detail') or ts['state']}")
    if kind == "vikunja" or (not at.get("url") and not cfg.get("hub_url")):
        if st["binary"]:
            lines.append(f"hub binary: installed ({st['release']})")
        else:
            lines.append("hub binary (Vikunja): not installed — ask me to "
                         "set up a team hub and I'll do it")
    if cfg.get("hub_url"):
        mark = "reachable" if st.get("reachable") else "NOT reachable"
        app = (f" (wired: {cfg.get('hub_app')})"
               if kind == "wired" and cfg.get("hub_app") else "")
        lines.append(f"hub: {st['hub_url']} ({mark}, "
                     f"{cfg.get('mode', 'local')} version){app}")
        lines.append(f"you: {ts.get('username', '?')} — token: {ts['state']}")
    if kind == "wired":
        lines += wiring_health_lines()
    if not cfg.get("hub_url") and not at.get("url"):
        lines.append("connection: not set up — ask me to connect you to the "
                     "team hub ('connect to the hub at …')")
    return "\n".join(lines)


def update_offer() -> tuple[str, dict | None]:
    """New versions are offered on the hub machine, on a yes. Returns (what to
    say, the release to install on a yes — None when the hub is current).
    """
    rel = latest_release()
    cur = installed_release()
    if cur and cur.get("tag") == rel.get("tag"):
        return f"hub is current ({cur['tag']}).", None
    return (f"a new hub version is out: {rel['tag']} "
            f"(you have {cur.get('tag') if cur else 'none'}).\n"
            + offer_text(rel)), rel


def apply_update(rel: dict) -> dict:
    """On the user's yes: a dump first (Vikunja's own), stop, download
    (checksum checked), start again if it was running. The data folder
    stays where it is."""
    was_running = _pid_file().exists()
    backup = hub_dump() if binary_path() else {"ok": False}
    stop_hub()
    meta = download_release(rel)
    out = {"ok": True, "tag": meta["tag"],
           "backup": backup.get("file") if backup.get("ok") else None,
           "detail": f"hub {meta['tag']} installed"}
    if was_running:
        st = start_hub()
        out["ok"] = bool(st.get("ok"))
        out["detail"] = st.get("detail") or f"hub {meta['tag']} is up"
    return out
