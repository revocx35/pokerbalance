import time

import pyotp

from app import auth, config, db
from tests.conftest import ADMIN_PW, login, make_client, new_member, setup_token


def test_setup_needs_token_and_only_once():
    c = make_client()
    st = c.get("/api/state").json()
    assert st["needs_setup"] and st["user"] is None
    body = {"token": "wrong", "username": "admin", "display_name": "Admin", "password": ADMIN_PW}
    assert c.post("/api/setup", json=body).status_code == 403
    body["token"] = setup_token()
    body["password"] = "short"
    assert c.post("/api/setup", json=body).status_code == 400
    body["password"] = ADMIN_PW
    assert c.post("/api/setup", json=body).status_code == 200
    assert c.get("/api/state").json()["user"]["is_admin"] is True
    # the token is single-use and setup can't run again
    c2 = make_client()
    assert c2.post("/api/setup", json=body).status_code == 409


def test_login_generic_errors(admin):
    _, r1 = login("admin", "wrong password!")
    _, r2 = login("nobody", "wrong password!")
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json() == {"detail": "Wrong username or password."}


def test_session_rotates_on_login(admin):
    c = make_client()
    c.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
    first = c.cookies.get(auth.COOKIE)
    c.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
    second = c.cookies.get(auth.COOKIE)
    assert first and second and first != second
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (auth._h(first),)).fetchone()[0] == 0


def test_cookie_flags(admin):
    c = make_client()
    r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
    sc = r.headers["set-cookie"]
    assert sc.startswith("__Host-pb_session=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in sc.lower()


def test_lockout_after_repeated_failures(admin):
    c = make_client()
    for _ in range(auth.Throttle.ACCOUNT_MAX):
        assert c.post("/api/login", json={"username": "admin", "password": "nope nope nope"}).status_code == 401
    r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 429  # even the right password is refused while locked


def test_throttle_unit_with_fake_clock():
    now = [1000.0]
    t = auth.Throttle(clock=lambda: now[0])
    for _ in range(5):
        assert t.blocked("1.2.3.4", "bob") == 0
        t.failure("1.2.3.4", "bob")
    assert t.blocked("1.2.3.4", "bob") > 0
    assert t.blocked("5.6.7.8", "BOB") > 0  # per account, case-insensitive
    now[0] += 15 * 60 + 1
    assert t.blocked("1.2.3.4", "bob") == 0
    for _ in range(5):
        t.failure("1.2.3.4", "bob")
    assert 29 * 60 < t.blocked("1.2.3.4", "bob") <= 30 * 60  # second lockout doubles
    t.success("bob")
    assert t.blocked("9.9.9.9", "bob") == 0
    # per-IP limit across many names
    t2 = auth.Throttle(clock=lambda: now[0])
    for i in range(auth.Throttle.IP_MAX):
        t2.failure("1.1.1.1", f"user{i}")
    assert t2.blocked("1.1.1.1", "fresh") > 0
    assert t2.blocked("2.2.2.2", "fresh") == 0


def test_csrf_required(admin):
    r = admin.c.post("/api/groups", json={"name": "G"})
    assert r.status_code == 403
    r = admin.c.post("/api/groups", json={"name": "G"}, headers={"X-CSRF-Token": "bogus"})
    assert r.status_code == 403
    admin.post("/groups", {"name": "G"})


def test_cross_site_and_content_type_refused(admin):
    h = {"X-CSRF-Token": admin.csrf}
    r = admin.c.post("/api/groups", json={"name": "G"}, headers={**h, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = admin.c.post("/api/groups", json={"name": "G"}, headers={**h, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = admin.c.post("/api/groups", content=b"name=G", headers={**h, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    r = admin.c.post("/api/groups", json={"name": "G"}, headers={**h, "Origin": "https://testserver"})
    assert r.status_code == 200


def test_body_size_limit(admin):
    r = admin.c.post("/api/groups", content=b'{"name":"' + b"x" * 70000 + b'"}',
                     headers={"X-CSRF-Token": admin.csrf, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_security_headers(admin):
    r = admin.c.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "max-age" in r.headers["strict-transport-security"]
    assert admin.c.get("/api/state").headers["cache-control"] == "no-store"
    assert "server" not in r.headers


def test_idle_and_absolute_expiry(admin, monkeypatch):
    conn = db.connect()
    conn.execute("UPDATE sessions SET last_seen = last_seen - ?", (config.SESSION_IDLE + 5,))
    assert admin.c.get("/api/groups").status_code == 401
    a, _ = login("admin", ADMIN_PW)
    conn.execute("UPDATE sessions SET created_at = created_at - ?", (config.SESSION_MAX + 5,))
    assert a.c.get("/api/groups").status_code == 401


def test_logout_invalidates_session(admin):
    token = admin.c.cookies.get(auth.COOKIE)
    admin.post("/logout")
    c = make_client()
    c.cookies.set(auth.COOKIE, token)
    assert c.get("/api/groups").status_code == 401


def test_logout_all(admin):
    other, _ = login("admin", ADMIN_PW)
    admin.post("/logout-all")
    assert other.c.get("/api/groups").status_code == 401


def test_bad_token_rejected():
    c = make_client()
    c.cookies.set(auth.COOKIE, "x" * 43)
    assert c.get("/api/groups").status_code == 401
    c.cookies.set(auth.COOKIE, "x" * 500)
    assert c.get("/api/groups").status_code == 401


def test_temp_password_must_be_changed(admin):
    res = admin.post("/users", {"username": "bob", "display_name": "Bob"})
    a, r = login("bob", res["temp_password"])
    assert r.status_code == 200
    assert a.get("/groups", expect=403)["detail"].startswith("Change your temporary password")
    a.post("/me/password", {"current": res["temp_password"], "new": "short"}, expect=400)
    a.post("/me/password", {"current": res["temp_password"], "new": "bobs new password"})
    a.get("/groups")


def test_password_change_signs_out_other_sessions(admin):
    other, _ = login("admin", ADMIN_PW)
    admin.post("/me/password", {"current": ADMIN_PW, "new": "another long password"})
    assert other.c.get("/api/groups").status_code == 401
    admin.get("/groups")


def test_members_cannot_admin(admin):
    _, bob = new_member(admin, "bob")
    bob.post("/groups", {"name": "Mine"}, expect=403)
    bob.get("/users", expect=403)


def test_disabled_user_is_signed_out(admin):
    uid, bob = new_member(admin, "bob")
    admin.patch(f"/users/{uid}", {"disabled": True})
    assert bob.c.get("/api/groups").status_code == 401
    _, r = login("bob", "member password 123")
    assert r.status_code == 401


def test_last_admin_protected(admin):
    me = admin.refresh()["user"]["id"]
    admin.patch(f"/users/{me}", {"is_admin": False}, expect=409)
    admin.delete(f"/users/{me}", expect=409)


def test_totp_flow_and_replay(admin):
    res = admin.post("/me/totp/setup")
    totp = pyotp.TOTP(res["secret"])
    admin.post("/me/totp/enable", {"code": "000000" if totp.now() != "000000" else "111111"}, expect=400)
    admin.post("/me/totp/enable", {"code": totp.now()})
    _, r = login("admin", ADMIN_PW)
    assert r.status_code == 401 and r.json()["detail"] == "totp_required"
    # the code used to enable can't be replayed
    _, r = login("admin", ADMIN_PW, totp.now())
    assert r.status_code == 401
    secret = res["secret"]
    later = auth.totp_check(secret, pyotp.TOTP(secret).at(time.time() + 30), 0)
    assert later is not None


def test_proxy_ip_resolution(monkeypatch):
    import ipaddress
    from app import main
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [ipaddress.ip_network("10.0.0.5/32")])
    scope = {"client": ("10.0.0.5", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")]}
    assert main.client_ip(scope) == "203.0.113.9"  # right-most untrusted, spoofed left entry ignored
    scope = {"client": ("198.51.100.1", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6")]}
    assert main.client_ip(scope) == "198.51.100.1"  # untrusted peer: header ignored


def test_http_via_trusted_proxy_redirects_to_https(admin, monkeypatch):
    import ipaddress
    from app import main
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [ipaddress.ip_network("10.0.0.5/32")])
    plain = {"client": ("10.0.0.5", 1), "headers": [(b"x-forwarded-proto", b"http")]}
    assert main.proxied_plain_http(plain)
    assert not main.proxied_plain_http({"client": ("10.0.0.5", 1), "headers": [(b"x-forwarded-proto", b"https")]})
    assert not main.proxied_plain_http({"client": ("203.0.113.1", 1), "headers": [(b"x-forwarded-proto", b"http")]})
    # end to end through the middleware (TestClient's peer is "testclient", so trust everything here)
    monkeypatch.setattr(main, "_trusted", lambda a: True)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [ipaddress.ip_network("0.0.0.0/0")])
    r = admin.c.get("/?a=1", headers={"X-Forwarded-Proto": "http", "Host": "poker.example"}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "https://poker.example/?a=1"
    r = admin.c.post("/api/groups", json={"name": "G"}, headers={"X-Forwarded-Proto": "http", "X-CSRF-Token": admin.csrf})
    assert r.status_code == 403
    r = admin.c.get("/api/state", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
