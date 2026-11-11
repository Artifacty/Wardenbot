"""Consistent copy of the SQLite database while the bot is running.

    python tools/backup.py [data/wardenbot.db] [backups]

Keeps the 14 newest copies. Run it from cron / a systemd timer (see README)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path


def backup(db_path: Path, out_dir: Path, keep: int = 14) -> Path:
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"wardenbot-{datetime.now(UTC):%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)  # safe with the bot running (WAL mode)
    finally:
        dst.close()
        src.close()
    for old in sorted(out_dir.glob("wardenbot-*.db"))[:-keep]:
        old.unlink()
    return target


if __name__ == "__main__":
    db = Path(sys.argv[1] if len(sys.argv) > 1 else "data/wardenbot.db")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "backups")
    print(backup(db, out))
