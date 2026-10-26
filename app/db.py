"""SQLite storage. One short-lived connection per request; WAL mode; schema migrations by user_version."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = [
    # v1
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        must_change_pw INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        totp_secret TEXT,
        totp_pending TEXT,
        totp_last_step INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
    );
    CREATE INDEX sessions_user ON sessions(user_id);
    CREATE TABLE groups (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        currency TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE group_members (
        group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (group_id, user_id)
    );
    CREATE TABLE games (
        id INTEGER PRIMARY KEY,
        group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        buy_in INTEGER NOT NULL,
        payout_bp TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        finish_kind TEXT,
        round_stack TEXT NOT NULL DEFAULT '[]',
        created_at INTEGER NOT NULL,
        finished_at INTEGER
    );
    CREATE INDEX games_group ON games(group_id);
    CREATE TABLE game_players (
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id),
        buy_ins INTEGER NOT NULL DEFAULT 1,
        bust_seq INTEGER,
        place INTEGER,
        deal_bp INTEGER,
        base_payout INTEGER,
        payout INTEGER,
        PRIMARY KEY (game_id, user_id)
    );
    CREATE INDEX game_players_user ON game_players(user_id);
    CREATE TABLE transfers (
        id INTEGER PRIMARY KEY,
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        from_user INTEGER NOT NULL REFERENCES users(id),
        to_user INTEGER NOT NULL REFERENCES users(id),
        amount INTEGER NOT NULL,
        paid INTEGER NOT NULL DEFAULT 0,
        paid_at INTEGER
    );
    CREATE INDEX transfers_game ON transfers(game_id);
    """,
    # v2: WhatsApp results through a wa_logger API token
    """
    CREATE TABLE wa_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        base_url TEXT NOT NULL,
        token_enc BLOB NOT NULL,
        pin TEXT
    );
    ALTER TABLE groups ADD COLUMN wa_mode TEXT NOT NULL DEFAULT 'off';
    ALTER TABLE groups ADD COLUMN wa_chat_id TEXT;
    ALTER TABLE groups ADD COLUMN wa_auto INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE users ADD COLUMN wa_chat_id TEXT;
    CREATE TABLE wa_sends (
        game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
        at INTEGER NOT NULL,
        ok INTEGER NOT NULL,
        detail TEXT NOT NULL
    );
    """,
]


def path() -> str:
    return os.path.join(config.DATA_DIR, "pokerbalance.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(path(), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(SCHEMA[version:], start=version + 1):
            conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    finally:
        conn.close()
    try:
        os.chmod(path(), 0o600)
    except OSError:
        pass


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction (BEGIN IMMEDIATE so concurrent writers queue instead of failing)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def now() -> int:
    return int(time.time())
