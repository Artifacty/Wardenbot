"""Database operations. Every function takes a sqlite3 connection as the first argument
and is safe to call inside a transaction (see Database.write / Database.read)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .slots import SlotSpec, SupplySpec
from .timeutil import from_iso, to_iso

Row = sqlite3.Row


def _now_iso(now: datetime) -> str:
    return to_iso(now)


# ------------------------------------------------------------------ config --
def cfg_get(conn: sqlite3.Connection, guild_id: int, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM guild_config WHERE guild_id=? AND key=?", (guild_id, key)).fetchone()
    return row["value"] if row else default


def cfg_get_int(conn: sqlite3.Connection, guild_id: int, key: str) -> int | None:
    value = cfg_get(conn, guild_id, key)
    return int(value) if value and value.lstrip("-").isdigit() else None


def cfg_set(conn: sqlite3.Connection, guild_id: int, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO guild_config(guild_id, key, value) VALUES (?,?,?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
        (guild_id, key, str(value)),
    )


def cfg_all(conn: sqlite3.Connection, guild_id: int) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM guild_config WHERE guild_id=?", (guild_id,)).fetchall()
    return {r["key"]: r["value"] for r in rows}


# ------------------------------------------------------------------- users --
USER_FIELDS = {
    "tz", "status", "rank", "admitted_at", "admitted_by", "steam_key", "steam_url",
    "ingame_name", "regiment", "signup_blocked_until", "last_reject_at",
}


def user_ensure(conn: sqlite3.Connection, user_id: int, now: datetime) -> Row:
    conn.execute(
        "INSERT OR IGNORE INTO users(discord_id, created_at) VALUES (?,?)", (user_id, _now_iso(now))
    )
    return conn.execute("SELECT * FROM users WHERE discord_id=?", (user_id,)).fetchone()


def user_get(conn: sqlite3.Connection, user_id: int) -> Row | None:
    return conn.execute("SELECT * FROM users WHERE discord_id=?", (user_id,)).fetchone()


def user_update(conn: sqlite3.Connection, user_id: int, now: datetime, **fields: Any) -> Row:
    unknown = set(fields) - USER_FIELDS
    if unknown:
        raise ValueError(f"unknown user fields: {sorted(unknown)}")
    user_ensure(conn, user_id, now)
    if fields:
        assignments = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE users SET {assignments} WHERE discord_id=?", (*fields.values(), user_id))
    return conn.execute("SELECT * FROM users WHERE discord_id=?", (user_id,)).fetchone()


def users_with_rank(conn: sqlite3.Connection, ranks: Iterable[str]) -> list[Row]:
    ranks = list(ranks)
    marks = ",".join("?" for _ in ranks)
    return conn.execute(
        f"SELECT * FROM users WHERE status='admitted' AND rank IN ({marks})", ranks
    ).fetchall()


def steam_conflicts(conn: sqlite3.Connection, steam_key: str, exclude_user: int) -> list[int]:
    """Other Discord users that already used or requested this Steam profile."""
    rows = conn.execute(
        "SELECT discord_id AS uid FROM users WHERE steam_key=? AND discord_id<>? "
        "UNION SELECT user_id AS uid FROM verification_requests "
        "WHERE steam_key=? AND user_id<>? AND status IN ('awaiting_screenshot','pending','needs_info','approved')",
        (steam_key, exclude_user, steam_key, exclude_user),
    ).fetchall()
    return [r["uid"] for r in rows]


# ------------------------------------------------------------ verification --
def req_create(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    code: str,
    steam_key: str,
    steam_url: str,
    ingame_name: str,
    regiment: str,
    voucher_text: str,
    flags: str,
    now: datetime,
) -> int:
    # A new attempt replaces any unfinished one.
    conn.execute(
        "UPDATE verification_requests SET status='rejected', reason='superseded' "
        "WHERE user_id=? AND status IN ('awaiting_screenshot','needs_info')",
        (user_id,),
    )
    cur = conn.execute(
        "INSERT INTO verification_requests(guild_id,user_id,code,steam_key,steam_url,ingame_name,regiment,"
        "voucher_text,flags,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (guild_id, user_id, code, steam_key, steam_url, ingame_name, regiment, voucher_text, flags, _now_iso(now)),
    )
    return int(cur.lastrowid)


def req_get(conn: sqlite3.Connection, req_id: int) -> Row | None:
    return conn.execute("SELECT * FROM verification_requests WHERE id=?", (req_id,)).fetchone()


def req_latest_for_user(conn: sqlite3.Connection, user_id: int) -> Row | None:
    return conn.execute(
        "SELECT * FROM verification_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()


def req_set_screenshot(conn: sqlite3.Connection, req_id: int, url: str) -> bool:
    cur = conn.execute(
        "UPDATE verification_requests SET screenshot_url=?, status='pending' "
        "WHERE id=? AND status IN ('awaiting_screenshot','needs_info')", (url, req_id)
    )
    return cur.rowcount == 1


def req_attach_review(
    conn: sqlite3.Connection, req_id: int, expected_status: str,
    expected_message_id: int | None, filename: str, channel_id: int, message_id: int,
) -> bool:
    """Attach one review card only if the upload's snapshot is still current."""
    if expected_status not in ('awaiting_screenshot', 'needs_info'):
        return False
    cur = conn.execute(
        "UPDATE verification_requests SET screenshot_url=?, status='pending', "
        "review_channel_id=?, review_message_id=? WHERE id=? AND status=? "
        "AND review_message_id IS ?",
        (filename, channel_id, message_id, req_id, expected_status, expected_message_id),
    )
    return cur.rowcount == 1


def req_set_review_message(conn: sqlite3.Connection, req_id: int, channel_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE verification_requests SET review_channel_id=?, review_message_id=? WHERE id=?",
        (channel_id, message_id, req_id),
    )


def req_decide(
    conn: sqlite3.Connection, req_id: int, status: str, decided_by: int, reason: str, now: datetime
) -> bool:
    """Record a decision once. Returns False if the request was already decided."""
    if status not in ('approved', 'rejected', 'needs_info'):
        raise ValueError(f"invalid verification decision: {status}")
    cur = conn.execute(
        "UPDATE verification_requests SET status=?, decided_by=?, decided_at=?, reason=? "
        "WHERE id=? AND status IN ('pending','needs_info')",
        (status, decided_by, _now_iso(now), reason, req_id),
    )
    return cur.rowcount == 1


def req_pending(conn: sqlite3.Connection, guild_id: int) -> list[Row]:
    return conn.execute(
        "SELECT * FROM verification_requests WHERE guild_id=? AND status='pending' ORDER BY id", (guild_id,)
    ).fetchall()


# ------------------------------------------------------------------ vouches --
def vouch_add(conn: sqlite3.Connection, voucher_id: int, user_id: int, valid: bool, now: datetime) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO vouches(voucher_id,user_id,valid,created_at) VALUES (?,?,?,?)",
        (voucher_id, user_id, int(valid), _now_iso(now)),
    )
    return cur.rowcount == 1


def vouch_valid_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM vouches WHERE user_id=? AND valid=1", (user_id,)).fetchone()[0]


# --------------------------------------------------------------- operations --
@dataclass(frozen=True)
class SignupResult:
    status: str  # ok | moved | already | full | closed | no_slot
    slot_name: str = ""


def op_create(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    organizer_id: int,
    title: str,
    region: str,
    type: str,
    language: str,
    start_at: datetime,
    duration_min: int,
    requirements: str,
    briefing_public: str,
    briefing_closed: str,
    slots: list[SlotSpec],
    supply: list[SupplySpec],
    now: datetime,
) -> int:
    cur = conn.execute(
        "INSERT INTO operations(guild_id,organizer_id,title,region,type,language,start_at,duration_min,"
        "requirements,briefing_public,briefing_closed,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (guild_id, organizer_id, title, region, type, language, to_iso(start_at), duration_min,
         requirements, briefing_public, briefing_closed, _now_iso(now)),
    )
    op_id = int(cur.lastrowid)
    for pos, s in enumerate(slots):
        conn.execute("INSERT INTO op_slots(op_id,position,name,capacity) VALUES (?,?,?,?)", (op_id, pos, s.name, s.capacity))
    for pos, it in enumerate(supply):
        conn.execute("INSERT INTO op_supply(op_id,position,name,needed) VALUES (?,?,?,?)", (op_id, pos, it.name, it.needed))
    return op_id


OP_FIELDS = {
    "title", "region", "type", "language", "start_at", "duration_min", "requirements", "briefing_public",
    "briefing_closed", "status", "urgent", "forum_id", "thread_id", "message_id", "reminder60_sent",
    "reminder10_sent", "ended_at",
}


def op_update(conn: sqlite3.Connection, op_id: int, **fields: Any) -> None:
    unknown = set(fields) - OP_FIELDS
    if unknown:
        raise ValueError(f"unknown operation fields: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE operations SET {assignments} WHERE id=?", (*fields.values(), op_id))


def op_get(conn: sqlite3.Connection, op_id: int) -> Row | None:
    return conn.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()


def op_slots(conn: sqlite3.Connection, op_id: int) -> list[Row]:
    return conn.execute("SELECT * FROM op_slots WHERE op_id=? ORDER BY position", (op_id,)).fetchall()


def op_supply(conn: sqlite3.Connection, op_id: int) -> list[Row]:
    return conn.execute("SELECT * FROM op_supply WHERE op_id=? ORDER BY position", (op_id,)).fetchall()


def op_signups(conn: sqlite3.Connection, op_id: int) -> list[Row]:
    return conn.execute(
        "SELECT * FROM op_signups WHERE op_id=? ORDER BY created_at, user_id", (op_id,)
    ).fetchall()


def op_signup_of(conn: sqlite3.Connection, op_id: int, user_id: int) -> Row | None:
    return conn.execute("SELECT * FROM op_signups WHERE op_id=? AND user_id=?", (op_id, user_id)).fetchone()


def ops_active(conn: sqlite3.Connection) -> list[Row]:
    return conn.execute(
        "SELECT * FROM operations WHERE status IN ('open','running') ORDER BY start_at"
    ).fetchall()


def ops_recent(conn: sqlite3.Connection, guild_id: int, organizer_id: int | None, limit: int = 25) -> list[Row]:
    if organizer_id is None:
        return conn.execute(
            "SELECT * FROM operations WHERE guild_id=? ORDER BY id DESC LIMIT ?", (guild_id, limit)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM operations WHERE guild_id=? AND organizer_id=? ORDER BY id DESC LIMIT ?",
        (guild_id, organizer_id, limit),
    ).fetchall()


def signup(
    conn: sqlite3.Connection, op_id: int, user_id: int, slot_id: int, now: datetime
) -> SignupResult:
    """Sign a user up (or move them) atomically, respecting slot capacity."""
    op = op_get(conn, op_id)
    if op is None or op["status"] not in ("open", "running"):
        return SignupResult("closed")
    slot = conn.execute("SELECT * FROM op_slots WHERE id=? AND op_id=?", (slot_id, op_id)).fetchone()
    if slot is None:
        return SignupResult("no_slot")
    existing = op_signup_of(conn, op_id, user_id)
    if existing is not None and existing["slot_id"] == slot_id:
        return SignupResult("already", slot["name"])
    taken = conn.execute("SELECT COUNT(*) FROM op_signups WHERE slot_id=?", (slot_id,)).fetchone()[0]
    if taken >= slot["capacity"]:
        return SignupResult("full", slot["name"])
    late = int(op["status"] == "running")
    if existing is None:
        conn.execute(
            "INSERT INTO op_signups(op_id,user_id,slot_id,late,created_at) VALUES (?,?,?,?,?)",
            (op_id, user_id, slot_id, late, _now_iso(now)),
        )
        return SignupResult("ok", slot["name"])
    conn.execute("UPDATE op_signups SET slot_id=? WHERE op_id=? AND user_id=?", (slot_id, op_id, user_id))
    return SignupResult("moved", slot["name"])


def leave(conn: sqlite3.Connection, op_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM op_signups WHERE op_id=? AND user_id=? AND EXISTS "
        "(SELECT 1 FROM operations WHERE id=? AND status IN ('open','running'))",
        (op_id, user_id, op_id),
    )
    return cur.rowcount == 1


def supply_apply(conn: sqlite3.Connection, op_id: int, updates: dict[str, tuple[int, bool]]) -> None:
    """Apply {name: (amount, is_increment)} to an operation's supply list."""
    for name, (amount, increment) in updates.items():
        if increment:
            conn.execute(
                "UPDATE op_supply SET collected = MAX(0, collected + ?) WHERE op_id=? AND name=?",
                (amount, op_id, name),
            )
        else:
            conn.execute("UPDATE op_supply SET collected=? WHERE op_id=? AND name=?", (amount, op_id, name))


def op_finish(conn: sqlite3.Connection, op_id: int, now: datetime) -> list[int]:
    """Mark an operation done. Everybody who did not get an explicit no-show counts as present.
    Returns the user ids who attended."""
    op = op_get(conn, op_id)
    if op is None or op["status"] not in ("open", "running"):
        return []
    conn.execute("UPDATE op_signups SET attended=1 WHERE op_id=? AND attended IS NULL", (op_id,))
    conn.execute("UPDATE operations SET status='done', ended_at=? WHERE id=?", (_now_iso(now), op_id))
    rows = conn.execute("SELECT user_id FROM op_signups WHERE op_id=? AND attended=1", (op_id,)).fetchall()
    return [r["user_id"] for r in rows]


def op_cancel(conn: sqlite3.Connection, op_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE operations SET status='cancelled', ended_at=? WHERE id=? AND status IN ('open','running')",
        (_now_iso(now), op_id),
    )
    return cur.rowcount == 1


def op_close_unconfirmed(conn: sqlite3.Connection, op_id: int, now: datetime) -> bool:
    """Close an operation nobody finished. Attendance stays unconfirmed, so it counts for nobody."""
    cur = conn.execute(
        "UPDATE operations SET status='done', ended_at=? WHERE id=? AND status IN ('open','running')",
        (_now_iso(now), op_id),
    )
    return cur.rowcount == 1


def op_delete(conn: sqlite3.Connection, op_id: int) -> None:
    conn.execute("DELETE FROM operations WHERE id=?", (op_id,))


def mark_noshow(conn: sqlite3.Connection, op_id: int, user_id: int) -> bool:
    cur = conn.execute("UPDATE op_signups SET attended=0 WHERE op_id=? AND user_id=?", (op_id, user_id))
    return cur.rowcount == 1


def attended_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM op_signups s JOIN operations o ON o.id=s.op_id "
        "WHERE s.user_id=? AND s.attended=1 AND o.status='done'",
        (user_id,),
    ).fetchone()[0]


def attendance_history(conn: sqlite3.Connection, user_id: int, limit: int) -> list[tuple[datetime, bool]]:
    """(end time, attended) for finished operations, newest first."""
    rows = conn.execute(
        "SELECT o.ended_at AS ended, s.attended AS attended FROM op_signups s JOIN operations o ON o.id=s.op_id "
        "WHERE s.user_id=? AND o.status='done' AND s.attended IS NOT NULL AND o.ended_at IS NOT NULL "
        "ORDER BY o.ended_at DESC, o.id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [(from_iso(r["ended"]), bool(r["attended"])) for r in rows]


# --------------------------------------------------------------- priorities --
def priority_get(conn: sqlite3.Connection, guild_id: int, slot: int) -> Row | None:
    return conn.execute("SELECT * FROM priorities WHERE guild_id=? AND slot=?", (guild_id, slot)).fetchone()


def priority_set(
    conn: sqlite3.Connection, guild_id: int, slot: int, title_ru: str, title_en: str,
    body_ru: str, body_en: str, updated_by: int, now: datetime,
) -> None:
    conn.execute(
        "INSERT INTO priorities(guild_id,slot,title_ru,title_en,body_ru,body_en,updated_by,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,slot) DO UPDATE SET "
        "title_ru=excluded.title_ru, title_en=excluded.title_en, body_ru=excluded.body_ru, "
        "body_en=excluded.body_en, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (guild_id, slot, title_ru, title_en, body_ru, body_en, updated_by, _now_iso(now)),
    )


def priority_set_message(conn: sqlite3.Connection, guild_id: int, slot: int, channel_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE priorities SET channel_id=?, message_id=? WHERE guild_id=? AND slot=?",
        (channel_id, message_id, guild_id, slot),
    )


def priority_delete(conn: sqlite3.Connection, guild_id: int, slot: int) -> Row | None:
    row = priority_get(conn, guild_id, slot)
    conn.execute("DELETE FROM priorities WHERE guild_id=? AND slot=?", (guild_id, slot))
    return row


# -------------------------------------------------------------------- audit --
def audit_add(
    conn: sqlite3.Connection, guild_id: int, actor_id: int | None, action: str,
    target: str, details: str, now: datetime,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(guild_id,ts,actor_id,action,target,details) VALUES (?,?,?,?,?,?)",
        (guild_id, _now_iso(now), actor_id, action, target, details[:1000]),
    )
