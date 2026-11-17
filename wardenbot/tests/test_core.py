from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from wardenbot import ranks, repo
from wardenbot.config import ConfigError, load_config
from wardenbot.db import connect
from wardenbot.slots import (
    SlotSpec,
    SupplySpec,
    parse_slots,
    parse_supply,
    parse_supply_progress,
    progress_bar,
)
from wardenbot.steam import parse_steam
from wardenbot.timeutil import (
    ParseError,
    parse_duration_hours,
    parse_local_datetime,
    resolve_tz,
    tz_from_name,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
MSK = resolve_tz("Europe/Moscow")[0]


# ------------------------------------------------------------------ time ----
def test_tz_variants():
    assert resolve_tz("МСК")[1] == "Europe/Moscow"
    assert resolve_tz("UTC+3")[1] == "UTC+03:00"
    assert resolve_tz("-05:30")[1] == "UTC-05:30"
    assert resolve_tz("utc")[1] == "UTC"
    with pytest.raises(ParseError):
        resolve_tz("Mars/Base")
    with pytest.raises(ParseError):
        resolve_tz("UTC+25")
    assert tz_from_name("garbage").utcoffset(None).total_seconds() == 0


def test_parse_datetime_iso_and_dmy():
    # 20:00 Moscow (UTC+3) == 17:00 UTC
    expected = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)
    assert parse_local_datetime("2026-11-08 20:00", MSK, NOW) == expected
    assert parse_local_datetime("08.11.2026 20:00", MSK, NOW) == expected
    assert parse_local_datetime("08.11 20:00", MSK, NOW) == expected
    assert parse_local_datetime("2026-11-08T20:00", MSK, NOW) == expected


def test_parse_datetime_relative():
    assert parse_local_datetime("завтра 21:30", MSK, NOW) == datetime(2026, 11, 6, 18, 30, tzinfo=UTC)
    assert parse_local_datetime("today 23:00", MSK, NOW) == datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    # 12:00 UTC is 15:00 in Moscow, so 14:00 has passed and rolls to tomorrow
    assert parse_local_datetime("14:00", MSK, NOW) == datetime(2026, 11, 6, 11, 0, tzinfo=UTC)


def test_parse_datetime_year_rollover():
    now = datetime(2026, 12, 30, 12, 0, tzinfo=UTC)
    assert parse_local_datetime("02.01 20:00", MSK, now) == datetime(2027, 1, 2, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize("text,code", [
    ("", "bad_datetime"), ("hello", "bad_datetime"), ("2026-13-40 20:00", "bad_datetime"),
    ("2026-11-08 25:00", "bad_datetime"), ("2020-01-01 20:00", "past"), ("2030-01-01 20:00", "too_far"),
    ("сегодня 01:00", "past"),
])
def test_parse_datetime_errors(text, code):
    with pytest.raises(ParseError) as exc:
        parse_local_datetime(text, MSK, NOW)
    assert exc.value.code == code


def test_duration():
    assert parse_duration_hours("2") == 120
    assert parse_duration_hours("1,5 ч") == 90
    assert parse_duration_hours("45m") == 45
    for bad in ("", "0", "abc", "30h", "5 мин"):
        with pytest.raises(ParseError):
            parse_duration_hours(bad)


# ----------------------------------------------------------------- slots ----
def test_parse_slots_formats():
    assert parse_slots("Пехота 20, ПТ 6; Танкисты: 4") == [
        SlotSpec("Пехота", 20), SlotSpec("ПТ", 6), SlotSpec("Танкисты", 4)]
    assert parse_slots("Medics=3\nEngineers - 4") == [SlotSpec("Medics", 3), SlotSpec("Engineers", 4)]
    assert parse_slots("20 пехота") == [SlotSpec("пехота", 20)]
    assert parse_slots("Пехота · Infantry 20") == [SlotSpec("Пехота · Infantry", 20)]


@pytest.mark.parametrize("text,code", [
    ("", "no_slots"), ("Пехота", "bad_slot_line"), ("Пехота 0", "bad_slot_capacity"),
    ("Пехота 999", "bad_slot_capacity"), ("Пехота 2, пехота 3", "dup_slot"),
    (", ".join(f"S{i} 1" for i in range(13)), "too_many_slots"),
])
def test_parse_slots_errors(text, code):
    with pytest.raises(ParseError) as exc:
        parse_slots(text)
    assert exc.value.code == code


def test_supply_parse_and_progress():
    items = parse_supply("BMats 5000, Топливо: 2к\n4 x Танки")
    assert items == [SupplySpec("BMats", 5000), SupplySpec("Топливо", 2000), SupplySpec("Танки", 4)]
    assert parse_supply("") == []
    with pytest.raises(ParseError):
        parse_supply("BMats")
    names = ["BMats", "Топливо"]
    assert parse_supply_progress("bmats 1200, Топливо +500", names) == {"BMats": (1200, False), "Топливо": (500, True)}
    with pytest.raises(ParseError) as exc:
        parse_supply_progress("Нефть 5", names)
    assert exc.value.code == "unknown_supply"


def test_progress_bar():
    assert progress_bar(0, 100) == "▱" * 8
    assert progress_bar(100, 100) == "▰" * 8
    assert progress_bar(1, 1000) == "▰" + "▱" * 7
    assert progress_bar(5, 0) == "▱" * 8


# ----------------------------------------------------------------- steam ----
def test_steam():
    sid = "76561198012345678"
    assert parse_steam(sid).key == f"id64:{sid}"
    assert parse_steam(f"https://steamcommunity.com/profiles/{sid}/").key == f"id64:{sid}"
    assert parse_steam("steamcommunity.com/id/Some_Name?x=1").key == "vanity:some_name"
    for bad in ("", "https://example.com/id/x", "1234", "https://steamcommunity.com/profiles/123"):
        with pytest.raises(ParseError):
            parse_steam(bad)


# ----------------------------------------------------------------- ranks ----
RULES = ranks.RankRules()


@pytest.mark.parametrize('count,expected', [(0,'recruit'),(6,'recruit'),(7,'private'),(29,'private'),(30,'sergeant'),(50,'sergeant')])
def test_auto_rank_progression(count, expected):
    assert ranks.auto_rank('recruit', NOW, NOW, False, count, RULES) == expected
    assert ranks.auto_rank('sergeant', NOW, NOW, False, count, RULES) == expected


def test_noshow_block():
    t = [NOW - timedelta(days=i) for i in range(5)]
    hist = [(t[0], False), (t[1], False), (t[2], False), (t[3], True)]
    assert ranks.noshow_block_until(hist, 3, 7) == t[0] + timedelta(days=7)
    assert ranks.noshow_block_until([(t[0], False), (t[1], True), (t[2], False)], 3, 7) is None
    assert ranks.noshow_block_until([(t[0], False)], 3, 7) is None
    assert ranks.noshow_block_until(hist, 0, 7) is None


# ---------------------------------------------------------------- config ----
def test_config(tmp_path):
    with pytest.raises(ConfigError):
        load_config({}, env_file=tmp_path / "missing")
    env_file = tmp_path / ".env"
    env_file.write_text('DISCORD_TOKEN="abc"\n# c\nDEV_GUILD_ID=42\nPRIVATE_AFTER_DAYS=3\n', encoding="utf-8")
    cfg = load_config({"LOG_LEVEL": "debug"}, env_file=env_file)
    assert (cfg.token, cfg.dev_guild_id, cfg.private_after_days, cfg.log_level) == ("abc", 42, 3, "DEBUG")
    with pytest.raises(ConfigError):
        load_config({"DISCORD_TOKEN": "x", "CHANNEL_NAME_STYLE": "bad"}, env_file=tmp_path / "none")
    with pytest.raises(ConfigError):
        load_config({"DISCORD_TOKEN": "x", "NOSHOW_LIMIT": "abc"}, env_file=tmp_path / "none")


# -------------------------------------------------------------------- db ----
@pytest.fixture()
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def _make_op(conn, slots=None, now=NOW):
    return repo.op_create(
        conn, guild_id=1, organizer_id=10, title="T", region="R", type="offense", language="ru",
        start_at=now + timedelta(hours=2), duration_min=120, requirements="", briefing_public="",
        briefing_closed="secret", slots=slots or [SlotSpec("Inf", 2), SlotSpec("AT", 1)],
        supply=[SupplySpec("BMats", 100)], now=now,
    )


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "x.db")
    connect(path).close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 8
    c.close()


def test_signup_capacity_move_and_leave(conn):
    op = _make_op(conn)
    inf, at = [s["id"] for s in repo.op_slots(conn, op)]
    assert repo.signup(conn, op, 1, inf, NOW).status == "ok"
    assert repo.signup(conn, op, 1, inf, NOW).status == "already"
    assert repo.signup(conn, op, 2, inf, NOW).status == "ok"
    assert repo.signup(conn, op, 3, inf, NOW).status == "full"
    assert repo.signup(conn, op, 1, at, NOW).status == "moved"
    assert repo.signup(conn, op, 3, inf, NOW).status == "ok"  # place freed by the move
    assert repo.signup(conn, op, 4, 9999, NOW).status == "no_slot"
    assert repo.leave(conn, op, 1) is True
    assert repo.leave(conn, op, 1) is False
    repo.op_update(conn, op, status="running")
    assert repo.signup(conn, op, 5, at, NOW).status == "ok"
    assert repo.op_signup_of(conn, op, 5)["late"] == 1
    repo.op_update(conn, op, status="done")
    assert repo.signup(conn, op, 6, at, NOW).status == "closed"
    assert repo.leave(conn, op, 5) is False  # cannot leave a finished operation


def test_finish_attendance_and_rank_inputs(conn):
    op = _make_op(conn)
    inf = repo.op_slots(conn, op)[0]["id"]
    for uid in (1, 2, 3):
        repo.signup(conn, op, uid, inf if uid < 3 else repo.op_slots(conn, op)[1]["id"], NOW)
    assert repo.mark_noshow(conn, op, 2) is True
    assert repo.mark_noshow(conn, op, 99) is False
    assert repo.confirm_attendance(conn, op, 1, True)
    assert repo.confirm_attendance(conn, op, 3, True)
    attendees = repo.op_finish(conn, op, NOW + timedelta(hours=3))
    assert sorted(attendees) == [1, 3]
    assert repo.op_finish(conn, op, NOW) == []  # already finished
    assert repo.attended_count(conn, 1) == 1 and repo.attended_count(conn, 2) == 0
    hist = repo.attendance_history(conn, 2, 5)
    assert hist and hist[0][1] is False


def test_supply_updates(conn):
    op = _make_op(conn)
    repo.supply_apply(conn, op, {"BMats": (30, False)})
    repo.supply_apply(conn, op, {"BMats": (20, True)})
    assert repo.op_supply(conn, op)[0]["collected"] == 50
    repo.supply_apply(conn, op, {"BMats": (500, True)})  # increments past the target are kept
    repo.supply_apply(conn, op, {"BMats": (-0, True)})
    assert repo.op_supply(conn, op)[0]["collected"] == 550


def test_cancel_and_double_decision(conn):
    op = _make_op(conn)
    assert repo.op_cancel(conn, op, NOW) is True
    assert repo.op_cancel(conn, op, NOW) is False

    rid = repo.req_create(conn, 1, 5, "WH-AAAA", "id64:1", "u", "n", "", "", "", NOW)
    repo.req_set_screenshot(conn, rid, "http://x/y.png")
    assert repo.req_decide(conn, rid, "approved", 9, "", NOW) is True
    assert repo.req_decide(conn, rid, "rejected", 9, "late", NOW) is False


def test_steam_conflicts_and_supersede(conn):
    r1 = repo.req_create(conn, 1, 5, "WH-AAAA", "id64:1", "u", "n", "", "", "", NOW)
    assert repo.steam_conflicts(conn, "id64:1", 6) == [5]
    assert repo.steam_conflicts(conn, "id64:1", 5) == []
    r2 = repo.req_create(conn, 1, 5, "WH-BBBB", "id64:1", "u", "n", "", "", "", NOW)
    assert repo.req_get(conn, r1)["status"] == "rejected"
    assert repo.req_get(conn, r2)["status"] == "awaiting_screenshot"
    assert repo.req_latest_for_user(conn, 5)["id"] == r2


def test_users_and_config(conn):
    repo.user_update(conn, 7, NOW, status="admitted", rank="recruit", steam_key="id64:7")
    assert repo.user_get(conn, 7)["rank"] == "recruit"
    with pytest.raises(ValueError):
        repo.user_update(conn, 7, NOW, bogus=1)
    assert [r["discord_id"] for r in repo.users_with_rank(conn, ["recruit"])] == [7]
    repo.cfg_set(conn, 1, "role.recruit", 123)
    repo.cfg_set(conn, 1, "role.recruit", 456)
    assert repo.cfg_get_int(conn, 1, "role.recruit") == 456
    assert repo.cfg_get(conn, 1, "none", "d") == "d"
    repo.vouch_add(conn, 1, 7, True, NOW)
    assert repo.vouch_add(conn, 1, 7, True, NOW) is False
    assert repo.vouch_valid_count(conn, 7) == 1
    repo.priority_set(conn, 1, 1, "a", "b", "c", "d", 5, NOW)
    repo.priority_set(conn, 1, 1, "a2", "b", "c", "d", 5, NOW)
    assert repo.priority_get(conn, 1, 1)["title_ru"] == "a2"
    assert repo.priority_delete(conn, 1, 1) is not None
    assert repo.priority_get(conn, 1, 1) is None


def test_backup_tool_makes_a_readable_copy_and_prunes(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import backup as backup_tool

    from wardenbot.db import Database

    db_file = tmp_path / "d" / "bot.db"
    db = Database(str(db_file))
    db.conn.execute("INSERT INTO guild_config(guild_id,key,value) VALUES (1,'k','v')")
    for i in range(3):
        copy = backup_tool.backup(db_file, tmp_path / "b", keep=2)
        import time; time.sleep(1.1)
    import sqlite3
    row = sqlite3.connect(copy).execute("SELECT value FROM guild_config").fetchone()
    assert row == ("v",)
    assert len(list((tmp_path / "b").glob("*.db"))) == 2
    db.close()



def test_signup_alone_and_cancelled_operations_never_award_attendance(conn):
    op = _make_op(conn)
    slot = repo.op_slots(conn, op)[0]['id']
    repo.signup(conn, op, 1, slot, NOW)
    assert repo.op_finish(conn, op, NOW) == []
    assert repo.attended_count(conn, 1) == 0
    # Explicit historical correction is possible and repeatable without double credit.
    assert repo.confirm_attendance(conn, op, 1, True)
    assert repo.confirm_attendance(conn, op, 1, True)
    assert repo.attended_count(conn, 1) == 1
    assert repo.confirm_attendance(conn, op, 1, False)
    assert repo.attended_count(conn, 1) == 0
    cancelled = _make_op(conn)
    slot = repo.op_slots(conn, cancelled)[0]['id']
    repo.signup(conn, cancelled, 1, slot, NOW)
    assert repo.confirm_attendance(conn, cancelled, 1, True)
    assert repo.op_cancel(conn, cancelled, NOW)
    assert not repo.confirm_attendance(conn, cancelled, 1, True)
    assert repo.attended_count(conn, 1) == 0
