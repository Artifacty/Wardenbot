"""Keep BotHost SQLite files under its persistent data directory."""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from collections.abc import MutableMapping
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)


def prepare_bot_host_db(root: Path, environ: MutableMapping[str, str]) -> Path:
    """Resolve an app-local DB path to BotHost storage, copying an existing DB once.

    This is called only from the root /app entry point. An absolute path outside
    the app may be a separately mounted disk, so it is left intact.
    """
    root = root.resolve()
    data_dir = Path(environ.get("DATA_DIR", "").strip() or root / "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    data_dir = data_dir.resolve()

    raw = environ.get("DB_PATH", "").strip()
    if raw == ":memory:":
        raise RuntimeError("BotHost не может сохранять данные с DB_PATH=:memory:. Укажите /app/data/wardenbot.db.")
    source = Path(raw) if raw else data_dir / "wardenbot.db"
    if not source.is_absolute():
        # main.py changes the working directory to /app/wardenbot before load_config.
        source = root / "wardenbot" / source
    source = source.resolve()

    target = data_dir / source.name if source.is_relative_to(root) and not source.is_relative_to(data_dir) else source
    if target != source:
        log.warning("DB_PATH=%s is in the replaceable app directory; using %s", source, target)
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="wardenbot-migration-", suffix=".db", dir=target.parent)
            os.close(fd)
            try:
                with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(temporary)) as new:
                    old.backup(new)
                    if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("SQLite copy failed integrity check")
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
            log.info("Copied existing SQLite data to %s", target)
    environ["DB_PATH"] = str(target)
    return target
