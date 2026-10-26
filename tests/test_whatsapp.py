"""WhatsApp results through a wa_logger API token, against a fake wa_logger over real (self-signed) TLS."""
import datetime
import hashlib
import http.server
import json
import os
import ssl
import tempfile
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import config, db, whatsapp
from tests.conftest import new_member
from tests.test_games import group_with_players

TOKEN = "walt_1_" + "A" * 43
GROUP_CHAT = "120363000000000001@g.us"
DMS = {0: "905550000000@c.us", 1: "905550000001@c.us", 2: "905550000002@c.us"}


def make_cert(dirname):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cf, kf = os.path.join(dirname, "c.pem"), os.path.join(dirname, "k.pem")
    with open(cf, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kf, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cf, kf, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


class FakeWaLogger:
    def __init__(self):
        self.sent = []      # (chatId, text)
        self.auth = []      # Authorization headers seen
        self.mode = "ok"    # ok | redirect | revoked | down
        self.destinations = [
            {"chatId": GROUP_CHAT, "kind": "group", "name": "Poker Night"},
            *[{"chatId": c, "kind": "user", "name": f"Player {i}"} for i, c in DMS.items()],
        ]
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None, headers=None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                outer.auth.append(self.headers.get("Authorization"))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                if outer.mode == "redirect":
                    return self._send(302, headers={"Location": "https://127.0.0.1:9/evil"})
                if outer.mode == "revoked" or self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"error": {"code": "unauthorized", "message": "no"}})
                if outer.mode == "down":
                    return self._send(503, {"error": {"code": "unavailable", "message": "WhatsApp is not connected."}})
                if self.path == "/api/v1/destinations":
                    return self._send(200, {"destinations": outer.destinations})
                if self.path == "/api/v1/send":
                    outer.sent.append((body["chatId"], body["text"]))
                    return self._send(200, {"id": "x"})
                self._send(404)

            do_GET = do_POST = _handle

        self.dir = tempfile.mkdtemp(prefix="fakewa-")
        cf, kf, self.fingerprint = make_cert(self.dir)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cf, kf)
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.url = f"https://localhost:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def wa(monkeypatch):
    monkeypatch.setattr(whatsapp.random, "uniform", lambda a, b: 0)
    f = FakeWaLogger()
    yield f
    f.close()


def connect(admin, wa, **over):
    body = {"base_url": wa.url, "token": TOKEN, "pin": wa.fingerprint, **over}
    return admin.put("/whatsapp", body)


def wait_sent(admin, game_id, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        st = admin.get(f"/games/{game_id}").get("whatsapp") or {}
        if st.get("at") and not st.get("sending"):
            return st
        time.sleep(0.05)
    raise AssertionError("send did not finish")


def play(admin, gid, ids, name="G1"):
    game = admin.post(f"/groups/{gid}/games", {"name": name, "buy_in": 10000, "payout_bp": [10000], "players": ids})
    gm = f"/games/{game['id']}"
    for uid in ids[:-1]:
        admin.post(f"{gm}/players/{uid}/bust")
    return game["id"]


def test_connect_stores_token_encrypted_and_never_returns_it(admin, wa):
    out = connect(admin, wa)
    assert out["connected"] and out["pinned"] and len(out["destinations"]) == 4
    assert TOKEN not in json.dumps(admin.get("/whatsapp"))
    assert wa.auth[-1] == f"Bearer {TOKEN}"
    raw = open(db.path(), "rb").read()
    assert TOKEN.encode() not in raw
    assert os.stat(os.path.join(config.DATA_DIR, "wa.key")).st_mode & 0o077 == 0
    # Updating the address without a token keeps the saved token.
    out = admin.put("/whatsapp", {"base_url": wa.url, "pin": wa.fingerprint})
    assert out["connected"]


def test_connection_is_validated_before_saving(admin, wa):
    admin.put("/whatsapp", {"base_url": "http://localhost:1", "token": TOKEN}, expect=502)
    admin.put("/whatsapp", {"base_url": wa.url + "/path", "token": TOKEN, "pin": wa.fingerprint}, expect=502)
    admin.put("/whatsapp", {"base_url": "https://user:pw@localhost", "token": TOKEN}, expect=502)
    admin.put("/whatsapp", {"base_url": wa.url, "token": "not-a-token", "pin": wa.fingerprint}, expect=502)
    admin.put("/whatsapp", {"base_url": wa.url, "token": TOKEN, "pin": "ab" * 32}, expect=502)  # wrong pin
    admin.put("/whatsapp", {"base_url": "https://169.254.169.254", "token": TOKEN}, expect=502)
    assert admin.get("/whatsapp")["connected"] is False
    # Self-signed and not pinned: refused, and the error offers the fingerprint to pin.
    r = admin.c.put("/api/whatsapp", json={"base_url": wa.url, "token": TOKEN}, headers={"X-CSRF-Token": admin.csrf})
    assert r.status_code == 502 and wa.fingerprint in r.json()["detail"]
    wa.mode = "revoked"
    admin.put("/whatsapp", {"base_url": wa.url, "token": TOKEN, "pin": wa.fingerprint}, expect=502)
    wa.mode = "redirect"
    admin.put("/whatsapp", {"base_url": wa.url, "token": TOKEN, "pin": wa.fingerprint}, expect=502)
    assert not [a for a in wa.auth if a is None]


def test_only_admins_with_csrf_may_touch_whatsapp(admin, wa):
    connect(admin, wa)
    _, member = new_member(admin, "bob")
    for method, path, body in [("GET", "/whatsapp", None), ("PUT", "/whatsapp", {"base_url": wa.url}),
                               ("DELETE", "/whatsapp", None), ("POST", "/whatsapp/test", {"chat_id": GROUP_CHAT}),
                               ("PUT", "/users/1/whatsapp", {"chat_id": None}),
                               ("PUT", "/groups/1/whatsapp", {"mode": "off"}), ("POST", "/games/1/whatsapp", {})]:
        member.req(method, path, body, expect=403)
    r = admin.c.put("/api/whatsapp", json={"base_url": wa.url})
    assert r.status_code == 403
    assert wa.sent == []


def test_group_message_sent_automatically_when_the_game_ends(admin, wa):
    connect(admin, wa)
    gid, ids = group_with_players(admin, 3)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "group", "chat_id": GROUP_CHAT, "auto": True})
    gm = play(admin, gid, ids)
    st = wait_sent(admin, gm)
    assert st["ok"], st
    assert len(wa.sent) == 1
    chat, text = wa.sent[0]
    assert chat == GROUP_CHAT
    assert "Friday · G1" in text and "1. P2: ₺300" in text and "Who pays whom" in text
    assert "P0 → P2: ₺100" in text and "P1 → P2: ₺100" in text
    # Manual resend.
    admin.post(f"/games/{gm}/whatsapp")
    deadline = time.time() + 5
    while len(wa.sent) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(wa.sent) == 2


def test_auto_off_and_mode_off_send_nothing(admin, wa):
    connect(admin, wa)
    gid, ids = group_with_players(admin, 3)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "group", "chat_id": GROUP_CHAT, "auto": False})
    gm = play(admin, gid, ids)
    time.sleep(0.3)
    assert wa.sent == []
    assert "at" not in admin.get(f"/games/{gm}")["whatsapp"]
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "off"})
    admin.post(f"/games/{gm}/whatsapp", expect=409)
    assert "whatsapp" not in admin.get(f"/games/{gm}")


def test_dm_mode_sends_each_player_their_own_payments(admin, wa):
    connect(admin, wa)
    gid, ids = group_with_players(admin, 3)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "dm", "auto": True})
    admin.put(f"/users/{ids[0]}/whatsapp", {"chat_id": DMS[0]})
    admin.put(f"/users/{ids[2]}/whatsapp", {"chat_id": DMS[2]})  # P1 has no chat
    gm = play(admin, gid, ids)
    st = wait_sent(admin, gm)
    assert st["ok"] and "P1" in st["detail"], st
    by = dict(wa.sent)
    assert set(by) == {DMS[0], DMS[2]}
    assert "You pay P2: ₺100" in by[DMS[0]] and "P1" not in by[DMS[0]].split("Your payments")[1]
    assert "P0 pays you: ₺100" in by[DMS[2]] and "P1 pays you: ₺100" in by[DMS[2]]


def test_failures_are_reported_not_raised(admin, wa):
    connect(admin, wa)
    gid, ids = group_with_players(admin, 3)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "group", "chat_id": GROUP_CHAT, "auto": True})
    wa.mode = "down"
    gm = play(admin, gid, ids)
    st = wait_sent(admin, gm)
    assert st["ok"] is False and "isn't connected" in st["detail"]
    assert admin.get(f"/games/{gm}")["status"] == "finished"


def test_chat_choices_must_come_from_the_token(admin, wa):
    connect(admin, wa)
    gid, ids = group_with_players(admin, 3)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "group", "chat_id": "999@g.us"}, expect=400)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "group", "chat_id": DMS[0]}, expect=400)  # a DM is not a group
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "group"}, expect=400)
    admin.put(f"/users/{ids[0]}/whatsapp", {"chat_id": GROUP_CHAT}, expect=400)
    admin.put(f"/users/{ids[0]}/whatsapp", {"chat_id": "../x@c.us"}, expect=400)
    admin.post("/whatsapp/test", {"chat_id": GROUP_CHAT})
    assert wa.sent[-1][0] == GROUP_CHAT


def test_disconnect_clears_settings_and_members_never_see_chat_ids(admin, wa):
    connect(admin, wa)
    gid, ids = group_with_players(admin, 3)
    admin.put(f"/groups/{gid}/whatsapp", {"mode": "dm"})
    admin.put(f"/users/{ids[0]}/whatsapp", {"chat_id": DMS[0]})
    assert admin.get(f"/groups/{gid}")["whatsapp"]["mode"] == "dm"
    _, member = new_member(admin, "bob")
    admin.post(f"/groups/{gid}/members", {"user_id": member.get("/me")["id"] if False else _uid(admin, "bob")})
    seen = member.get(f"/groups/{gid}")
    assert "whatsapp" not in seen and all("wa_chat_id" not in m for m in seen["members"])
    admin.delete("/whatsapp")
    assert admin.get(f"/groups/{gid}")["whatsapp"]["mode"] == "off"
    assert all(u["wa_chat_id"] is None for u in admin.get("/users"))


def _uid(admin, username):
    return next(u["id"] for u in admin.get("/users") if u["username"] == username)


def test_message_text_strips_formatting_and_control_characters():
    assert whatsapp._clean("*Bo\nb*_~") == "Bob"
    assert whatsapp._money(-12550, "₺") == "−₺125.50" and whatsapp._money(100000, "₺") == "₺1,000"


def test_link_local_and_unspecified_addresses_are_never_contacted(monkeypatch):
    called = []
    monkeypatch.setattr(whatsapp.socket, "create_connection", lambda *a, **k: called.append(a))
    for host in ("169.254.169.254", "0.0.0.0", "fe80::1", "224.0.0.1"):
        with pytest.raises(whatsapp.WaError, match="isn't allowed"):
            whatsapp._open(host, 443)
    assert called == []
