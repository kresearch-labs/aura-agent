"""teamserver/teamclient.py — the client side every Aura uses (plan).

Pinned HTTPS: the certificate fingerprint IS the trust. The pin is checked
right after the handshake and BEFORE any byte of the request is sent — a wrong
pin sends nothing and raises CertMismatch, which is always shown to the user:
"The team server's identity changed — someone may be in between. Don't connect
unless your admin moved the server."

Invites that carry `"fp": "ca"` (the online version) use normal certificate
verification instead: system trust store + host name check.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import ssl


class CertMismatch(Exception):
    """The server's certificate does not match the pinned fingerprint."""


PIN_CARD = ("The team server's identity changed — someone may be in between. "
            "Don't connect unless your admin moved the server.")


def _pinned_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # Trust comes from the pin, not a CA
    return ctx


def _ca_ctx() -> ssl.SSLContext:
    """Normal verification for the online version (`"fp": "ca"`)."""
    return ssl.create_default_context()


class Pinned(http.client.HTTPSConnection):
    """HTTPS with the server's certificate pinned by fingerprint."""

    def __init__(self, host, port, fingerprint, timeout: float = 15.0,
                 ca: bool = False):
        super().__init__(host, port, context=_ca_ctx() if ca else _pinned_ctx(),
                         timeout=timeout)
        self._pin = fingerprint
        self._ca = ca

    def connect(self):
        super().connect()
        if self._ca:
            return                       # Normal verification did the work
        der = self.sock.getpeercert(binary_form=True)
        got = "sha256:" + hashlib.sha256(der).hexdigest()
        if not hmac.compare_digest(got, self._pin):
            self.sock.close()
            raise CertMismatch(
                f"the team server's certificate changed (expected "
                f"{self._pin[:19]}…, got {got[:19]}…)")


def protocol_told(server_protocol: int | None, ours: int,
                  already_told: bool) -> str | None:
    """The server's Aura-Protocol header vs ours, in plain words — returned at
    most once per client (the caller flips the flag itself).
    """
    if server_protocol is None or server_protocol == ours or already_told:
        return None
    if server_protocol > ours:
        return (f"the hub is NEWER than this Aura (server protocol "
                f"{server_protocol}, aura protocol {ours}) — update aura; "
                f"server and Aura are the same program and update together")
    return (f"the hub is OLDER than this Aura (server protocol "
            f"{server_protocol}, aura protocol {ours}) — update it "
            f"(I can update it for you — a backup is made first, and only if you say yes)")


class Client:
    """A small wrapper: request(method, path, body) → (status, json)."""

    def __init__(self, host, port, fingerprint, timeout: float = 15,
                 ca: bool = False, protocol: int = 1):
        self.host, self.port = host, port
        self.fp = fingerprint
        self.timeout = timeout
        self.ca = ca
        self.protocol = protocol
        self.server_protocol: int | None = None   # From response headers
        self._protocol_told = False

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None):
        conn = Pinned(self.host, self.port, self.fp, self.timeout, self.ca)
        h = {"Aura-Protocol": str(self.protocol)}
        h.update(headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            server_protocol = resp.getheader("Aura-Protocol")
            try:
                self.server_protocol = (int(server_protocol)
                                        if server_protocol is not None
                                        else self.server_protocol)
            except ValueError:
                pass
            return resp.status, server_protocol, data
        finally:
            conn.close()

    def protocol_tell(self) -> str | None:
        """The once-only note when the hub's protocol differs from ours."""
        note = protocol_told(self.server_protocol, self.protocol,
                             self._protocol_told)
        if note is not None:
            self._protocol_told = True
        return note


# --------------------------------------------------------- signed Aura --
def load_device_key(team_id: str):
    """This Aura's device key for one team: the 32-byte seed lives in the keystore
    — never printed, never sent.
    """
    import base64
    import nacl.signing
    from ..keystore import get
    seed = get(f"team-device:{team_id}")
    if not seed:
        return None
    return nacl.signing.SigningKey(base64.b64decode(seed))


def new_device_key(team_id: str):
    """Made once on first join; the seed saved, the pubkey goes to the server.
    Never printed, never sent.
    """
    import base64
    import nacl.encoding
    import nacl.signing
    from ..keystore import put
    sk = nacl.signing.SigningKey.generate()
    put(f"team-device:{team_id}",
        base64.b64encode(sk.encode(nacl.encoding.RawEncoder)).decode())
    return sk


def sign_request(sk, method: str, path_with_query: str, host: str,
                 body: bytes | None, device_id: str,
                 now: float | None = None) -> dict:
    """The headers for one request."""
    import secrets
    import time as _t
    body = body or b""
    created = str(int(_t.time() if now is None else now))
    nonce = secrets.token_hex(16)
    text = signed_text(method, path_with_query, host, created, nonce,
                       body_hash_of(body))
    sig = sk.sign(text).signature
    return {"Aura-Device": device_id, "Aura-Created": created,
            "Aura-Nonce": nonce, "Aura-Signature": sig_b64(sig)}


def sig_b64(sig: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


class AuraClient:
    """The joined Aura's client: pinned HTTPS + signed requests, built from the
    `aura_team` config and the keystore device key.
    """

    def __init__(self, cfg: dict, sk=None):
        from urllib.parse import urlparse
        from . import PROTOCOL
        self.fp = cfg["fingerprint"]
        self.device_id = cfg["device_id"]
        self.team_id = cfg.get("team_id", cfg["url"])
        self.sk = sk if sk is not None else load_device_key(self.team_id)
        self.protocol = PROTOCOL          # Ours; sent on every request
        self.server_protocol: int | None = None   # Theirs, from responses
        self._protocol_told = False
        self.cfg = cfg
        # Several addresses — the saved one first, then the invite's alternates
        # (LAN IPs + <hostname>[.local]); the first that answers WITH the
        # pinned fingerprint wins (a wrong cert is never accepted, so trying
        # names is safe)
        self.addresses: list[str] = []
        u = urlparse(cfg["url"])
        first = f"{u.hostname or '127.0.0.1'}:{u.port or 7443}"
        for a in [cfg.get("url")] + list(cfg.get("alt") or []) + \
                [f"https://{a}" for a in (cfg.get("lan_addrs") or [])]:
            if not a:
                continue
            pu = urlparse(a if "://" in a else "https://" + a)
            cand = f"{pu.hostname}:{pu.port or 7443}"
            if cand not in self.addresses:
                self.addresses.append(cand)
        if first not in self.addresses:
            self.addresses.insert(0, first)

    def protocol_tell(self) -> str | None:
        """The once-only note when the hub's protocol differs from ours — surfaced
        in `aura teams status`.
        """
        note = protocol_told(self.server_protocol, self.protocol,
                             self._protocol_told)
        if note is not None:
            self._protocol_told = True
        return note

    def get_task_json(self, task_id: int) -> dict | None:
        """GET /tasks/<id> — the raw task dict (for version-carrying
        PATCHes), None when it isn't there."""
        status, data = self.request("GET", f"/api/v1/tasks/{int(task_id)}")
        return data.get("task") if status == 200 else None

    def request(self, method: str, path: str, payload: dict | None = None):
        """Try each address in order; keep the first that answers WITH the pinned
        fingerprint. The pin is checked right after the handshake and BEFORE
        any byte of the request is sent (, same as the join flow's Pinned): a
        host answering with another certificate is refused — never accepted,
        but remembered, so when nothing answers with the pinned fp the caller
        sees CertMismatch (the identity-changed card), not "hub unreachable".
        """
        body = json.dumps(payload).encode() if payload is not None else b""
        last_err = None
        mismatch: CertMismatch | None = None
        # A down hub answers within ~8 s TOTAL, whatever the addresses list
        # holds — the budget shrinks per candidate
        import time as _t
        budget_deadline = _t.monotonic() + 8
        for cand in self.addresses:
            remaining = budget_deadline - _t.monotonic()
            if remaining <= 0:
                break                       # The budget is spent — off is off
            host, _, port = cand.rpartition(":")
            try:
                conn = Pinned(host, int(port), self.fp,
                              timeout=min(6, remaining),
                              ca=self.fp == "ca")
                headers = sign_request(self.sk, method, path, cand, body,
                                       self.device_id)
                headers["Aura-Protocol"] = str(self.protocol)
                if body:
                    headers["Content-Type"] = "application/json"
                    headers["Content-Length"] = str(len(body))
                try:
                    conn.request(method, path, body=body or None,
                                 headers=headers)
                    r = conn.getresponse()
                    raw = r.read()
                    try:
                        sp = r.getheader("Aura-Protocol")
                        if sp is not None:        # Remember the server's
                            self.server_protocol = int(sp)
                    except ValueError:
                        pass
                    try:
                        out = (r.status, json.loads(raw or b"{}"))
                    except ValueError:
                        out = (r.status, {})
                    # Remember the winner first
                    if cand != self.addresses[0]:
                        self.addresses.remove(cand)
                        self.addresses.insert(0, cand)
                    return out
                finally:
                    conn.close()
            except CertMismatch as e:
                mismatch = e          # A wrong cert never wins — keep trying
                last_err = e
                continue
            except (OSError, ssl.SSLError) as e:
                last_err = e
                continue
        if mismatch is not None:
            raise mismatch
        raise ConnectionError(f"the team hub is not reachable at any known "
                              f"address ({last_err})")


from .auth import body_hash_of, signed_text  # noqa: E402


# ------------------------------------------- address notices
def verify_address_notice(notice: str, pinned_fp: str) -> dict | None:
    """Check `aura-address:v1:` notice: the signature must verify against the
    pinned certificate's public key and the fp inside must equal the pinned
    one. Returns {"u", "fp"} or None.
    """
    import base64
    import ssl
    from urllib.parse import urlparse
    if not notice.startswith("aura-address:v1:"):
        return None
    body = notice[len("aura-address:v1:"):]
    payload_b64, _, sig_b64 = body.partition(".")
    try:
        payload = base64.urlsafe_b64decode(
            payload_b64 + "=" * (-len(payload_b64) % 4))
        sig = base64.urlsafe_b64decode(
            sig_b64 + "=" * (-len(sig_b64) % 4))
        j = json.loads(payload)
        if j.get("fp") != pinned_fp:
            return None
        # the signature comes from the pinned cert's key: fetch the cert by
        # connecting (the pin is verified first — a wrong cert never gets
        # past Pinned.connect)
        u = urlparse(j["u"] if "://" in j["u"] else "https://" + j["u"])
        conn = Pinned(u.hostname, u.port or 7443, pinned_fp, timeout=6)
        try:
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True)
        finally:
            conn.close()
        if der is None:
            return None
        cert = __import__("cryptography").x509.load_der_x509_certificate(der)
        pub = cert.public_key()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        pub.verify(sig, payload, ec.ECDSA(hashes.SHA256()))
        return j
    except Exception:                                # noqa: BLE001
        return None
