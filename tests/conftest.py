import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="pokerbalance-test-")
os.environ["POKER_DATA_DIR"] = _tmp

import pytest  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402

# Cheap hashing in tests only.
auth._ph = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
auth._DUMMY_HASH = auth._ph.hash("dummy")

ADMIN_PW = "correct horse battery"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for f in os.listdir(_tmp):
        p = os.path.join(_tmp, f)
        if os.path.isfile(p):
            os.remove(p)
    auth.throttle.__init__()
    auth.setup_throttle.__init__()
    yield


def make_client():
    from app.main import create_app
    return TestClient(create_app(), base_url="https://testserver")


class Api:
    """Test helper that behaves like the browser UI: JSON + CSRF header."""

    def __init__(self, client):
        self.c = client
        self.csrf = ""

    def refresh(self):
        st = self.c.get("/api/state").json()
        self.csrf = st.get("csrf", "")
        return st

    def req(self, method, path, body=None, expect=200):
        kw = {}
        if method != "GET":
            kw["json"] = body if body is not None else {}
            kw["headers"] = {"X-CSRF-Token": self.csrf}
        r = self.c.request(method, "/api" + path, **kw)
        assert r.status_code == expect, (r.status_code, r.text)
        return r.json()

    def get(self, path, expect=200):
        return self.req("GET", path, expect=expect)

    def post(self, path, body=None, expect=200):
        return self.req("POST", path, body, expect)

    def put(self, path, body=None, expect=200):
        return self.req("PUT", path, body, expect)

    def patch(self, path, body=None, expect=200):
        return self.req("PATCH", path, body, expect)

    def delete(self, path, expect=200):
        return self.req("DELETE", path, {}, expect)


def setup_token():
    with open(auth.setup_token_path()) as f:
        return f.read().strip()


@pytest.fixture
def admin():
    c = make_client()
    r = c.post("/api/setup", json={"token": setup_token(), "username": "admin", "display_name": "Admin",
                                    "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    a = Api(c)
    a.refresh()
    return a


def login(username, password, totp=""):
    c = make_client()
    r = c.post("/api/login", json={"username": username, "password": password, "totp": totp})
    a = Api(c)
    a.refresh()
    return a, r


def new_member(admin, username, name=None):
    res = admin.post("/users", {"username": username, "display_name": name or username.title()})
    a, r = login(username, res["temp_password"])
    assert r.status_code == 200
    a.post("/me/password", {"current": res["temp_password"], "new": "member password 123"})
    return res["user"]["id"], a
