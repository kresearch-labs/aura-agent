"""keystore.py — one place per profile for every key Aura holds.

Model API keys, MCP logins, gateway tokens and the credentials commands may use
all live here — never in profiles.json, gateway.json or another plain file. A
config file keeps only a marker ("@keystore") where a key was, so it can be
read, shared or backed up without leaking anything.

  keyring the OS keychain — Windows Credential Manager, macOS Keychain,
           Linux Secret Service. Encrypted by the OS, unlocked by your
           login: nothing to type. The default when one is available.
  vault an AES-256-GCM file (vault.json) with a key made from your
           password (scrypt). Chosen with `aura keys lock`, or for
           machines without a keychain. The password is asked once per
           Needs the `cryptography` package. A forgotten password cannot
           be recovered — the keys are gone.
           there is neither; Aura says so every time it starts.

AURA_KEYSTORE=keyring|vault|file forces one (tests, headless servers).
keystore.json in the profile records the backend and the key NAMES (never
values), because a keychain can't list what Aura put in it.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

MARK = "@keystore"            # What a config file holds where a key was
BACKENDS = ("keyring", "vault", "file")
_META = "keystore.json"
_VAULT = "vault.json"
_FILE = "secrets.json"
_SCRYPT = dict(n=2 ** 15, r=8, p=1)


class KeyStoreError(Exception):
    """The store can't be used right now (locked vault, missing package)."""


# ------------------------------------------------------------------ meta --
def _pdir() -> Path:
    from .paths import profile_dir
    return profile_dir()


def _meta() -> dict:
    try:
        return json.loads((_pdir() / _META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_meta(meta: dict) -> None:
    p = _pdir() / _META
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _owner_only(p: Path) -> None:
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------- keyring ------
def keychain_available() -> bool:
    try:
        import keyring
        from keyring.backends import fail, null
        kr = keyring.get_keyring()
        bad = (fail.Keyring, null.Keyring)
        if isinstance(kr, bad):
            return False
        subs = getattr(kr, "backends", None)       # Chainer
        if subs is not None:
            return any(not isinstance(b, bad) for b in subs)
        return True
    except Exception:                              # noqa: BLE001
        return False


def _service() -> str:
    from .paths import profile_name
    return f"aura:{profile_name()}"


# ------------------------------------------------------------ vault ------
_KEYS: dict[str, bytes] = {}          # Derived vault key per profile folder


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise KeyStoreError(
            "the password vault needs the `cryptography` package — "
            "pip install cryptography") from e
    return AESGCM


def _derive(password: str, salt: bytes) -> bytes:
    import hashlib
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32,
                          maxmem=128 * 1024 * 1024, **_SCRYPT)


def _ask_password(prompt: str) -> str:
    pw = os.environ.get("AURA_VAULT_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        raise KeyStoreError("the key vault is locked — run in a terminal, "
                            "or set AURA_VAULT_PASSWORD for unattended runs")
    import getpass
    return getpass.getpass(prompt)


def _vault_read() -> dict:
    p = _pdir() / _VAULT
    if not p.exists():
        return {}
    AESGCM = _aesgcm()
    blob = json.loads(p.read_text(encoding="utf-8"))
    salt = base64.b64decode(blob["salt"])
    nonce = base64.b64decode(blob["nonce"])
    ct = base64.b64decode(blob["ct"])
    key = _KEYS.get(str(p))
    for _try in range(3):
        if key is None:
            key = _derive(_ask_password("Aura key vault password: "), salt)
        try:
            data = AESGCM(key).decrypt(nonce, ct, b"aura-vault-v1")
            _KEYS[str(p)] = key
            return json.loads(data.decode("utf-8"))
        except Exception:                          # noqa: BLE001 (InvalidTag)
            key = None
            if os.environ.get("AURA_VAULT_PASSWORD") or not sys.stdin.isatty():
                break
            print("  wrong password")
    raise KeyStoreError("wrong vault password — the keys stay locked")


def _vault_write(entries: dict, password: str | None = None) -> None:
    AESGCM = _aesgcm()
    p = _pdir() / _VAULT
    key = _KEYS.get(str(p))
    salt = None
    if p.exists() and key is not None and password is None:
        salt = base64.b64decode(
            json.loads(p.read_text(encoding="utf-8"))["salt"])
    if key is None or password is not None or salt is None:
        salt = os.urandom(16)
        if password is None:
            password = _ask_password("Aura key vault password: ")
        key = _derive(password, salt)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(entries).encode("utf-8"),
                             b"aura-vault-v1")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "v": 1, "cipher": "AES-256-GCM", "kdf": "scrypt", **_SCRYPT,
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode()}), encoding="utf-8")
    _owner_only(tmp)
    tmp.replace(p)
    _KEYS[str(p)] = key


# ------------------------------------------------------------- file ------
def _file_read() -> dict:
    try:
        return json.loads((_pdir() / _FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _file_write(entries: dict) -> None:
    p = _pdir() / _FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    _owner_only(p)


# ---------------------------------------------------------- backend ------
def backend() -> str:
    env = (os.environ.get("AURA_KEYSTORE") or "").lower()
    if env in BACKENDS:
        return env
    chosen = _meta().get("backend")
    if chosen in BACKENDS:
        return chosen
    if (_pdir() / _VAULT).exists():
        return "vault"
    return "keyring" if keychain_available() else "file"


def describe(name: str | None = None) -> str:
    b = name or backend()
    return {"keyring": "the OS keychain",
            "vault": "the password vault (AES-256)",
            "file": "a plain owner-only file (no keychain found)"}[b]


_WARNED = False


def warn_if_plain() -> str | None:
    """One line to show at start when keys sit in a plain file. When an
    OS keychain has become available since, offer once (with a keyboard)
    to move them there; the answer is remembered."""
    global _WARNED
    if (_WARNED or backend() != "file" or not names()
            or os.environ.get("AURA_KEYSTORE")):
        return None
    _WARNED = True
    if keychain_available():
        meta = _meta()
        if sys.stdin.isatty() and not meta.get("keychain_offered"):
            meta["keychain_offered"] = True
            _save_meta(meta)
            try:
                ans = input(f"  your {len(names())} key(s) are in a plain "
                            "file, and an OS keychain is now available — "
                            "move them there? [Y/n] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            if ans in ("", "y", "yes"):
                try:
                    n = move_all("keyring")
                    return f"moved {n} key(s) into the OS keychain"
                except KeyStoreError as e:
                    return f"keys not moved: {e}"
        return ("keys are in a plain file — `aura keys move-to-keychain` "
                "puts them in the OS keychain")
    return ("keys are in a plain file (no OS keychain found) — "
            "`aura keys lock` puts them in a password vault")


# --------------------------------------------------------------- API ------
def names() -> list[str]:
    return list(_meta().get("names") or [])


def _index(name: str, add: bool) -> None:
    meta = _meta()
    ns = [n for n in (meta.get("names") or []) if n != name]
    if add:
        ns.append(name)
    meta["names"] = sorted(ns)
    meta.setdefault("backend", backend())
    _save_meta(meta)


def get(name: str) -> str | None:
    """The value, or None when there is none (or the store is locked —
    then it says so once)."""
    b = backend()
    try:
        if b == "keyring":
            import keyring
            return keyring.get_password(_service(), name)
        if b == "vault":
            return _vault_read().get(name)
        return _file_read().get(name)
    except KeyStoreError as e:
        _say_once(str(e))
        return None
    except Exception as e:                         # noqa: BLE001
        _say_once(f"key store unreadable: {e}")
        return None


def put(name: str, value: str) -> bool:
    """Store a key and read it back; True only when it is really there."""
    b = backend()
    if b == "keyring":
        import keyring
        keyring.set_password(_service(), name, value)
    elif b == "vault":
        entries = _vault_read()
        entries[name] = value
        _vault_write(entries)
    else:
        entries = _file_read()
        entries[name] = value
        _file_write(entries)
    _index(name, True)
    return get(name) == value


def remove(name: str) -> None:
    b = backend()
    try:
        if b == "keyring":
            import keyring
            try:
                keyring.delete_password(_service(), name)
            except Exception:                      # noqa: BLE001 (not there)
                pass
        elif b == "vault":
            entries = _vault_read()
            if entries.pop(name, None) is not None:
                _vault_write(entries)
        else:
            entries = _file_read()
            if entries.pop(name, None) is not None:
                _file_write(entries)
    finally:
        _index(name, False)


_SAID: set[str] = set()


def _say_once(msg: str) -> None:
    if msg in _SAID:
        return
    _SAID.add(msg)
    try:
        from . import ui
        ui.warn(msg)
    except Exception:                              # noqa: BLE001
        print(f"  [!] {msg}", file=sys.stderr)


# ------------------------------------------------ config-file helpers -----
def seal(obj, prefix: str, fields: set[str]) -> bool:
    """Move every plain key in `obj` (fields named in `fields`, at any
    depth) into the store; the file keeps MARK in its place. "${VAR}"
    references stay as they are. Returns True when something moved. A key
    that can't be stored safely stays where it was (never lost)."""
    moved = False
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            path = f"{prefix}.{k}"
            if k in fields and isinstance(v, str) and v and v != MARK \
                    and not v.startswith("${"):
                try:
                    ok = put(path, v)
                except Exception as e:             # noqa: BLE001
                    _say_once(f"could not move a key into "
                              f"{describe()}: {e}")
                    ok = False
                if ok:
                    obj[k] = MARK
                    moved = True
            elif isinstance(v, (dict, list)):
                moved = seal(v, path, fields) or moved
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            moved = seal(v, f"{prefix}.{i}", fields) or moved
    return moved


def unseal(obj, prefix: str):
    """A copy of `obj` with every MARK replaced by its key (None when the
    store has none)."""
    if isinstance(obj, dict):
        return {k: (get(f"{prefix}.{k}") if v == MARK
                    else unseal(v, f"{prefix}.{k}"))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [unseal(v, f"{prefix}.{i}") for i, v in enumerate(obj)]
    return obj


# ------------------------------------------------- switching backends -----
def move_all(to: str, password: str | None = None) -> int:
    """Move every key to another backend, checking each one arrived
    before removing it from the old place. Returns how many moved."""
    if to not in BACKENDS:
        raise KeyStoreError(f"unknown key store '{to}'")
    src = backend()
    if src == to:
        return 0
    if to == "keyring" and not keychain_available():
        raise KeyStoreError("no OS keychain on this machine")
    if to == "vault":
        _aesgcm()                                  # Fail before touching keys
    values = {n: get(n) for n in names()}
    missing = [n for n, v in values.items() if v is None]
    if missing:
        raise KeyStoreError(f"can't read {len(missing)} key(s) from "
                            f"{describe(src)} — nothing was moved")
    old_env = os.environ.pop("AURA_KEYSTORE", None)
    meta = _meta()
    try:
        meta["backend"] = to
        _save_meta(meta)
        if to == "vault":
            _vault_write(values, password)
        else:
            for n, v in values.items():
                put(n, v)
        bad = [n for n, v in values.items() if get(n) != v]
        if bad:
            raise KeyStoreError(f"{len(bad)} key(s) did not arrive — "
                                "they stay where they were")
    except Exception:
        meta["backend"] = src
        _save_meta(meta)
        raise
    finally:
        if old_env:
            os.environ["AURA_KEYSTORE"] = old_env
    # everything arrived — now clear the old place
    meta = _meta()
    meta["backend"] = src
    _save_meta(meta)
    for n in values:
        try:
            if src == "keyring":
                import keyring
                keyring.delete_password(_service(), n)
        except Exception:                          # noqa: BLE001
            pass
    if src == "vault":
        (_pdir() / _VAULT).unlink(missing_ok=True)
        _KEYS.pop(str(_pdir() / _VAULT), None)
    elif src == "file":
        (_pdir() / _FILE).unlink(missing_ok=True)
    meta["backend"] = to
    meta["names"] = sorted(values)
    _save_meta(meta)
    return len(values)


# --------------------------------------------------------- aura keys -----
def _label(n: str) -> str:
    if n.startswith("profiles.json.profiles."):
        setup = n[len("profiles.json.profiles."):].rsplit(".", 1)[0]
        return f"model setup '{setup}'"
    if n.startswith("profiles.json.mcp_servers."):
        return f"MCP server '{n.split('.')[3]}'"
    if n.startswith("mcp-token:"):
        return f"MCP login {n[len('mcp-token:'):].split('::')[0]}"
    if n.startswith("gateway.json"):
        return f"gateway {n.split('.', 2)[-1]}"
    if n.startswith("cmd:"):
        return f"command credential {n[4:]}"
    return n


def _read_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\r\n")


def command(args) -> int:
    """aura keys [list] | set <NAME> | remove <NAME> | lock | unlock"""
    from .paths import profile_name
    action = getattr(args, "keys_cmd", None) or "list"
    try:
        if action == "list":
            print(f"keys of profile '{profile_name()}' — kept in "
                  f"{describe()} (values are never shown)")
            ns = names()
            for n in ns:
                print(f"  • {_label(n)}")
            if not ns:
                print("  (none)")
            if backend() == "file":
                print("  ⚠ plain file — `aura keys lock` puts them in a "
                      "password vault")
            return 0
        if action == "set":
            v = _read_secret(f"value for {args.name} (not shown): ")
            if not v:
                print("nothing entered — unchanged")
                return 1
            ok = put("cmd:" + args.name, v)
            print(f"stored {args.name} in {describe()}" if ok
                  else "could not store it")
            return 0 if ok else 1
        if action == "remove":
            full = args.name if ":" in args.name or "." in args.name \
                else "cmd:" + args.name
            if full not in names():
                print(f"no key '{args.name}' — aura keys list")
                return 1
            remove(full)
            print(f"removed {_label(full)}")
            return 0
        if action == "lock":
            if backend() == "vault":
                print("keys are already in the password vault")
                return 0
            print("The vault keeps your keys AES-256 encrypted with a "
                  "password.\nYou type it once each time Aura starts; "
                  "unattended runs need AURA_VAULT_PASSWORD.\nA forgotten "
                  "password can't be recovered — the keys are lost.")
            pw = _read_secret("new vault password: ")
            if len(pw) < 8:
                print("use at least 8 characters — unchanged")
                return 1
            if sys.stdin.isatty() and _read_secret("again: ") != pw:
                print("the two don't match — unchanged")
                return 1
            n = move_all("vault", pw)
            print(f"moved {n} key(s) into the password vault")
            return 0
        if action == "move-to-keychain":
            if backend() == "keyring":
                print("keys are already in the OS keychain")
                return 0
            if backend() == "vault":
                print("keys are in the password vault — `aura keys unlock` "
                      "moves them to the keychain")
                return 1
            n = move_all("keyring")
            print(f"moved {n} key(s) into the OS keychain; the plain file "
                  "is gone")
            return 0
        if action == "unlock":
            if backend() != "vault":
                print("keys are not in the password vault")
                return 0
            to = "keyring" if keychain_available() else "file"
            n = move_all(to)
            print(f"moved {n} key(s) to {describe(to)}; the vault is gone")
            return 0
    except KeyStoreError as e:
        print(f"✗ {e}")
        return 1
    print(f"unknown action {action!r}")
    return 1
