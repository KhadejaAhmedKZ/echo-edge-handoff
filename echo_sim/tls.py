"""Throwaway self-signed certificate so QUIC's mandatory TLS has something to use.

QUIC is encrypted from the first flight; there is no unencrypted mode to fall
back to for a demo. The certificate is generated once per run into a temp dir
and the client is configured not to verify it.
"""
from __future__ import annotations

import datetime
import os
import tempfile
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_CACHE: Tuple[str, str] | None = None


def ensure_cert() -> Tuple[str, str]:
    global _CACHE
    if _CACHE is not None and all(os.path.exists(p) for p in _CACHE):
        return _CACHE

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "echo-edge.local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ECHO prototype"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("echo-edge.local"),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    tmp = tempfile.mkdtemp(prefix="echo-tls-")
    certfile = os.path.join(tmp, "cert.pem")
    keyfile = os.path.join(tmp, "key.pem")
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(keyfile, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    _CACHE = (certfile, keyfile)
    return _CACHE
