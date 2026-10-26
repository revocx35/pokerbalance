"""JSON API. Admins manage everything; members can read the groups they belong to."""
from __future__ import annotations

import base64
import hmac
import json
import re
import sqlite3
from typing import Annotated, Iterator, Literal

import segno
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator

from . import auth, config, db, games, payouts, whatsapp
from .games import err

router = APIRouter()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")


# ---------------------------------------------------------------- dependencies


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class Ctx:
    def __init__(self, conn, session, user, token):
        self.conn, self.session, self.user, self.token = conn, session, user, token

    @property
    def admin(self) -> bool:
        return bool(self.user["is_admin"])


def current(request: Request, conn: Conn) -> Ctx:
    token = request.cookies.get(auth.COOKIE)
    loaded = auth.load_session(conn, token)
    if not loaded:
        raise err(401, "Please sign in.")
    session, user = loaded
    if request.method not in ("GET", "HEAD"):
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent.encode(), session["csrf"].encode()):
            raise err(403, "Session check failed. Reload the page.")
    ctx = Ctx(conn, session, user, token)
    # A user with a temporary password may only change it (or sign out).
    if user["must_change_pw"] and request.url.path not in ("/api/state", "/api/me/password", "/api/logout"):
        raise err(403, "Change your temporary password first.")
    return ctx


def admin(ctx: Annotated[Ctx, Depends(current)]) -> Ctx:
    if not ctx.admin:
        raise err(403, "Only an admin can do that.")
    return ctx


User = Annotated[Ctx, Depends(current)]
Admin = Annotated[Ctx, Depends(admin)]


def set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(auth.COOKIE, token, max_age=config.SESSION_MAX, path="/", secure=config.COOKIE_SECURE,
                    httponly=True, samesite="strict")


def clear_cookie(resp: Response) -> None:
    resp.delete_cookie(auth.COOKIE, path="/", secure=config.COOKIE_SECURE, httponly=True, samesite="strict")


def ip(request: Request) -> str:
    return request.state.client_ip if hasattr(request.state, "client_ip") else request.client.host


def user_json(u) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"],
            "is_admin": bool(u["is_admin"]), "must_change_pw": bool(u["must_change_pw"]),
            "disabled": bool(u["disabled"]), "totp": bool(u["totp_secret"])}


def can_view_group(ctx: Ctx, gid: int) -> None:
    if not ctx.conn.execute("SELECT 1 FROM groups WHERE id = ?", (gid,)).fetchone():
        raise err(404, "Group not found.")
    if ctx.admin:
        return
    if not ctx.conn.execute("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
                            (gid, ctx.user["id"])).fetchone():
        raise err(404, "Group not found.")


# ---------------------------------------------------------------- models


class Strict(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}


Name = Annotated[str, Field(min_length=1, max_length=40)]
Password = Annotated[str, Field(min_length=1, max_length=auth.PASSWORD_MAX)]
Money = Annotated[int, Field(ge=1, le=10**11)]  # cents


class SetupIn(Strict):
    token: Annotated[str, Field(max_length=200)]
    username: Annotated[str, Field(max_length=32)]
    display_name: Name
    password: Password


class LoginIn(Strict):
    username: Annotated[str, Field(max_length=64)]
    password: Password
    totp: Annotated[str, Field(max_length=12)] = ""


class PasswordIn(Strict):
    current: Password
    new: Password


class CodeIn(Strict):
    code: Annotated[str, Field(max_length=12)]


class TotpOffIn(Strict):
    password: Password
    code: Annotated[str, Field(max_length=12)]


class UserIn(Strict):
    username: Annotated[str, Field(max_length=32)]
    display_name: Name
    is_admin: bool = False


class UserPatch(Strict):
    display_name: Name | None = None
    is_admin: bool | None = None
    disabled: bool | None = None
    reset_totp: bool | None = None


class GroupIn(Strict):
    name: Name
    currency: Annotated[str, Field(min_length=1, max_length=8)] = config.DEFAULT_CURRENCY


class GroupPatch(Strict):
    name: Name | None = None
    currency: Annotated[str, Field(min_length=1, max_length=8)] | None = None


class MemberIn(Strict):
    user_id: int


class WaConfigIn(Strict):
    base_url: Annotated[str, Field(min_length=1, max_length=200)]
    token: Annotated[str, Field(max_length=200)] | None = None
    pin: Annotated[str, Field(max_length=100)] | None = None


class WaGroupIn(Strict):
    mode: Literal["off", "group", "dm"]
    chat_id: Annotated[str, Field(max_length=200)] | None = None
    auto: bool = True


class WaChatIn(Strict):
    chat_id: Annotated[str, Field(max_length=200)] | None = None


class GameIn(Strict):
    name: Name
    buy_in: Money
    payout_bp: Annotated[list[Annotated[int, Field(ge=0, le=payouts.FULL_BP)]], Field(min_length=1, max_length=20)]
    players: Annotated[list[int], Field(min_length=2, max_length=60)]
    buy_ins: dict[str, Annotated[int, Field(ge=1, le=99)]] = {}


class GamePatch(Strict):
    name: Name | None = None
    buy_in: Money | None = None
    payout_bp: Annotated[list[Annotated[int, Field(ge=0, le=payouts.FULL_BP)]],
                         Field(min_length=1, max_length=20)] | None = None


class BuyInsIn(Strict):
    buy_ins: Annotated[int, Field(ge=1, le=99)]


class DealIn(Strict):
    shares: dict[str, Annotated[int, Field(ge=0, le=payouts.FULL_BP)]]

    @field_validator("shares")
    @classmethod
    def keys_are_ids(cls, v):
        if len(v) > 60 or not all(k.isdigit() for k in v):
            raise ValueError("bad player ids")
        return v


class RoundIn(Strict):
    action: Annotated[str, Field(pattern="^(more|revert)$")]


class PaidIn(Strict):
    paid: bool


# ---------------------------------------------------------------- session & setup


@router.get("/state")
def state(request: Request, conn: Conn):
    needs_setup = not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    out = {"needs_setup": needs_setup, "version": config.__version__, "user": None,
           "default_currency": config.DEFAULT_CURRENCY}
    loaded = auth.load_session(conn, request.cookies.get(auth.COOKIE))
    if loaded:
        s, u = loaded
        out["user"] = user_json(u)
        out["csrf"] = s["csrf"]
    return out


@router.post("/setup")
def setup(body: SetupIn, request: Request, response: Response, conn: Conn):
    client = ip(request)
    if auth.setup_throttle.blocked(client, "setup"):
        raise err(429, "Too many attempts. Try again later.")
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        raise err(409, "Setup is already done.")
    if not auth.check_setup_token(body.token):
        auth.setup_throttle.failure(client, "setup")
        raise err(403, "Wrong setup token. It is printed in the container log.")
    if not USERNAME_RE.match(body.username):
        raise err(400, "Username: 2-32 letters, digits, dot, dash or underscore.")
    if p := auth.password_problem(body.password):
        raise err(400, p)
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise err(409, "Setup is already done.")
        cur = conn.execute("INSERT INTO users(username, display_name, password_hash, is_admin, created_at) "
                           "VALUES (?,?,?,1,?)", (body.username, body.display_name,
                                                  auth.hash_password(body.password), db.now()))
        token, _ = auth.create_session(conn, cur.lastrowid)
    auth.clear_setup_token()
    set_cookie(response, token)
    return {"ok": True}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, conn: Conn):
    client = ip(request)
    name = body.username.strip()
    wait = auth.throttle.blocked(client, name)
    if wait:
        raise err(429, f"Too many attempts. Try again in {max(1, int(wait // 60) + 1)} min.")
    u = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    ok = auth.verify_password(u["password_hash"] if u else None, body.password)
    if not ok or u["disabled"]:
        auth.throttle.failure(client, name)
        raise err(401, "Wrong username or password.")
    if u["totp_secret"]:
        if not body.totp:
            raise err(401, "totp_required")
        step = auth.totp_check(u["totp_secret"], body.totp, u["totp_last_step"])
        if step is None:
            auth.throttle.failure(client, name)
            raise err(401, "Wrong or already used 2FA code.")
        conn.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, u["id"]))
    auth.throttle.success(name)
    with db.tx(conn):
        auth.delete_session(conn, request.cookies.get(auth.COOKIE))  # rotate: never reuse a prior session
        if _ph_needs_rehash(u["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (auth.hash_password(body.password), u["id"]))
        token, _ = auth.create_session(conn, u["id"])
    set_cookie(response, token)
    return {"ok": True}


def _ph_needs_rehash(h: str) -> bool:
    try:
        return auth._ph.check_needs_rehash(h)
    except Exception:
        return False


@router.post("/logout")
def logout(request: Request, response: Response, ctx: User):
    auth.delete_session(ctx.conn, ctx.token)
    clear_cookie(response)
    return {"ok": True}


@router.post("/logout-all")
def logout_all(response: Response, ctx: User):
    auth.delete_user_sessions(ctx.conn, ctx.user["id"])
    clear_cookie(response)
    return {"ok": True}


@router.post("/me/password")
def change_password(body: PasswordIn, request: Request, ctx: User):
    client = ip(request)
    if auth.throttle.blocked(client, ctx.user["username"]):
        raise err(429, "Too many attempts. Try again later.")
    if not auth.verify_password(ctx.user["password_hash"], body.current):
        auth.throttle.failure(client, ctx.user["username"])
        raise err(400, "Current password is wrong.")
    if p := auth.password_problem(body.new):
        raise err(400, p)
    if body.new == body.current:
        raise err(400, "Pick a new password.")
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE users SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
                         (auth.hash_password(body.new), ctx.user["id"]))
        auth.delete_user_sessions(ctx.conn, ctx.user["id"], keep_token=ctx.token)
    return {"ok": True}


@router.post("/me/totp/setup")
def totp_setup(ctx: User):
    if ctx.user["totp_secret"]:
        raise err(409, "2FA is already on.")
    secret = auth.new_totp_secret()
    ctx.conn.execute("UPDATE users SET totp_pending = ? WHERE id = ?", (secret, ctx.user["id"]))
    uri = auth.totp_uri(secret, ctx.user["username"])
    svg = segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
    return {"secret": secret, "uri": uri,
            "qr": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()}


@router.post("/me/totp/enable")
def totp_enable(body: CodeIn, ctx: User):
    pending = ctx.user["totp_pending"]
    if not pending:
        raise err(409, "Start the 2FA setup first.")
    step = auth.totp_check(pending, body.code, 0)
    if step is None:
        raise err(400, "That code doesn't match. Check the time on your phone.")
    ctx.conn.execute("UPDATE users SET totp_secret = ?, totp_pending = NULL, totp_last_step = ? WHERE id = ?",
                     (pending, step, ctx.user["id"]))
    return {"ok": True}


@router.post("/me/totp/disable")
def totp_disable(body: TotpOffIn, request: Request, ctx: User):
    client = ip(request)
    if auth.throttle.blocked(client, ctx.user["username"]):
        raise err(429, "Too many attempts. Try again later.")
    u = ctx.user
    step = auth.totp_check(u["totp_secret"], body.code, u["totp_last_step"]) if u["totp_secret"] else None
    if not auth.verify_password(u["password_hash"], body.password) or step is None:
        auth.throttle.failure(client, u["username"])
        raise err(400, "Wrong password or 2FA code.")
    ctx.conn.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL WHERE id = ?", (u["id"],))
    return {"ok": True}


# ---------------------------------------------------------------- users (admin)


@router.get("/users")
def list_users(ctx: Admin):
    rows = ctx.conn.execute("SELECT * FROM users ORDER BY display_name COLLATE NOCASE").fetchall()
    return [{**user_json(u), "wa_chat_id": u["wa_chat_id"]} for u in rows]


@router.post("/users")
def create_user(body: UserIn, ctx: Admin):
    if not USERNAME_RE.match(body.username):
        raise err(400, "Username: 2-32 letters, digits, dot, dash or underscore.")
    temp = auth.generate_password()
    try:
        with db.tx(ctx.conn):
            cur = ctx.conn.execute(
                "INSERT INTO users(username, display_name, password_hash, is_admin, must_change_pw, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (body.username, body.display_name, auth.hash_password(temp), int(body.is_admin), db.now()))
    except sqlite3.IntegrityError:
        raise err(409, "That username is taken.")
    u = ctx.conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"user": user_json(u), "temp_password": temp}


def _get_user(conn, uid: int):
    u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not u:
        raise err(404, "User not found.")
    return u


def _other_active_admins(conn, uid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0 AND id != ?",
                        (uid,)).fetchone()[0]


@router.patch("/users/{uid}")
def update_user(uid: int, body: UserPatch, ctx: Admin):
    u = _get_user(ctx.conn, uid)
    with db.tx(ctx.conn):
        if body.display_name is not None:
            ctx.conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (body.display_name, uid))
        if body.is_admin is not None and bool(body.is_admin) != bool(u["is_admin"]):
            if not body.is_admin and not _other_active_admins(ctx.conn, uid):
                raise err(409, "There must always be at least one admin.")
            ctx.conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(body.is_admin), uid))
        if body.disabled is not None and bool(body.disabled) != bool(u["disabled"]):
            if body.disabled and u["is_admin"] and not _other_active_admins(ctx.conn, uid):
                raise err(409, "You can't disable the last admin.")
            ctx.conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), uid))
            if body.disabled:
                auth.delete_user_sessions(ctx.conn, uid)
        if body.reset_totp:
            ctx.conn.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL WHERE id = ?", (uid,))
    return user_json(_get_user(ctx.conn, uid))


@router.post("/users/{uid}/reset-password")
def reset_password(uid: int, ctx: Admin):
    _get_user(ctx.conn, uid)
    temp = auth.generate_password()
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE users SET password_hash = ?, must_change_pw = 1 WHERE id = ?",
                         (auth.hash_password(temp), uid))
        auth.delete_user_sessions(ctx.conn, uid, keep_token=ctx.token if uid == ctx.user["id"] else None)
    return {"temp_password": temp}


@router.delete("/users/{uid}")
def delete_user(uid: int, ctx: Admin):
    u = _get_user(ctx.conn, uid)
    if uid == ctx.user["id"]:
        raise err(409, "You can't delete yourself.")
    if u["is_admin"] and not _other_active_admins(ctx.conn, uid):
        raise err(409, "There must always be at least one admin.")
    if ctx.conn.execute("SELECT 1 FROM game_players WHERE user_id = ? LIMIT 1", (uid,)).fetchone():
        raise err(409, "This user has played games, so their history would break. Disable them instead.")
    with db.tx(ctx.conn):
        ctx.conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    return {"ok": True}


# ---------------------------------------------------------------- groups


@router.get("/groups")
def list_groups(ctx: User):
    q = ("SELECT g.*, (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id) AS members, "
         "(SELECT COUNT(*) FROM games x WHERE x.group_id = g.id) AS games, "
         "(SELECT COUNT(*) FROM games x WHERE x.group_id = g.id AND x.status = 'running') AS running FROM groups g ")
    if ctx.admin:
        rows = ctx.conn.execute(q + "ORDER BY g.name COLLATE NOCASE").fetchall()
    else:
        rows = ctx.conn.execute(q + "JOIN group_members gm ON gm.group_id = g.id AND gm.user_id = ? "
                                    "ORDER BY g.name COLLATE NOCASE", (ctx.user["id"],)).fetchall()
    return [dict(r) for r in rows]


@router.post("/groups")
def create_group(body: GroupIn, ctx: Admin):
    with db.tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO groups(name, currency, created_at) VALUES (?,?,?)",
                               (body.name, body.currency, db.now()))
    return {"id": cur.lastrowid}


@router.get("/groups/{gid}")
def get_group(gid: int, ctx: User):
    can_view_group(ctx, gid)
    g = ctx.conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    members = ctx.conn.execute(
        "SELECT u.id, u.display_name, u.username, u.disabled, u.wa_chat_id FROM group_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.group_id = ? ORDER BY u.display_name COLLATE NOCASE", (gid,)).fetchall()
    game_rows = ctx.conn.execute("SELECT * FROM games WHERE group_id = ? ORDER BY created_at DESC, id DESC",
                                 (gid,)).fetchall()
    game_list = []
    for gm in game_rows:
        ps = ctx.conn.execute(
            "SELECT gp.buy_ins, gp.place, u.display_name FROM game_players gp JOIN users u ON u.id = gp.user_id "
            "WHERE gp.game_id = ?", (gm["id"],)).fetchall()
        winner = next((p["display_name"] for p in ps if p["place"] == 1), None) if gm["status"] == "finished" else None
        unpaid = ctx.conn.execute("SELECT COUNT(*) FROM transfers WHERE game_id = ? AND paid = 0",
                                  (gm["id"],)).fetchone()[0]
        game_list.append({"id": gm["id"], "name": gm["name"], "status": gm["status"],
                          "finish_kind": gm["finish_kind"], "created_at": gm["created_at"],
                          "players": len(ps), "pot": gm["buy_in"] * sum(p["buy_ins"] for p in ps),
                          "winner": winner, "unpaid": unpaid})
    member_list = [dict(m) for m in members]
    out = {"id": g["id"], "name": g["name"], "currency": g["currency"],
           "members": member_list, "games": game_list, **games.group_summary(ctx.conn, gid)}
    if ctx.admin:
        out["whatsapp"] = {"mode": g["wa_mode"], "chat_id": g["wa_chat_id"], "auto": bool(g["wa_auto"])}
    else:  # a player's WhatsApp chat is the admin's business
        for m in member_list:
            m.pop("wa_chat_id", None)
    return out


@router.patch("/groups/{gid}")
def update_group(gid: int, body: GroupPatch, ctx: Admin):
    can_view_group(ctx, gid)
    with db.tx(ctx.conn):
        if body.name is not None:
            ctx.conn.execute("UPDATE groups SET name = ? WHERE id = ?", (body.name, gid))
        if body.currency is not None:
            ctx.conn.execute("UPDATE groups SET currency = ? WHERE id = ?", (body.currency, gid))
    return {"ok": True}


@router.delete("/groups/{gid}")
def delete_group(gid: int, ctx: Admin):
    can_view_group(ctx, gid)
    with db.tx(ctx.conn):
        ctx.conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
    return {"ok": True}


@router.post("/groups/{gid}/members")
def add_member(gid: int, body: MemberIn, ctx: Admin):
    can_view_group(ctx, gid)
    _get_user(ctx.conn, body.user_id)
    with db.tx(ctx.conn):
        ctx.conn.execute("INSERT OR IGNORE INTO group_members(group_id, user_id) VALUES (?,?)", (gid, body.user_id))
    return {"ok": True}


@router.delete("/groups/{gid}/members/{uid}")
def remove_member(gid: int, uid: int, ctx: Admin):
    can_view_group(ctx, gid)
    if ctx.conn.execute("SELECT 1 FROM game_players gp JOIN games g ON g.id = gp.game_id "
                        "WHERE g.group_id = ? AND g.status = 'running' AND gp.user_id = ?", (gid, uid)).fetchone():
        raise err(409, "This player is in a running game. Remove them from the game first.")
    with db.tx(ctx.conn):
        ctx.conn.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (gid, uid))
    return {"ok": True}


@router.post("/groups/{gid}/settle-all")
def settle_all(gid: int, ctx: Admin):
    can_view_group(ctx, gid)
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE transfers SET paid = 1, paid_at = ? WHERE paid = 0 AND game_id IN "
                         "(SELECT id FROM games WHERE group_id = ?)", (db.now(), gid))
    return {"ok": True}


# ---------------------------------------------------------------- games


def _members(conn, gid: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (gid,))}


@router.post("/groups/{gid}/games")
def create_game(gid: int, body: GameIn, ctx: Admin):
    can_view_group(ctx, gid)
    players = list(dict.fromkeys(body.players))
    if len(players) < 2:
        raise err(400, "A game needs at least two players.")
    if not set(players) <= _members(ctx.conn, gid):
        raise err(400, "Every player must be a member of the group.")
    games.check_structure(body.payout_bp, len(players))
    with db.tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO games(group_id, name, buy_in, payout_bp, created_at) VALUES (?,?,?,?,?)",
                               (gid, body.name, body.buy_in, json.dumps(body.payout_bp), db.now()))
        for uid in players:
            ctx.conn.execute("INSERT INTO game_players(game_id, user_id, buy_ins) VALUES (?,?,?)",
                             (cur.lastrowid, uid, body.buy_ins.get(str(uid), 1)))
    return {"id": cur.lastrowid}


def _game_for(ctx: Ctx, game_id: int):
    g = games.get_game(ctx.conn, game_id)
    can_view_group(ctx, g["group_id"])
    return g


def _running(g) -> None:
    if g["status"] != "running":
        raise err(409, "The game is finished. Reopen it to change that.")


def _finished(g) -> None:
    if g["status"] != "finished":
        raise err(409, "The game is still running.")


@router.get("/games/{game_id}")
def get_game(game_id: int, ctx: User):
    view = games.game_view(ctx.conn, _game_for(ctx, game_id))
    if ctx.admin and view["status"] == "finished":
        mode = ctx.conn.execute("SELECT wa_mode FROM groups WHERE id = ?", (view["group"]["id"],)).fetchone()[0]
        if mode != "off":
            view["whatsapp"] = whatsapp.status(ctx.conn, game_id)
    return view


@router.patch("/games/{game_id}")
def update_game(game_id: int, body: GamePatch, ctx: Admin):
    g = _game_for(ctx, game_id)
    if (body.buy_in is not None or body.payout_bp is not None):
        _running(g)
    with db.tx(ctx.conn):
        if body.name is not None:
            ctx.conn.execute("UPDATE games SET name = ? WHERE id = ?", (body.name, game_id))
        if body.buy_in is not None:
            ctx.conn.execute("UPDATE games SET buy_in = ? WHERE id = ?", (body.buy_in, game_id))
        if body.payout_bp is not None:
            n = ctx.conn.execute("SELECT COUNT(*) FROM game_players WHERE game_id = ?", (game_id,)).fetchone()[0]
            games.check_structure(body.payout_bp, n)
            ctx.conn.execute("UPDATE games SET payout_bp = ? WHERE id = ?", (json.dumps(body.payout_bp), game_id))
    return games.game_view(ctx.conn, games.get_game(ctx.conn, game_id))


@router.delete("/games/{game_id}")
def delete_game(game_id: int, ctx: Admin):
    _game_for(ctx, game_id)
    with db.tx(ctx.conn):
        ctx.conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
    return {"ok": True}


@router.post("/games/{game_id}/players")
def add_player(game_id: int, body: MemberIn, ctx: Admin):
    g = _game_for(ctx, game_id)
    _running(g)
    if body.user_id not in _members(ctx.conn, g["group_id"]):
        raise err(400, "Add them to the group first.")
    with db.tx(ctx.conn):
        ctx.conn.execute("INSERT OR IGNORE INTO game_players(game_id, user_id, buy_ins) VALUES (?,?,1)",
                         (game_id, body.user_id))
    return games.game_view(ctx.conn, g)


def _player(conn, game_id: int, uid: int):
    p = conn.execute("SELECT * FROM game_players WHERE game_id = ? AND user_id = ?", (game_id, uid)).fetchone()
    if not p:
        raise err(404, "That player isn't in this game.")
    return p


@router.delete("/games/{game_id}/players/{uid}")
def remove_player(game_id: int, uid: int, ctx: Admin):
    g = _game_for(ctx, game_id)
    _running(g)
    _player(ctx.conn, game_id, uid)
    n = ctx.conn.execute("SELECT COUNT(*) FROM game_players WHERE game_id = ?", (game_id,)).fetchone()[0]
    if n - 1 < max(2, len(json.loads(g["payout_bp"]))):
        raise err(409, "Too few players would be left for the payout structure.")
    with db.tx(ctx.conn):
        ctx.conn.execute("DELETE FROM game_players WHERE game_id = ? AND user_id = ?", (game_id, uid))
    return games.game_view(ctx.conn, g)


@router.put("/games/{game_id}/players/{uid}/buy-ins")
def set_buy_ins(game_id: int, uid: int, body: BuyInsIn, ctx: Admin):
    g = _game_for(ctx, game_id)
    _running(g)
    _player(ctx.conn, game_id, uid)
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE game_players SET buy_ins = ? WHERE game_id = ? AND user_id = ?",
                         (body.buy_ins, game_id, uid))
    return games.game_view(ctx.conn, g)


@router.post("/games/{game_id}/players/{uid}/bust")
def bust(game_id: int, uid: int, ctx: Admin):
    g = _game_for(ctx, game_id)
    _running(g)
    p = _player(ctx.conn, game_id, uid)
    if p["bust_seq"] is not None:
        raise err(409, "That player is already out.")
    finished = False
    with db.tx(ctx.conn):
        seq = ctx.conn.execute("SELECT COALESCE(MAX(bust_seq), 0) + 1 FROM game_players WHERE game_id = ?",
                               (game_id,)).fetchone()[0]
        ctx.conn.execute("UPDATE game_players SET bust_seq = ? WHERE game_id = ? AND user_id = ?",
                         (seq, game_id, uid))
        alive = ctx.conn.execute("SELECT COUNT(*) FROM game_players WHERE game_id = ? AND bust_seq IS NULL",
                                 (game_id,)).fetchone()[0]
        if alive == 1:
            games.finish_last_standing(ctx.conn, games.get_game(ctx.conn, game_id))
            finished = True
    if finished:
        _auto_whatsapp(ctx.conn, game_id)
    return games.game_view(ctx.conn, games.get_game(ctx.conn, game_id))


@router.post("/games/{game_id}/players/{uid}/unbust")
def unbust(game_id: int, uid: int, ctx: Admin):
    """Undo a bust (mistake) - with rebuy=True the player also buys in again."""
    g = _game_for(ctx, game_id)
    _running(g)
    _player(ctx.conn, game_id, uid)
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE game_players SET bust_seq = NULL WHERE game_id = ? AND user_id = ?",
                         (game_id, uid))
    return games.game_view(ctx.conn, g)


@router.post("/games/{game_id}/players/{uid}/rebuy")
def rebuy(game_id: int, uid: int, ctx: Admin):
    g = _game_for(ctx, game_id)
    _running(g)
    p = _player(ctx.conn, game_id, uid)
    if p["buy_ins"] >= 99:
        raise err(409, "That's a lot of buy-ins.")
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE game_players SET bust_seq = NULL, buy_ins = buy_ins + 1 "
                         "WHERE game_id = ? AND user_id = ?", (game_id, uid))
    return games.game_view(ctx.conn, g)


@router.post("/games/{game_id}/deal")
def deal(game_id: int, body: DealIn, ctx: Admin):
    g = _game_for(ctx, game_id)
    _running(g)
    with db.tx(ctx.conn):
        games.apply_deal(ctx.conn, g, {int(k): v for k, v in body.shares.items()})
    _auto_whatsapp(ctx.conn, game_id)
    return games.game_view(ctx.conn, games.get_game(ctx.conn, game_id))


@router.post("/games/{game_id}/round")
def round_payouts(game_id: int, body: RoundIn, ctx: Admin):
    g = _game_for(ctx, game_id)
    _finished(g)
    with db.tx(ctx.conn):
        games.round_action(ctx.conn, g, body.action)
    return games.game_view(ctx.conn, games.get_game(ctx.conn, game_id))


@router.post("/games/{game_id}/reopen")
def reopen(game_id: int, ctx: Admin):
    g = _game_for(ctx, game_id)
    _finished(g)
    with db.tx(ctx.conn):
        games.reopen(ctx.conn, g)
    return games.game_view(ctx.conn, games.get_game(ctx.conn, game_id))


@router.put("/games/{game_id}/transfers/{tid}")
def mark_paid(game_id: int, tid: int, body: PaidIn, ctx: Admin):
    g = _game_for(ctx, game_id)
    with db.tx(ctx.conn):
        cur = ctx.conn.execute("UPDATE transfers SET paid = ?, paid_at = ? WHERE id = ? AND game_id = ?",
                               (int(body.paid), db.now() if body.paid else None, tid, game_id))
        if not cur.rowcount:
            raise err(404, "Transfer not found.")
    return games.game_view(ctx.conn, g)


# ---------------------------------------------------------------- WhatsApp (results through a wa_logger API token)


def _auto_whatsapp(conn: sqlite3.Connection, game_id: int) -> None:
    """Called right after a game ended: sends in the background when the group asked for it."""
    row = conn.execute("SELECT gr.wa_mode, gr.wa_auto FROM games g JOIN groups gr ON gr.id = g.group_id "
                       "WHERE g.id = ?", (game_id,)).fetchone()
    if row and row["wa_mode"] != "off" and row["wa_auto"] and whatsapp.get_config(conn):
        whatsapp.dispatch(game_id)


def _wa_fail(e: whatsapp.WaError):
    return err(502, str(e))


def _known_chat(conn, chat_id: str, kind: str) -> None:
    cfg = whatsapp.get_config(conn)
    if not cfg:
        raise err(409, "Connect WhatsApp first.")
    try:
        found = whatsapp.destinations(cfg)
    except whatsapp.WaError as e:
        raise _wa_fail(e)
    if not any(d["chat_id"] == chat_id and d["kind"] == kind for d in found):
        raise err(400, "That chat isn't one of the chats this WhatsApp token may use.")


@router.get("/whatsapp")
def wa_status(ctx: Admin):
    out = whatsapp.public_config(ctx.conn)
    out["destinations"], out["error"] = None, None
    cfg = whatsapp.get_config(ctx.conn)
    if cfg:
        try:
            out["destinations"] = whatsapp.destinations(cfg)
        except whatsapp.WaError as e:
            out["error"] = str(e)
    return out


@router.put("/whatsapp")
def wa_connect(body: WaConfigIn, ctx: Admin):
    try:
        url = whatsapp.normalize_url(body.base_url)
        pin = whatsapp.normalize_pin(body.pin)
        old = whatsapp.get_config(ctx.conn)
        token = body.token.strip() if body.token and body.token.strip() else None
        if token is None and not old:
            raise whatsapp.WaError("Paste the API token from wa_logger.")
        if token is not None and not re.fullmatch(r"walt_\d{1,5}_[A-Za-z0-9_-]{43}", token):
            raise whatsapp.WaError("That doesn't look like a wa_logger API token.")
        trial = {"base_url": url, "pin": pin, "token_enc": whatsapp.seal(token) if token else old["token_enc"]}
        found = whatsapp.destinations(trial)  # prove the connection works before saving anything
        whatsapp.save_config(ctx.conn, url, token, pin)
    except whatsapp.WaError as e:
        raise _wa_fail(e)
    return {**whatsapp.public_config(ctx.conn), "destinations": found, "error": None}


@router.delete("/whatsapp")
def wa_disconnect(ctx: Admin):
    whatsapp.delete_config(ctx.conn)
    return {"ok": True}


@router.post("/whatsapp/test")
def wa_test(body: WaChatIn, ctx: Admin):
    cfg = whatsapp.get_config(ctx.conn)
    if not cfg or not body.chat_id:
        raise err(409, "Connect WhatsApp and pick a chat first.")
    try:
        whatsapp.send_text(cfg, body.chat_id, "✅ Poker Balance is connected. Game results will show up here.")
    except whatsapp.WaError as e:
        raise _wa_fail(e)
    return {"ok": True}


@router.put("/groups/{gid}/whatsapp")
def wa_group(gid: int, body: WaGroupIn, ctx: Admin):
    can_view_group(ctx, gid)
    chat = body.chat_id if body.mode == "group" else None
    if body.mode == "group":
        if not chat:
            raise err(400, "Pick the WhatsApp group to post in.")
        _known_chat(ctx.conn, chat, "group")
    elif body.mode == "dm" and not whatsapp.get_config(ctx.conn):
        raise err(409, "Connect WhatsApp first.")
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE groups SET wa_mode = ?, wa_chat_id = ?, wa_auto = ? WHERE id = ?",
                         (body.mode, chat, int(body.auto), gid))
    return {"ok": True}


@router.put("/users/{uid}/whatsapp")
def wa_user(uid: int, body: WaChatIn, ctx: Admin):
    _get_user(ctx.conn, uid)
    if body.chat_id:
        _known_chat(ctx.conn, body.chat_id, "user")
    with db.tx(ctx.conn):
        ctx.conn.execute("UPDATE users SET wa_chat_id = ? WHERE id = ?", (body.chat_id or None, uid))
    return {"ok": True}


@router.post("/groups/{gid}/whatsapp/balances")
def wa_send_balances(gid: int, ctx: Admin):
    can_view_group(ctx, gid)
    cfg = whatsapp.get_config(ctx.conn)
    if not cfg:
        raise err(409, "Connect WhatsApp first.")
    g = ctx.conn.execute("SELECT name, currency, wa_mode, wa_chat_id FROM groups WHERE id = ?", (gid,)).fetchone()
    if g["wa_mode"] != "group" or not g["wa_chat_id"]:
        raise err(409, "Pick a WhatsApp group under this group's WhatsApp results first.")
    plan = games.group_summary(ctx.conn, gid)["settle_all"]
    try:
        whatsapp.send_text(cfg, g["wa_chat_id"], whatsapp.balances_text(g["name"], g["currency"], plan))
    except whatsapp.WaError as e:
        raise _wa_fail(e)
    return {"ok": True}


@router.post("/games/{game_id}/whatsapp")
def wa_send_game(game_id: int, ctx: Admin):
    g = _game_for(ctx, game_id)
    _finished(g)
    if not whatsapp.get_config(ctx.conn):
        raise err(409, "Connect WhatsApp first.")
    if ctx.conn.execute("SELECT wa_mode FROM groups WHERE id = ?", (g["group_id"],)).fetchone()[0] == "off":
        raise err(409, "WhatsApp messages are off for this group.")
    if not whatsapp.dispatch(game_id):
        raise err(409, "This game is already being sent.")
    return {"ok": True}
