"""Self-signed certificate: make, load, fingerprint.

The card asks; Aura runs the install on a yes; the user types no command.
Nothing installs without the yes (plan rule 5).
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

CERT_FILE = "team-cert.pem"
KEY_FILE = "team-key.pem"
YEARS = 10


def cryptography_ok() -> bool:
    """True when the TLS library imports."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def installer_uv() -> str:
    """The uv that made this venv — the installer's, not a random pip."""
    found = shutil.which("uv")
    if found:
        return found
    for cand in ("~/.local/bin/uv", "~/.cargo/bin/uv"):
        import os
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return "uv"


def install_offer() -> str | None:
    """The card when `cryptography` is missing — None when it is present. Offers
    the install; runs only on the user's yes; the user never types a command.
    """
    if cryptography_ok():
        return None
    return ("Teams needs one extra part (cryptography, about 4 MB) to make its "
            "secure certificate. Install it now?")


def install(timeout: float = 300.0) -> str:
    """On the user's yes: install with the installer's uv. Returns what
    happened, plainly."""
    if cryptography_ok():
        return "cryptography is already installed — nothing to do"
    uv = shutil.which("uv") or installer_uv()
    if not shutil.which(uv) and not uv.startswith("/"):
        return ("Aura's installer couldn't be found, so the part wasn't "
                "installed. Aura can't set up a hub on this computer yet.")
    r = subprocess.run(
        [uv, "pip", "install", "--python", sys.executable, "cryptography"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return (f"the install did not finish (exit {r.returncode}): "
                f"{(r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else 'no output'}")
    return ("cryptography installed" if cryptography_ok()
            else "The part was installed but still doesn't work. "
                 "Aura can't set up a hub on this computer yet.")


# ------------------------------------------------------------- the cert --
def _private_lan_ips() -> list[str]:
    """Every private IPv4 of this machine (never a public address)."""
    import ipaddress
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ipaddress.ip_address(ip).is_private and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out


def ensure_cert(team_dir) -> tuple[Path, Path, str]:
    """Make the team cert once, or load the existing one. Returns (cert_path,
    key_path, fingerprint). EC P-256; subject/issuer CN=aura-team-<8 random
    hex> — no names, no host names (privacy rule); SAN = localhost + the
    loopback and LAN IPs only (, for browsers;: nothing the server makes
    carries personal details — the machine's host name is one, so it is NOT in
    the SAN); 10 years; BasicConstraints(ca=False).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = Path(team_dir)
    cert_p, key_p = d / CERT_FILE, d / KEY_FILE
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p, fingerprint(cert_p.read_bytes())

    key = ec.generate_private_key(ec.SECP256R1())
    cn = "aura-team-" + os.urandom(4).hex()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    # No host names in the SAN — `localhost` and IP addresses only
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost"),
         x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
         x509.IPAddress(ipaddress.ip_address("::1"))]
        + [x509.IPAddress(ipaddress.ip_address(ip))
           for ip in _private_lan_ips()])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365 * YEARS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(san, critical=False)
            .sign(key, hashes.SHA256()))
    d.mkdir(parents=True, exist_ok=True)
    key_p.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    _owner_only(key_p)
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p, fingerprint(cert_p.read_bytes())


def fingerprint(cert_pem: bytes) -> str:
    """`sha256:` + 64 hex of the DER bytes — THE pin."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return "sha256:" + hashlib.sha256(der).hexdigest()


def server_context(cert_path, key_path) -> ssl.SSLContext:
    """TLS ≥ 1.2, server side."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def _owner_only(p: Path) -> None:
    """The key file."""
    os.chmod(p, 0o600)
