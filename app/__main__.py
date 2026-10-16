"""Entry point: `python -m app`."""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
import socket

import uvicorn

from . import config

log = logging.getLogger("pokerbalance")


def self_signed() -> tuple[str, str]:
    """Create (once) a self-signed cert in DATA_DIR/tls and log its SHA-256 fingerprint for pinning."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = os.path.join(config.DATA_DIR, "tls")
    os.makedirs(d, exist_ok=True)
    cert_p, key_p = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    if not (os.path.exists(cert_p) and os.path.exists(key_p)):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pokerbalance")])
        now = datetime.datetime.now(datetime.timezone.utc)
        sans = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname()),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        fd = os.open(key_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        with open(cert_p, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(cert_p, "rb") as f:
        der = x509.load_pem_x509_certificate(f.read()).public_bytes(serialization.Encoding.DER)
    fp = hashlib.sha256(der).hexdigest().upper()
    log.warning("TLS: self-signed certificate, SHA-256 fingerprint %s", ":".join(fp[i:i + 2] for i in range(0, 64, 2)))
    return cert_p, key_p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ssl = {}
    if config.TLS == "self-signed":
        cert, key = self_signed()
        ssl = {"ssl_certfile": cert, "ssl_keyfile": key}
    elif config.TLS == "custom":
        if not (config.TLS_CERT and config.TLS_KEY):
            raise SystemExit("POKER_TLS=custom needs POKER_TLS_CERT and POKER_TLS_KEY")
        ssl = {"ssl_certfile": config.TLS_CERT, "ssl_keyfile": config.TLS_KEY}
    else:
        log.warning("TLS is off: only run this behind a TLS-terminating reverse proxy.")
    from .main import create_app
    log.info("Poker Balance %s listening on %s:%s", config.__version__, config.HOST, config.PORT)
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, proxy_headers=False, server_header=False,
                date_header=False, log_level="info", limit_concurrency=200, timeout_keep_alive=15,
                h11_max_incomplete_event_size=16 * 1024, **ssl)


if __name__ == "__main__":
    main()
