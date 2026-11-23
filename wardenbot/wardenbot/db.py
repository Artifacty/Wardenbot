"""SQLite storage. Uses only the standard library; calls run in a worker thread."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

MIGRATIONS: list[str] = [
    # ---- v1 ----
    """
    CREATE TABLE guild_config (
        guild_id INTEGER NOT NULL,
        key      TEXT    NOT NULL,
        value    TEXT    NOT NULL,
        PRIMARY KEY (guild_id, key)
    );

    CREATE TABLE users (
        discord_id         INTEGER PRIMARY KEY,
        tz                 TEXT,
        status             TEXT NOT NULL DEFAULT 'new',   -- new | pending | admitted | rejected | banned
        rank               TEXT,                          -- recruit | private | sergeant
        admitted_at        TEXT,
        admitted_by        INTEGER,
        steam_key          TEXT,
        steam_url          TEXT,
        ingame_name        TEXT,
        regiment           TEXT,
        signup_blocked_until TEXT,
        last_reject_at     TEXT,
        created_at         TEXT NOT NULL
    );
    CREATE INDEX idx_users_steam ON users(steam_key);

    CREATE TABLE verification_requests (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id       INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        code           TEXT    NOT NULL,
        steam_key      TEXT    NOT NULL,
        steam_url      TEXT    NOT NULL,
        ingame_name    TEXT    NOT NULL,
        regiment       TEXT    NOT NULL DEFAULT '',
        voucher_text   TEXT    NOT NULL DEFAULT '',
        flags          TEXT    NOT NULL DEFAULT '',
        screenshot_url TEXT,
        status         TEXT    NOT NULL DEFAULT 'awaiting_screenshot',
            -- awaiting_screenshot | pending | approved | rejected | needs_info
        review_channel_id INTEGER,
        review_message_id INTEGER,
        decided_by     INTEGER,
        decided_at     TEXT,
        reason         TEXT,
        created_at     TEXT NOT NULL
    );
    CREATE INDEX idx_vr_user ON verification_requests(user_id);
    CREATE INDEX idx_vr_steam ON verification_requests(steam_key);

    CREATE TABLE vouches (
        voucher_id INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        valid      INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY (voucher_id, user_id)
    );

    CREATE TABLE operations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id        INTEGER NOT NULL,
        organizer_id    INTEGER NOT NULL,
        title           TEXT    NOT NULL,
        region          TEXT    NOT NULL DEFAULT '',
        type            TEXT    NOT NULL,
        language        TEXT    NOT NULL,
        start_at        TEXT    NOT NULL,
        duration_min    INTEGER NOT NULL,
        requirements    TEXT    NOT NULL DEFAULT '',
        briefing_public TEXT    NOT NULL DEFAULT '',
        briefing_closed TEXT    NOT NULL DEFAULT '',
        status          TEXT    NOT NULL DEFAULT 'open',  -- open | running | done | cancelled
        urgent          INTEGER NOT NULL DEFAULT 0,
        forum_id        INTEGER,
        thread_id       INTEGER,
        message_id      INTEGER,
        reminder60_sent INTEGER NOT NULL DEFAULT 0,
        reminder10_sent INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL,
        ended_at        TEXT
    );
    CREATE INDEX idx_ops_status ON operations(status, start_at);

    CREATE TABLE op_slots (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id    INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        name     TEXT    NOT NULL,
        capacity INTEGER NOT NULL
    );
    CREATE INDEX idx_slots_op ON op_slots(op_id);

    CREATE TABLE op_signups (
        op_id      INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL,
        slot_id    INTEGER NOT NULL REFERENCES op_slots(id) ON DELETE CASCADE,
        late       INTEGER NOT NULL DEFAULT 0,
        attended   INTEGER,
        created_at TEXT NOT NULL,
        PRIMARY KEY (op_id, user_id)
    );
    CREATE INDEX idx_signups_user ON op_signups(user_id);

    CREATE TABLE op_supply (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id     INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
        position  INTEGER NOT NULL,
        name      TEXT    NOT NULL,
        needed    INTEGER NOT NULL,
        collected INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_supply_op ON op_supply(op_id);

    CREATE TABLE priorities (
        guild_id   INTEGER NOT NULL,
        slot       INTEGER NOT NULL,
        title_ru   TEXT NOT NULL,
        title_en   TEXT NOT NULL,
        body_ru    TEXT NOT NULL,
        body_en    TEXT NOT NULL,
        channel_id INTEGER,
        message_id INTEGER,
        updated_by INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (guild_id, slot)
    );

    CREATE TABLE audit_log (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        ts       TEXT    NOT NULL,
        actor_id INTEGER,
        action   TEXT    NOT NULL,
        target   TEXT,
        details  TEXT
    );
    """,
    # v2: language preferences and durable desired Discord roles.
    """
    ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'en' CHECK(language IN ('ru','en'));
    ALTER TABLE verification_requests ADD COLUMN language TEXT NOT NULL DEFAULT 'en' CHECK(language IN ('ru','en'));
    CREATE TABLE role_jobs (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        target TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        applied_revision INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        PRIMARY KEY(guild_id, user_id)
    );
    """,
    # v3: distinguish explicit attendance from legacy auto-counted signups.
    """
    ALTER TABLE op_signups ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0;
    """,
    # v4: independently retry language and clan role assignments.
    """
    CREATE TABLE aux_role_jobs (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, scope TEXT NOT NULL,
        target TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        applied_revision INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        PRIMARY KEY(guild_id,user_id,scope)
    );
    """,
    # v5: shared clan authority and stable operation ownership.
    """
    CREATE TABLE clans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        name TEXT NOT NULL, name_key TEXT NOT NULL, UNIQUE(guild_id,name_key)
    );
    CREATE TABLE clan_members (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        clan_id INTEGER NOT NULL REFERENCES clans(id),
        duty TEXT NOT NULL CHECK(duty IN ('leader','delegate')),
        PRIMARY KEY(guild_id,user_id)
    );
    CREATE UNIQUE INDEX one_clan_leader ON clan_members(clan_id) WHERE duty='leader';
    ALTER TABLE operations ADD COLUMN clan_id INTEGER REFERENCES clans(id);
    """,
    # v6: archive receipts survive restarts; delete the source only after delivery.
    """
    UPDATE aux_role_jobs SET applied_revision=revision WHERE scope='language';
    CREATE TABLE operation_archives (
        op_id INTEGER PRIMARY KEY REFERENCES operations(id),
        channel_id INTEGER NOT NULL, payload TEXT NOT NULL,
        message_id INTEGER, source_deleted INTEGER NOT NULL DEFAULT 0
    );
    """,
    # v7: operation revisions, delivery ledger and durable public notifications.
    """
    ALTER TABLE operations ADD COLUMN card_revision INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE operations ADD COLUMN card_applied INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE operations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE operations ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/Moscow';
    ALTER TABLE operations ADD COLUMN signup_open INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE operations ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT '';
    ALTER TABLE operations ADD COLUMN publication_key TEXT;
    CREATE UNIQUE INDEX op_publication_key ON operations(publication_key) WHERE publication_key IS NOT NULL;
    CREATE TABLE supply_pledges (
        op_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
        supply_id INTEGER NOT NULL REFERENCES op_supply(id), user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL CHECK(amount >= 0), delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered >= 0),
        PRIMARY KEY(supply_id,user_id), CHECK(delivered <= amount)
    );
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY, op_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL, recipients TEXT NOT NULL DEFAULT '[]',
        state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, created_at TEXT NOT NULL, UNIQUE(op_id,kind,revision)
    );
    INSERT INTO notifications(op_id,kind,revision,content,state,created_at)
        SELECT id,'remind60',1,'Legacy reminder','sent',created_at FROM operations WHERE reminder60_sent=1;
    INSERT INTO notifications(op_id,kind,revision,content,state,created_at)
        SELECT id,'remind15',1,'Legacy reminder','sent',created_at FROM operations WHERE reminder10_sent=1;

    """,

    """
    CREATE TABLE community_posts (
        id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
        kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, capacity INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'open', channel_id INTEGER, message_id INTEGER, request_key TEXT UNIQUE
    );
    CREATE TABLE group_members (
        post_id INTEGER NOT NULL REFERENCES community_posts(id), user_id INTEGER NOT NULL,
        PRIMARY KEY(post_id,user_id)
    );
    CREATE TABLE activity_rooms (
        guild_id INTEGER NOT NULL, source TEXT NOT NULL, source_id INTEGER NOT NULL,
        role TEXT NOT NULL, channel_id INTEGER NOT NULL, PRIMARY KEY(guild_id,source,source_id,role)
    );
    """,
    """
    ALTER TABLE community_posts ADD COLUMN ticket_type TEXT NOT NULL DEFAULT 'question';
    """,

]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        log.info("Applying database migration v%d", version)
        conn.execute("BEGIN")
        try:
            for statement in _split_script(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)


def _split_script(script: str) -> list[str]:
    """Split on semicolons that end a statement (the schema has no triggers or strings with ';')."""
    statements = []
    for chunk in script.split(";"):
        cleaned = "\n".join(
            line.split("--", 1)[0] if "--" in line else line for line in chunk.splitlines()
        ).strip()
        if cleaned:
            statements.append(cleaned)
    return statements


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


class Database:
    """Async facade: runs repository functions in a worker thread, one at a time."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = connect(path)
        self._lock = threading.Lock()

    def _run(self, fn: Callable[..., T], read_only: bool, args: tuple, kwargs: dict[str, Any]) -> T:
        with self._lock:
            if read_only:
                return fn(self.conn, *args, **kwargs)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self.conn, *args, **kwargs)
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")
            return result

    async def write(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(self._run, fn, False, args, kwargs)

    async def read(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(self._run, fn, True, args, kwargs)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
