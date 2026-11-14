"""sandbox.py — BUILD 4 + 5: OS-level containment for tool subprocesses.

The industry convergence. The fix every vendor shipped: run the agent's
commands inside an OS boundary (Anthropic: Seatbelt; OpenAI: seccomp+landlock;
we: bubblewrap on Linux, with honest fallback).

  - FILESYSTEM: everything read-only EXCEPT the engagement root and /tmp —
    the agent can physically write only where the operator's work lives.
  - NETWORK: `--unshare-net` (default OFF egress) unless the operator
    approves network for the call — BUILD 5. The two boundaries together:
    FS isolation without network isolation = exfiltration path, and
    vice versa.
  - CREDENTIALS (BUILD 6 layer-one): sensitive env vars are stripped from
    the sandboxed subprocess environment, and credential files stay
    read-only under the FS boundary. Full brokering (proxy-held keys)

Fallback honesty: when bubblewrap is absent (or on Windows), commands run
UNSANDBOXED and every result says so — the operator is never left believing a
boundary existed when it didn't. Disable entirely with AURA_SANDBOX=0.

Test-safety: sandbox tests run only in synthetic tmp roots.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

SENSITIVE_ENV_VARS = (
    "GITHUB_TOKEN", "GIT_TOKEN", "GIT_ASKPASS", "NPM_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AURA_API_KEY",
    "GPG_PASSPHRASE", "PYPI_TOKEN",
)
# BUILD 6 notary: SSH_AUTH_SOCK is NOT a credential — it is the sign-request
# socket (the agent asks the key-holder to sign; key material never moves).
# It rides into the sandbox deliberately, per the sign-don't-reveal pattern.
_SANDBOX_DISABLED = "0"


def bwrap_available() -> bool:
    if os.environ.get("AURA_SANDBOX") == _SANDBOX_DISABLED:
        return False
    return shutil.which("bwrap") is not None


# ------------------------------------------------ engine 2: docker -------
_DOCKER_OK: bool | None = None   # Daemon ping cached per process
_DOCKER_IMAGES = ("busybox", "alpine", "debian", "ubuntu")


def docker_available() -> bool:
    """BUILD 4 engine 2: Docker containment. The daemon manages namespaces itself,
    so the AppArmor unprivileged-userns restriction that blocks bwrap does NOT
    block Docker.
    """
    global _DOCKER_OK
    if os.environ.get("AURA_SANDBOX") == _SANDBOX_DISABLED:
        return False
    if _DOCKER_OK is None:
        try:
            r = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
            _DOCKER_OK = r.returncode == 0 and shutil.which("docker") is not None
        except Exception:
            _DOCKER_OK = False
    return _DOCKER_OK


def _sandbox_image() -> str | None:
    """First locally-present image usable as the sandbox base (no pulls from
    the tool path — pulling is an operator/setup action, never implicit)."""
    override = os.environ.get("AURA_SANDBOX_IMAGE")
    wanted = [override] if override else list(_DOCKER_IMAGES)
    for name in wanted:
        r = subprocess.run(
            ["docker", "images", "-q", name], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return name
    return None


def _notary_sockets(extra_env: dict | None) -> list[Path]:
    """BUILD 6 notary: existing agent sockets (sign-request paths, not key
    material) that must be mounted into the containment engine."""
    out = []
    for var in ("SSH_AUTH_SOCK", "GPG_SOCK"):
        v = (extra_env or {}).get(var) or os.environ.get(var)
        if v:
            p = Path(v)
            if p.exists() and stat.S_ISSOCK(p.stat().st_mode):
                out.append(p)
    return out


def docker_argv(cmd: str, engagement_root: Path, cwd: str | None,
                network: bool = False,
                extra_env: dict | None = None) -> tuple[list[str] | None, str, str]:
    """Build the docker containment argv. Returns (argv, note, workdir).
    Mounts: engagement root rw at its REAL path (cwd semantics preserved),
    container rootfs read-only, /tmp tmpfs, notary sockets ro. Files land
    as the operator's uid (never root-owned). argv None = unusable call."""
    if not docker_available():
        return None, "", ""
    img = _sandbox_image()
    if img is None:
        return None, "", ""
    root = Path(engagement_root).resolve()
    workdir = str(root)
    if cwd:
        c = Path(cwd).resolve()
        if c == root or root in c.parents:
            workdir = str(c)          # Cwd inside the mount: honored
        # cwd outside the engagement root would escape the mount — clamp
    argv = [
        "docker", "run", "--rm",
        *(["--user", f"{os.getuid()}:{os.getgid()}"]   # Operator ids, not root
          if hasattr(os, "getuid") else []),           # (Windows has none)
        "--read-only", "--tmpfs", "/tmp",        # Container fs: RO + tmp tmpfs
        "--network", "none" if not network else "bridge",
        "-v", f"{root}:{root}",
        "-w", workdir,
    ]
    for sock in _notary_sockets(extra_env):
        argv += ["-v", f"{sock}:{sock}"]         # Notary: sign socket, no key
    argv += [img, "sh", "-c", cmd]
    return argv, img, workdir


def scrubbed_env() -> dict:
    """BUILD 6 layer-one: credential env vars never enter the sandbox."""
    env = dict(os.environ)
    for k in SENSITIVE_ENV_VARS:
        env.pop(k, None)
    return env


def hidden_paths() -> list[Path]:
    """What a sandboxed command never sees: the folders and files that hold
    keys (outside.py's list) and Aura's own folder (settings, keys, chats)."""
    from .outside import _SECRET_DIRS, _SECRET_FILES
    from .paths import aura_home
    home = Path.home()
    out = [home / d for d in _SECRET_DIRS] + [home / f for f in _SECRET_FILES]
    try:
        out.append(aura_home())
    except Exception:                                  # noqa: BLE001
        pass
    return out


def sandbox_cache_env(env: dict) -> dict:
    """Home is read-only inside the sandbox: tools that keep a cache there
    (pip, npm, uv, anything on XDG) write it under /tmp instead."""
    base = "/tmp/aura-cache"
    for k, v in (("XDG_CACHE_HOME", base), ("PIP_CACHE_DIR", base + "/pip"),
                 ("npm_config_cache", base + "/npm"), ("UV_CACHE_DIR", base + "/uv")):
        env.setdefault(k, v)
    return env


def wrap_argv(cmd: str, engagement_root: Path,
              network: bool = False, die_with_aura: bool = True) -> tuple[list[str] | None, str]:
    """Wrap a shell command line for bubblewrap. Returns (argv, note).
    argv None = sandbox unavailable (caller runs unsandboxed + keeps note)."""
    if not bwrap_available():
        return None, "sandbox: bwrap not available — ran UNSANDBOXED"
    root = Path(engagement_root).resolve()
    argv = [
        "bwrap",
        *(["--die-with-parent"] if die_with_aura else []),   # A command ends with Aura
        "--ro-bind", "/", "/",                     # Fs: read everything...
        "--bind", str(root), str(root),            # ...write ONLY engagement
        "--bind", "/tmp", "/tmp",                  # ...and tmp
        "--dev-bind", "/dev", "/dev",
        "--proc", "/proc",
        "--bind", "/run", "/run",
    ]
    # Home stays readable (read-only, like the rest of the system): programs
    # installed there — a project's.venv, uv's Pythons, ~/.local/bin, nvm,
    # cargo — must run. What holds keys is hidden: the credential folders and
    # files, and Aura's own folder
    hidden = hidden_paths()
    for p in hidden:
        if p.is_dir():
            argv += ["--tmpfs", str(p)]
        elif p.is_file():
            argv += ["--ro-bind", "/dev/null", str(p)]
    if any(h == root or h in root.parents for h in hidden if h.is_dir()):
        argv += ["--bind", str(root), str(root)]   # The project inside one stays usable
    if not network:
        argv.append("--unshare-net")               # BUILD 5: egress off
    argv += ["--", "bash", "-c", cmd]
    return argv, ("sandbox: bwrap (fs=%s, network=%s)"
                  % (root, "on" if network else "off"))


def add_bind(argv: list[str], path: str) -> None:
    """One more read-write mount in a wrap_argv command — after every other mount,
    just before `--`.
    """
    i = argv.index("--")
    argv[i:i] = ["--bind", str(path), str(path)]


def _net_word(network: bool, sites: bool) -> str:
    return "on" if network else ("allowed sites only" if sites else "off")


def run_outside(cmd: str, cwd: str | None, timeout: float,
                extra_env: dict | None = None):
    """Run on the user's computer, outside the safe box — only after they said
    yes (or at L3 with 'Stop it' off). Credentials are still scrubbed from the
    environment; everything it starts still stops with it."""
    from . import sandbox_os as SO
    env = scrubbed_env()
    env.update(extra_env or {})
    if os.name == "nt":
        return SO.run_in_job(cmd, timeout, cwd, env), "ran on your computer, outside the safe box"
    return (SO.run_tree(cmd, shell=True, timeout=timeout, cwd=cwd, env=env),
            "ran on your computer, outside the safe box")


def run_sandboxed(cmd: str, engagement_root: Path, cwd: str | None,
                  timeout: float, network: bool = False,
                  extra_env: dict | None = None, allowed=None):
    """Run a command inside the sandbox. Engine chain: bubblewrap -> docker ->
    plain. extra_env carries BUILD 6 just-in-time entries (askpass paths,
    broker-injected values) + the notary socket var; merged into the child env
    AFTER the credential scrub, never written to sandbox fs by us. Returns
    (completed_process, note) — the note always tells the truth.

    Allowed: with the network not switched on, commands still reach the
    project's allowed sites — through Aura's filtering proxy; the OS sandbox
    lets them reach only the proxy.
    """
    env = scrubbed_env()
    env.update(extra_env or {})
    from . import sandbox_os as SO
    proxy = None
    if allowed is not None and not network:
        from . import netproxy
        proxy = netproxy.get(allowed)
        proxy.take_refused()                       # Count this command's refusals only
        env.update(netproxy.env_for(proxy.port))
    sites = proxy is not None
    if SO.seatbelt_available() and os.environ.get("AURA_SANDBOX") != _SANDBOX_DISABLED:
        # Seatbelt with the network off still allows localhost: the proxy
        proc = SO.run_tree(SO.seatbelt_argv(cmd, engagement_root, network), shell=False,
                           timeout=timeout, cwd=cwd, env=env)
        return proc, ("sandbox: seatbelt (fs=%s, network=%s)"
                      % (Path(engagement_root).resolve(), _net_word(network, sites)))
    argv, note = wrap_argv(cmd, engagement_root, network=network)
    if argv is not None:
        sandbox_cache_env(env)
    if argv is not None:
        for sock in _notary_sockets(extra_env):
            add_bind(argv, sock)                           # Notary mount
        if sites:
            # no network inside: a relay on the sandbox's own 127.0.0.1 passes
            # connections to the proxy through a socket file (Anthropic's
            # sandbox-runtime does the same with socat)
            import shlex
            import sys as _sys
            from .netproxy import RELAY
            sock = proxy.unix_socket(Path("/tmp"))
            add_bind(argv, sock)
            argv[-1] = (f"{shlex.quote(_sys.executable)} -c {shlex.quote(RELAY)} {proxy.port} "
                        f"{shlex.quote(sock)} >/dev/null 2>&1 & sleep 0.2; {argv[-1]}")
            note = note.replace("network=off", "network=allowed sites only")
            from .netproxy import local_through
            local_through(env)
    try:
        if argv is not None:
            proc = SO.run_tree(argv, shell=False, timeout=timeout, cwd=cwd, env=env)
            err = (proc.stderr or "").strip()
            if proc.returncode != 0 and err.startswith("bwrap: "):
                # OS refused bwrap (e.g. AppArmor userns policy) — fall
                # through to the docker engine instead of giving up
                note = (f"sandbox: bwrap refused by OS "
                        f"({err.splitlines()[0][:100]}) — trying docker")
            else:
                return proc, note
        # engine 2 (Linux): Landlock — no install, no admin
        if SO.landlock_abi() and os.environ.get("AURA_SANDBOX") != _SANDBOX_DISABLED:
            pre, abi = SO.landlock_preexec(engagement_root, network,
                                           proxy_port=proxy.port if sites else None)
            if sites and abi >= 4:
                from .netproxy import local_through
                local_through(env)               # Only the proxy's port is open
            proc = SO.run_tree(cmd, shell=True, timeout=timeout, cwd=cwd, env=env,
                               preexec_fn=pre)
            if proc.returncode != 126:
                return proc, ("sandbox: landlock v%d (fs=%s, network=%s)"
                              % (abi, Path(engagement_root).resolve(),
                                 _net_word(network, sites) if abi >= 4 or network
                                 else "NOT limited — kernel too old" + (
                                     "; programs that use the proxy setting reach allowed "
                                     "sites only" if sites else "")))
            note = "sandbox: landlock could not start"
        # engine 2 (Windows): Microsoft's MXC process container — no admin;
        # present only when the user said yes to it in `aura setup safety`
        if os.name == "nt" and os.environ.get("AURA_SANDBOX") != _SANDBOX_DISABLED:
            from . import sandbox_win as SW
            if SW.ready():
                if sites and SW.proxy_supported():
                    from .netproxy import local_through
                    local_through(env)           # Only the proxy is reachable
                wproc, wnote = SW.run(cmd, engagement_root, cwd, timeout, env, network,
                                      proxy.port if sites else None)
                if wproc is not None:
                    return wproc, wnote
                note = wnote
        # engine 3: docker containment
        dargv, img, workdir = docker_argv(cmd, engagement_root, cwd=cwd,
                                          network=network,
                                          extra_env=extra_env)
        if dargv is not None:
            # a named container: on a timeout the container itself is stopped
            # (stopping the docker client alone leaves it running)
            import secrets
            cname = "aura-" + secrets.token_hex(6)
            dargv = dargv[:2] + ["--name", cname] + dargv[2:]
            proc = SO.run_tree(
                dargv, shell=False, timeout=timeout, cwd=cwd, env=env,
                on_timeout=lambda: subprocess.run(["docker", "kill", cname],
                                                  capture_output=True, timeout=20))
            dnote = (f"sandbox: docker[{img}] (fs={engagement_root}, "
                     f"network={'on' if network else 'off'})")
            derr = (proc.stderr or "").strip()
            if proc.returncode != 0 and derr.startswith("docker:"):
                # docker daemon also refused — plain + truthful
                note = (f"sandbox setup FAILED: {derr.splitlines()[0][:120]}"
                        f" — ran UNSANDBOXED")
                proc = SO.run_tree(cmd, shell=True, timeout=timeout, cwd=cwd, env=env)
                return proc, note
            return proc, dnote
        # no engine available — refuse when required, else plain and said
        if SO.required():
            return (subprocess.CompletedProcess(cmd, 126, "",
                    "refused: sandbox.required is on and no OS sandbox is available here "
                    f"({SO.describe()})"), "sandbox: REQUIRED but unavailable — not run")
        if "refused by OS" in note:
            note += " — ran UNSANDBOXED"
        if os.name == "nt":
            proc = SO.run_in_job(cmd, timeout, cwd, env)
            return proc, ("sandbox: none on Windows — ran with your rights in a job object "
                          "(everything it started stops with it); Docker Desktop gives a "
                          "file/network boundary"
                          + ("; network: allowed sites through Aura's proxy for programs that "
                             "use the proxy setting (not enforced here)" if sites else ""))
        proc = SO.run_tree(cmd, shell=True, timeout=timeout, cwd=cwd, env=env)
        return proc, note
    except subprocess.TimeoutExpired:
        raise
