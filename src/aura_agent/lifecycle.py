"""lifecycle.py — `aura update` and `aura uninstall`.

Update: from a release (checked against its SHA256SUMS) or a folder, with the
uv the installer placed (install.json says where) — updater.py does the work;
Windows finishes after Aura closes, nothing for the user to run.

Uninstall: removes the launcher, the venv, the repo clone, and (optionally,
with --purge) engagement data under AURA_HOME / ~/.aura. Always prints what it
deleted and what it kept.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _aura_home() -> Path:
    from .paths import aura_home
    return aura_home()


def _launcher() -> Path:
    return Path.home() / ".local" / "bin" / "aura"


def _repo_dir() -> Path:
    return _aura_home() / "aura-agent"


def _venv() -> Path:
    return _aura_home() / "venv"


def layout() -> dict | None:
    """What the installer recorded (install.json), or None (pip / source)."""
    import json
    try:
        # Windows PowerShell 5.1 writes UTF-8 with a BOM
        return json.loads((_aura_home() / "install.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _uv_env(lay: dict) -> dict:
    env = dict(os.environ)
    env.update({"UV_TOOL_DIR": lay.get("tool_dir", ""), "UV_TOOL_BIN_DIR": lay.get("bin_dir", ""),
                "UV_PYTHON_INSTALL_DIR": lay.get("python_dir", ""), "UV_PYTHON_INSTALL_BIN": "0",
                "UV_CACHE_DIR": lay.get("cache_dir", "")})
    return {k: v for k, v in env.items() if v != ""}


def cmd_update(from_dir: str | None = None, check_only: bool = False,
               rollback: bool = False, assume_yes: bool = False, ask=input) -> int:
    """`aura update` (the same steps run when you ask Aura in the chat).

    From a release (installed by the installer from GitHub): read the
    release's SHA256SUMS, say what's new, and only on a yes download the
    package, check its SHA-256, install it. --check only looks;
    --rollback goes back to the newest kept older package. --from FOLDER
    or a git checkout install that folder. Windows: a helper finishes the
    install once Aura has closed — nothing for you to run. Your data and
    settings stay (settings files only hold what you changed)."""
    import sys
    from . import __version__
    from . import updater as U
    lay = layout()
    print()
    if not lay or lay.get("method") != "uv-tool":
        print("update : this Aura wasn't installed by Aura's installer, so it can't update itself")
        print("  (a pip install updates with pip; a container with a newer image)")
        return 1

    def yes(q: str) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            print("  ✗ not changed — no terminal to ask; add --yes to confirm")
            return False
        try:
            return ask(q).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def finish(res: dict) -> int:
        print(("  ✓ " if res["ok"] else "  ✗ ") + res["message"])
        return 0 if res["ok"] else 1

    if rollback:
        t = U.rollback_target()
        if not t:
            print(f"update : no earlier version is kept here (this is {__version__})")
            return 1
        v, wheel = t
        print(f"go back : {__version__} → {v}  (the package kept from before, checksum still matches)")
        if not yes(f"go back to {v}? [y/N] "):
            print("nothing changed.")
            return 1
        return finish(U.install(wheel, v, lay, verb="went back"))

    if from_dir or lay.get("kind") in ("folder", None, "") and not os.environ.get("AURA_RELEASE_URL"):
        src = Path(from_dir or lay.get("source", "")).expanduser().resolve()
        if not (src / "pyproject.toml").exists():
            if from_dir:
                print(f"update : ✗ no Aura at {src}")
                return 1
        else:
            if (src / ".git").exists() and not from_dir:
                print("→ getting the latest source (git pull)...")
                r = subprocess.run(["git", "-C", str(src), "pull", "--ff-only"],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    print("  ✗ git pull failed:", (r.stderr or r.stdout).strip()[:300])
                    return 1
                print("  ✓", ((r.stdout or "").strip().splitlines() or ["up to date"])[0])
            if check_only:
                print(f"update : installed from the folder {src}")
                return 0
            print(f"→ installing Aura from {src} ...")
            res = U.install(src, "the folder's version", lay)
            if res["ok"] and from_dir:
                import json
                lay2 = layout() or lay
                lay2.update({"kind": "folder", "source": str(src)})
                (_aura_home() / "install.json").write_text(json.dumps(lay2, indent=1), encoding="utf-8")
            return finish(res)

    info = U.check(lay)
    if not info["ok"]:
        print(f"update : ✗ {info['error']}")
        return 1
    if not info["newer"]:
        print(f"update : Aura {__version__} is the newest release ✓")
        return 0
    print(f"update : Aura {__version__} → {info['latest']}")
    print(f"  from : {info['base']}")
    print("  check: the package's SHA-256 against the release's SHA256SUMS, before anything is installed")
    print("  stays: your chats, settings, keys and projects")
    if check_only:
        return 0
    if not yes(f"update to {info['latest']}? [y/N] "):
        print("nothing changed.")
        return 1
    try:
        wheel = U.download(info)
    except Exception as e:                               # noqa: BLE001
        print(f"  ✗ {e}")
        return 1
    print(f"  ✓ checked (SHA-256): {wheel.name}")
    return finish(U.install(wheel, info["latest"], lay))


def _remove_after_exit(paths: list[Path]) -> None:
    """Windows: delete these once this process has exited."""
    import tempfile
    lines = ["@echo off", f"set PID={os.getpid()}",
             ":wait",
             'tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul && (timeout /t 1 /nobreak >nul & goto wait)',
             "timeout /t 1 /nobreak >nul"]
    for p in paths:
        lines.append(f'rmdir /s /q "{p}" 2>nul' if p.is_dir() else f'del /f /q "{p}" 2>nul')
    lines.append('del /f /q "%~f0"')
    f = Path(tempfile.gettempdir()) / f"aura-uninstall-{os.getpid()}.cmd"
    f.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    from .updater import detach        # Outlives Aura (leaves the launcher's job)
    detach(["cmd", "/c", str(f)])


def _keychain_entries() -> list[tuple[str, str]]:
    """(service, name) of every key a work profile put in the system
    keychain — they live outside the Aura folder, so deleting the folder
    leaves them behind."""
    out: list[tuple[str, str]] = []
    try:
        from .workprofiles import names as profile_names
        from .paths import profile_dir
        for prof in profile_names():
            meta_p = profile_dir(prof) / "keystore.json"
            try:
                import json
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("backend") == "keyring":
                out += [(f"aura:{prof}", n) for n in meta.get("names") or []]
    except Exception:                              # noqa: BLE001
        pass
    return out


def cmd_uninstall(purge: bool = False, assume_yes: bool = False,
                  ask=input) -> int:
    """Removes the program parts only (launcher, venv, source clone,
    Aura's own bin). The user's data — chats, memory, model setups, keys,
    settings — stays unless --purge, and --purge says exactly what goes
    and asks first (typed 'delete'; --yes for scripts)."""
    import sys
    home_root = _aura_home()
    data_home = Path(os.environ.get("AURA_DATA_HOME", str(home_root)))
    program = [("launcher", _launcher()), ("source clone", _repo_dir()),
               ("venv", _venv()), ("Aura's tools", home_root / "bin")]
    lay = layout()
    if lay and lay.get("method") == "uv-tool":
        # what the installer put in place (install.json)
        exe = Path(lay.get("bin_dir", "")) / ("aura.exe" if os.name == "nt" else "aura")
        program += [("the aura command", exe),
                    ("Aura's environment", Path(lay.get("tool_dir", "") or home_root / "tools")),
                    ("Aura's Python", Path(lay.get("python_dir", "") or home_root / "python")),
                    ("download cache", Path(lay.get("cache_dir", "") or home_root / "cache")),
                    ("install record", home_root / "install.json")]
        program = list(dict.fromkeys(program))
    # the background runner setup may have installed (it would keep
    # starting a program that is gone)
    try:
        from .setup_steps import runner_file, runner_kind
        rf = runner_file(runner_kind())
        if rf is not None:
            program.append(("background runner", rf))
    except Exception:                              # noqa: BLE001
        pass
    present = [(lbl, p) for lbl, p in program if p.exists()]
    keys = _keychain_entries() if purge else []

    print()
    print("this will remove:")
    for lbl, p in present:
        print(f"  • {lbl}: {p}")
    if purge:
        for d in dict.fromkeys([home_root, data_home]):
            if d.exists():
                print(f"  • ALL your Aura data: {d}  (chats, memory, model "
                      "setups, keys, settings)")
        if keys:
            print(f"  • {len(keys)} key(s) in the system keychain")
    if not present and not purge:
        print("  (no program parts found — Aura may have been installed "
              "with pip; remove it with: pip uninstall aura-agent)")
        print(f"your data stays in {data_home}")
        return 0
    if not assume_yes:
        word = "delete" if purge else "yes"
        if not sys.stdin.isatty():
            print("✗ not removed — no terminal to ask; add --yes to confirm")
            return 1
        try:
            ans = ask(f"type '{word}' to go ahead: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != word:
            print("nothing removed.")
            return 1

    removed, kept = [], []
    if any(lbl == "background runner" for lbl, _ in present):
        try:
            from .setup_steps import EXEC, runner_kind, stop_commands
            for argv in stop_commands(runner_kind()):
                EXEC(argv)
        except Exception:                          # noqa: BLE001
            pass
    later = []                      # Windows: files in use by this very run
    for lbl, p in present:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(f"{lbl}: {p}")
        except OSError as e:
            if os.name == "nt":
                later.append((lbl, p))
            else:
                kept.append(f"{lbl}: {p} (could not remove: {e})")
    if later:
        # Windows won't delete the running aura.exe / its Python — a small
        # helper removes them a few seconds after Aura closes
        _remove_after_exit([p for _, p in later])
        removed += [f"{lbl}: {p} (removed a few seconds after Aura closes)" for lbl, p in later]
    if purge:
        if keys:
            gone = 0
            try:
                import keyring
                for svc, n in keys:
                    try:
                        keyring.delete_password(svc, n)
                        gone += 1
                    except Exception:              # noqa: BLE001
                        pass
            except Exception:                      # noqa: BLE001
                pass
            removed.append(f"{gone} key(s) from the system keychain")
            if gone < len(keys):
                kept.append(f"{len(keys) - gone} keychain key(s) (could not "
                            "remove — see your OS keychain, service 'aura:…')")
        for d in dict.fromkeys([home_root, data_home]):
            if d.exists():
                try:
                    shutil.rmtree(d)
                    removed.append(f"data: {d}")
                except OSError as e:
                    if os.name == "nt":            # Files in use by this run
                        _remove_after_exit([d])
                        removed.append(f"data: {d} (the rest removed a few seconds "
                                       "after Aura closes)")
                    else:
                        kept.append(f"data: {d} (could not remove: {e})")
    else:
        kept.append(f"your data: {data_home}  — delete it too with "
                    "'aura uninstall --purge'")

    print()
    if removed:
        print("removed:")
        for r in removed:
            print(f"  • {r}")
    if kept:
        print("kept:")
        for k in kept:
            print(f"  • {k}")
    print()
    print("Aura uninstalled." if removed else "Nothing to uninstall.")
    return 0
