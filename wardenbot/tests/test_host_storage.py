import sqlite3

from wardenbot.host_storage import prepare_bot_host_db


def test_relative_bot_host_db_moves_to_persistent_storage(tmp_path):
    root = tmp_path / 'app'
    old = root / 'wardenbot' / 'data' / 'wardenbot.db'
    old.parent.mkdir(parents=True)
    with sqlite3.connect(old) as conn:
        conn.execute('CREATE TABLE users (discord_id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO users VALUES (123)')
        conn.execute('PRAGMA journal_mode=WAL')
    env = {'DB_PATH': 'data/wardenbot.db'}
    target = prepare_bot_host_db(root, env)
    assert target == root / 'data' / 'wardenbot.db'
    assert env['DB_PATH'] == str(target)
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT discord_id FROM users').fetchone()[0] == 123
    assert old.exists()


def test_existing_persistent_db_is_never_replaced(tmp_path):
    root = tmp_path / 'app'
    old = root / 'wardenbot' / 'data' / 'wardenbot.db'
    target = root / 'data' / 'wardenbot.db'
    for path, value in ((old, 1), (target, 2)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE users (discord_id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO users VALUES (?)', (value,))
    env = {'DB_PATH': 'data/wardenbot.db'}
    assert prepare_bot_host_db(root, env) == target
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT discord_id FROM users').fetchone()[0] == 2


def test_explicit_external_db_path_is_preserved(tmp_path):
    root = tmp_path / 'app'
    external = tmp_path / 'external' / 'bot.db'
    env = {'DB_PATH': str(external)}
    assert prepare_bot_host_db(root, env) == external
    assert env['DB_PATH'] == str(external)


def test_default_bot_host_db_path_is_persistent(tmp_path):
    root = tmp_path / 'app'
    env = {}
    assert prepare_bot_host_db(root, env) == root / 'data' / 'wardenbot.db'
