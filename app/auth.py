"""Passwords, login throttling, sessions, TOTP and the first-run setup token."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import config, db

log = logging.getLogger("pokerbalance")

# argon2id, RFC 9106 "second recommended" profile (64 MiB, t=3, p=4).
_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))

PASSWORD_MIN = 10
PASSWORD_MAX = 256


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(stored: str | None, pw: str) -> bool:
    """Constant-ish time: unknown users still pay for one argon2 verify."""
    try:
        return _ph.verify(stored or _DUMMY_HASH, pw) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(pw: str) -> str | None:
    if len(pw) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters."
    if len(pw) > PASSWORD_MAX:
        return f"Password must be at most {PASSWORD_MAX} characters."
    return None


def generate_password() -> str:
    # 4 groups of 4 from an unambiguous alphabet: ~82 bits, easy to type on a phone.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


# ---------------------------------------------------------------- throttling


class Throttle:
    """Failed-login limiter per client IP, per account name and globally.

    Accounts lock after 5 consecutive failures; each further lockout doubles
    (15 min, 30 min, ... capped at 24 h). Names that don't exist are tracked the
    same way so lockouts don't reveal which usernames are real.
    """

    IP_MAX, IP_WINDOW = 20, 15 * 60
    ACCOUNT_MAX = 5
    LOCK_BASE, LOCK_CAP = 15 * 60, 24 * 3600
    GLOBAL_MAX, GLOBAL_WINDOW = 200, 60
    MAX_KEYS = 50_000

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.ip_fails: dict[str, list[float]] = {}
        self.accounts: dict[str, dict] = {}
        self.global_fails: list[float] = []

    def _prune(self, now: float) -> None:
        if len(self.ip_fails) > self.MAX_KEYS:
            self.ip_fails = {k: v for k, v in self.ip_fails.items() if v and v[-1] > now - self.IP_WINDOW}
        if len(self.accounts) > self.MAX_KEYS:
            self.accounts = {k: v for k, v in self.accounts.items()
                             if v["until"] > now or v["fails"] or v["lockouts"]}

    def blocked(self, ip: str, account: str) -> float:
        """Seconds until a login attempt is allowed again (0 = allowed)."""
        now = self.clock()
        with self.lock:
            wait = 0.0
            fails = [t for t in self.ip_fails.get(ip, []) if t > now - self.IP_WINDOW]
            self.ip_fails[ip] = fails
            if len(fails) >= self.IP_MAX:
                wait = max(wait, fails[0] + self.IP_WINDOW - now)
            acc = self.accounts.get(account.lower())
            if acc and acc["until"] > now:
                wait = max(wait, acc["until"] - now)
            self.global_fails = [t for t in self.global_fails if t > now - self.GLOBAL_WINDOW]
            if len(self.global_fails) >= self.GLOBAL_MAX:
                wait = max(wait, self.global_fails[0] + self.GLOBAL_WINDOW - now)
            return wait

    def failure(self, ip: str, account: str) -> None:
        now = self.clock()
        with self.lock:
            self._prune(now)
            self.ip_fails.setdefault(ip, []).append(now)
            self.global_fails.append(now)
            acc = self.accounts.setdefault(account.lower(), {"fails": 0, "lockouts": 0, "until": 0.0})
            acc["fails"] += 1
            if acc["fails"] >= self.ACCOUNT_MAX:
                acc["lockouts"] += 1
                acc["fails"] = 0
                acc["until"] = now + min(self.LOCK_CAP, self.LOCK_BASE * 2 ** (acc["lockouts"] - 1))
                log.warning("login locked for account %r after repeated failures", account[:64])

    def success(self, account: str) -> None:
        with self.lock:
            self.accounts.pop(account.lower(), None)


throttle = Throttle()
setup_throttle = Throttle()

# ---------------------------------------------------------------- sessions

COOKIE = "__Host-pb_session" if config.COOKIE_SECURE else "pb_session"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    t = db.now()
    conn.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, last_seen) VALUES (?,?,?,?,?)",
                 (_h(token), user_id, csrf, t, t))
    conn.execute("DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
                 (t - config.SESSION_IDLE, t - config.SESSION_MAX))
    return token, csrf


def load_session(conn: sqlite3.Connection, token: str | None):
    """Returns (session_row, user_row) or None. Expired sessions are deleted."""
    if not token or len(token) > 128:
        return None
    th = _h(token)
    s = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (th,)).fetchone()
    if not s:
        return None
    t = db.now()
    if s["last_seen"] < t - config.SESSION_IDLE or s["created_at"] < t - config.SESSION_MAX:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
        return None
    u = conn.execute("SELECT * FROM users WHERE id = ?", (s["user_id"],)).fetchone()
    if not u or u["disabled"]:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
        return None
    if t - s["last_seen"] > 60:
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (t, th))
    return s, u


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_h(token),))


def delete_user_sessions(conn: sqlite3.Connection, user_id: int, keep_token: str | None = None) -> None:
    if keep_token:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _h(keep_token)))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------- TOTP


def new_totp_secret() -> str:
    return pyotp.random_base32(32)


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Poker Balance")


def totp_check(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """Returns the matched time step (to store, preventing replay) or None."""
    code = (code or "").strip().replace(" ", "")
    if not (len(code) == 6 and code.isdigit()):
        return None
    t = pyotp.TOTP(secret)
    cur = int((time.time() if now is None else now) // 30)
    found = None
    for step in (cur - 1, cur, cur + 1):
        if hmac.compare_digest(t.at(step * 30), code) and step > last_step:
            found = step
    return found


# ---------------------------------------------------------------- first-run setup token

_setup_token: str | None = None


def setup_token_path() -> str:
    return os.path.join(config.DATA_DIR, "setup-token")


def ensure_setup_token(conn: sqlite3.Connection) -> None:
    """If no user exists yet, create a one-time setup token (file + log) that the
    first-run form requires, so a stranger can't claim the admin account first."""
    global _setup_token
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        _setup_token = None
        return
    p = setup_token_path()
    try:
        with open(p) as f:
            _setup_token = f.read().strip() or None
    except OSError:
        _setup_token = None
    if not _setup_token:
        _setup_token = secrets.token_urlsafe(18)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(_setup_token + "\n")
    log.warning("First-run setup: open the web UI and enter this setup token: %s "
                "(also in %s)", _setup_token, p)


def check_setup_token(token: str) -> bool:
    return bool(_setup_token) and hmac.compare_digest(_setup_token.encode(), (token or "").strip().encode())


def clear_setup_token() -> None:
    global _setup_token
    _setup_token = None
    try:
        os.remove(setup_token_path())
    except OSError:
        pass
