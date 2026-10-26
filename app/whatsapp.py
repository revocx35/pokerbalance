"""Sends game results to WhatsApp through a wa_logger API token (see wa_logger "API tokens").

The token can only list the chats the wa_logger owner picked and send text to them. It is stored encrypted at rest
(AES-GCM, key file next to the database), never returned by the API and never logged. Outbound calls verify the
server certificate (or pin its SHA-256 fingerprint for a self-signed LAN server), never follow redirects and have
short timeouts and a size cap.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import random
import re
import socket
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, db

TIMEOUT = 10
MAX_RESPONSE = 256 * 1024
CHAT_ID_RE = re.compile(r"^[^\s/?#]{3,200}@[A-Za-z.]{2,20}$")
MODES = ("off", "group", "dm")
_AAD = b"pokerbalance/whatsapp-token/v1"


class WaError(Exception):
    """A failure whose message is safe to show to the admin."""


# ---------------------------------------------------------------- secret storage


def _key() -> bytes:
    path = os.path.join(config.DATA_DIR, "wa.key")
    try:
        with open(path, "rb") as f:
            key = f.read()
    except FileNotFoundError:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
        with open(path, "rb") as f:
            key = f.read()
    if len(key) != 32:
        raise WaError("The WhatsApp key file is damaged.")
    return key


def seal(token: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key()).encrypt(nonce, token.encode(), _AAD)


def unseal(blob: bytes) -> str:
    try:
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], _AAD).decode()
    except Exception:
        raise WaError("The saved WhatsApp token can't be read. Save the connection again.") from None


# ---------------------------------------------------------------- configuration


def normalize_url(raw: str) -> str:
    """https://host[:port] only: no credentials, path, query or fragment."""
    try:
        u = urlsplit(raw.strip())
        port = u.port
    except ValueError:
        raise WaError("That address isn't valid.") from None
    if u.scheme != "https":
        raise WaError("Use an https:// address. The token is a secret and must not travel unencrypted.")
    if not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
        raise WaError("Use just the server address, like https://host or https://host:8443.")
    host = u.hostname.lower()
    if not re.fullmatch(r"[a-z0-9.\-]+|[0-9a-f:]+", host):
        raise WaError("That address isn't valid.")
    shown = f"[{host}]" if ":" in host else host
    return f"https://{shown}" + (f":{port}" if port and port != 443 else "")


def normalize_pin(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    p = re.sub(r"[\s:]", "", raw).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", p):
        raise WaError("The certificate fingerprint must be 64 hex characters (SHA-256).")
    return p


def get_config(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT * FROM wa_config WHERE id = 1").fetchone()
    return dict(r) if r else None


def public_config(conn: sqlite3.Connection) -> dict:
    c = get_config(conn)
    if not c:
        return {"connected": False, "base_url": None, "pinned": False}
    return {"connected": True, "base_url": c["base_url"], "pinned": bool(c["pin"])}


def save_config(conn: sqlite3.Connection, base_url: str, token: str | None, pin: str | None) -> None:
    url = normalize_url(base_url)
    pin = normalize_pin(pin)
    old = get_config(conn)
    if token is None:
        if not old:
            raise WaError("Paste the API token from wa_logger.")
        blob = old["token_enc"]
    else:
        if not re.fullmatch(r"walt_\d{1,5}_[A-Za-z0-9_-]{43}", token.strip()):
            raise WaError("That doesn't look like a wa_logger API token.")
        blob = seal(token.strip())
    with db.tx(conn):
        conn.execute("INSERT INTO wa_config(id, base_url, token_enc, pin) VALUES (1, ?, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET base_url = excluded.base_url, token_enc = excluded.token_enc, "
                     "pin = excluded.pin", (url, blob, pin))


def delete_config(conn: sqlite3.Connection) -> None:
    with db.tx(conn):
        conn.execute("DELETE FROM wa_config WHERE id = 1")
        conn.execute("UPDATE groups SET wa_mode = 'off', wa_chat_id = NULL")
        conn.execute("UPDATE users SET wa_chat_id = NULL")


# ---------------------------------------------------------------- http


def _open(host: str, port: int) -> socket.socket:
    """TCP connection to the first reachable allowed address of [host]."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise WaError("Can't find that server.") from None
    allowed = False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Private LAN addresses are the normal case; refuse only addresses that are never a service
        # (this also stops a hostname from pointing at a cloud metadata endpoint).
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            continue
        allowed = True
        try:
            return socket.create_connection((str(ip), port), timeout=TIMEOUT)
        except OSError:
            continue
    raise WaError("Can't connect to that server." if allowed else "That address isn't allowed.")


def _connect(base_url: str, pin: str | None) -> tuple[ssl.SSLSocket, str, int]:
    u = urlsplit(base_url)
    host, port = u.hostname, u.port or 443
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if pin:  # self-signed server: trust exactly this certificate instead of a CA
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.load_default_certs()
    raw = _open(host, port)
    try:
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except ssl.SSLCertVerificationError:
        raw.close()
        fp = _presented_fingerprint(host, port)
        hint = f" If this is your server, enter its fingerprint to trust it: {fp}" if fp else ""
        raise WaError("The server's certificate isn't trusted." + hint) from None
    except (ssl.SSLError, OSError):
        raw.close()
        raise WaError("Couldn't make a secure connection to the server.") from None
    if pin and not hmac.compare_digest(hashlib.sha256(sock.getpeercert(True) or b"").hexdigest(), pin):
        sock.close()
        raise WaError("The server's certificate doesn't match the saved fingerprint.")
    return sock, host, port


def _presented_fingerprint(host: str, port: int) -> str | None:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with _open(host, port) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                return hashlib.sha256(s.getpeercert(True) or b"").hexdigest()
    except (OSError, WaError):
        return None


def _request(cfg: dict, method: str, path: str, body: dict | None = None) -> dict:
    token = unseal(cfg["token_enc"])
    sock, host, port = _connect(cfg["base_url"], cfg["pin"])
    try:
        conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT)
        conn.sock = sock
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()  # redirects are never followed
            data = resp.read(MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException):
            raise WaError("The WhatsApp server didn't answer.") from None
        if len(data) > MAX_RESPONSE:
            raise WaError("The WhatsApp server sent too much data.")
        try:
            parsed = json.loads(data) if data else {}
        except ValueError:
            parsed = {}
        if resp.status in (401, 403):
            raise WaError("The WhatsApp server rejected the token (revoked or wrong).")
        if resp.status == 404:
            raise WaError("That chat isn't allowed for this token.")
        if resp.status == 429:
            raise WaError("The WhatsApp server says slow down. Try again in a minute.")
        if resp.status == 503:
            raise WaError("WhatsApp isn't connected on the server right now.")
        if resp.status >= 300:
            msg = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
            raise WaError(f"The WhatsApp server refused the request ({resp.status}): {str(msg or '')[:120]}")
        return parsed if isinstance(parsed, dict) else {}
    finally:
        sock.close()


def destinations(cfg: dict) -> list[dict]:
    out = []
    for d in _request(cfg, "GET", "/api/v1/destinations").get("destinations", []):
        if isinstance(d, dict) and isinstance(d.get("chatId"), str) and CHAT_ID_RE.match(d["chatId"]):
            out.append({"chat_id": d["chatId"], "kind": "group" if d.get("kind") == "group" else "user",
                        "name": str(d.get("name") or d["chatId"].split("@")[0])[:100]})
    return out


def send_text(cfg: dict, chat_id: str, text: str) -> None:
    if not CHAT_ID_RE.match(chat_id):
        raise WaError("That chat id isn't valid.")
    _request(cfg, "POST", "/api/v1/send", {"chatId": chat_id, "text": text[:4000]})


# ---------------------------------------------------------------- messages


def _money(cents: int, cur: str) -> str:
    a = abs(cents)
    s = f"{a // 100:,}" if a % 100 == 0 else f"{a / 100:,.2f}"
    return ("−" if cents < 0 else "") + cur + s


def _signed(cents: int, cur: str) -> str:
    return ("+" if cents > 0 else "") + _money(cents, cur)


def _clean(s: str) -> str:
    """Names are free text: no control characters and no WhatsApp formatting marks that would garble the message."""
    return re.sub(r"[\x00-\x1f\x7f*_~`]", "", s).strip()[:40]


def _game_data(conn: sqlite3.Connection, game_id: int) -> dict:
    g = conn.execute("SELECT g.*, gr.name AS group_name, gr.currency, gr.wa_mode, gr.wa_chat_id "
                     "FROM games g JOIN groups gr ON gr.id = g.group_id WHERE g.id = ?", (game_id,)).fetchone()
    if not g or g["status"] != "finished":
        raise WaError("The game isn't finished.")
    players = conn.execute(
        "SELECT gp.*, u.display_name AS name, u.wa_chat_id FROM game_players gp JOIN users u ON u.id = gp.user_id "
        "WHERE gp.game_id = ?", (game_id,)).fetchall()
    transfers = conn.execute("SELECT * FROM transfers WHERE game_id = ? ORDER BY amount DESC, id", (game_id,)).fetchall()
    return {"game": g, "players": sorted(players, key=lambda p: (p["place"] or 99, p["name"].lower())),
            "transfers": transfers}


def _results_text(d: dict) -> list[str]:
    g, cur = d["game"], d["game"]["currency"]
    pot = sum(p["buy_ins"] for p in d["players"]) * g["buy_in"]
    lines = [f"♠️ *{_clean(g['group_name'])} · {_clean(g['name'])}*",
             f"Pot {_money(pot, cur)} · {len(d['players'])} players" + (" · deal" if g["finish_kind"] == "deal" else ""), ""]
    for p in d["players"]:
        net = (p["payout"] or 0) - p["buy_ins"] * g["buy_in"]
        lines.append(f"{p['place']}. {_clean(p['name'])}: {_money(p['payout'] or 0, cur)} ({_signed(net, cur)})")
    return lines


def balances_text(group_name: str, currency: str, plan: list[dict]) -> str:
    """The group's combined unpaid payments (the "Still to pay" card) as one message."""
    lines = [f"♠️ *{_clean(group_name)} · Still to pay*", ""]
    if not plan:
        return "\n".join(lines[:1] + ["Everyone is square. 🎉"])
    lines += [f"{_clean(t['from_name'])} → {_clean(t['to_name'])}: {_money(t['amount'], currency)}" for t in plan]
    return "\n".join(lines)


def build_messages(conn: sqlite3.Connection, game_id: int) -> tuple[list[tuple[str, str]], list[str]]:
    """([(chat_id, text)], [names that can't be messaged]) for the group's mode."""
    d = _game_data(conn, game_id)
    g, cur = d["game"], d["game"]["currency"]
    name = {p["user_id"]: _clean(p["name"]) for p in d["players"]}
    base = _results_text(d)
    if g["wa_mode"] == "group":
        if not g["wa_chat_id"]:
            raise WaError("Pick the WhatsApp group for this group first.")
        lines = list(base)
        if d["transfers"]:
            lines += ["", "*Who pays whom*"]
            lines += [f"{name[t['from_user']]} → {name[t['to_user']]}: {_money(t['amount'], cur)}" for t in d["transfers"]]
        else:
            lines += ["", "Nobody owes anything."]
        return [(g["wa_chat_id"], "\n".join(lines))], []
    if g["wa_mode"] == "dm":
        out, skipped = [], []
        for p in d["players"]:
            if not p["wa_chat_id"]:
                skipped.append(p["name"])
                continue
            mine = []
            for t in d["transfers"]:
                if t["from_user"] == p["user_id"]:
                    mine.append(f"You pay {name[t['to_user']]}: {_money(t['amount'], cur)}")
                elif t["to_user"] == p["user_id"]:
                    mine.append(f"{name[t['from_user']]} pays you: {_money(t['amount'], cur)}")
            lines = base + ["", "*Your payments*"] + (mine or ["You're square, nothing to pay."])
            out.append((p["wa_chat_id"], "\n".join(lines)))
        return out, skipped
    raise WaError("WhatsApp messages are off for this group.")


# ---------------------------------------------------------------- dispatch (background)

_running: set[int] = set()
_lock = threading.Lock()


def _record(conn: sqlite3.Connection, game_id: int, ok: bool, detail: str) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO wa_sends(game_id, at, ok, detail) VALUES (?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET "
                     "at = excluded.at, ok = excluded.ok, detail = excluded.detail", (game_id, db.now(), int(ok), detail[:300]))


def run(game_id: int, pause=lambda: time.sleep(random.uniform(2.0, 4.0))) -> None:
    """Sends one game's results and records the outcome. Never raises."""
    conn = db.connect()
    sent = 0
    try:
        try:
            cfg = get_config(conn)
            if not cfg:
                raise WaError("WhatsApp isn't connected.")
            messages, skipped = build_messages(conn, game_id)
            note = " No WhatsApp chat set for: " + ", ".join(skipped) + "." if skipped else ""
            if not messages:
                raise WaError("Nothing to send." + note)
            for i, (chat_id, text) in enumerate(messages):
                if i:
                    pause()  # a burst of DMs looks like spam to WhatsApp
                send_text(cfg, chat_id, text)
                sent += 1
            _record(conn, game_id, True, f"Sent {sent} message{'s' if sent != 1 else ''}." + note)
        except WaError as e:
            _record(conn, game_id, False, (f"Sent {sent}, then failed: " if sent else "") + str(e))
        except Exception:
            _record(conn, game_id, False, "Unexpected error while sending.")
    except Exception:
        pass
    finally:
        conn.close()
        with _lock:
            _running.discard(game_id)


def dispatch(game_id: int) -> bool:
    """Starts sending in the background; False when this game is already being sent."""
    with _lock:
        if game_id in _running:
            return False
        _running.add(game_id)
    threading.Thread(target=run, args=(game_id,), daemon=True, name=f"wa-send-{game_id}").start()
    return True


def is_running(game_id: int) -> bool:
    with _lock:
        return game_id in _running


def status(conn: sqlite3.Connection, game_id: int) -> dict | None:
    r = conn.execute("SELECT at, ok, detail FROM wa_sends WHERE game_id = ?", (game_id,)).fetchone()
    s = {"sending": is_running(game_id)}
    if r:
        s.update(at=r["at"], ok=bool(r["ok"]), detail=r["detail"])
    return s
