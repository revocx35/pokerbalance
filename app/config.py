"""Settings, all from POKER_* environment variables."""
from __future__ import annotations

import ipaddress
import os

__version__ = "1.1.1"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _networks(value: str) -> list[ipaddress._BaseNetwork]:
    out = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "private":
            out += [ipaddress.ip_network(n) for n in
                    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")]
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise SystemExit(f"POKER_TRUSTED_PROXIES: bad address {part!r}")
    return out


DATA_DIR = os.environ.get("POKER_DATA_DIR", "/data")
HOST = os.environ.get("POKER_HOST", "0.0.0.0")
PORT = _int("POKER_PORT", 8443)
# self-signed (default): generate a cert in DATA_DIR/tls; custom: POKER_TLS_CERT/KEY; off: plain HTTP
# (only behind a TLS-terminating reverse proxy).
TLS = os.environ.get("POKER_TLS", "self-signed").strip().lower()
TLS_CERT = os.environ.get("POKER_TLS_CERT", "")
TLS_KEY = os.environ.get("POKER_TLS_KEY", "")
TRUSTED_PROXIES = _networks(os.environ.get("POKER_TRUSTED_PROXIES", ""))
SESSION_IDLE = _int("POKER_SESSION_IDLE_HOURS", 24 * 7) * 3600
SESSION_MAX = _int("POKER_SESSION_MAX_DAYS", 30) * 86400
# Only for local development over http://localhost: browsers drop Secure cookies on plain HTTP.
COOKIE_SECURE = _bool("POKER_COOKIE_SECURE", True)
DEFAULT_CURRENCY = os.environ.get("POKER_DEFAULT_CURRENCY", "₺")[:8]

if TLS not in ("self-signed", "custom", "off"):
    raise SystemExit("POKER_TLS must be self-signed, custom or off")
