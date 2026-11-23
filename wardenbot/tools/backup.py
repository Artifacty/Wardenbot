"""Atomic, verified SQLite backups while the bot is running.

    python tools/backup.py [data/wardenbot.db] [backups]

Keeps the 14 newest successful copies. Partial copies never replace a valid backup.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def backup(db_path: Path, out_dir: Path, keep: int = 14) -> Path:
    if keep < 1:
        raise ValueError('keep must be positive')
    if not db_path.is_file():
        raise SystemExit(f'database not found: {db_path}')
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')
    descriptor, name = tempfile.mkstemp(prefix=f'wardenbot-{stamp}-', suffix='.db.partial', dir=out_dir)
    os.close(descriptor)
    partial = Path(name)
    target = partial.with_suffix('')
    try:
        src = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            dst = sqlite3.connect(partial)
            try:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed; previous backups retained')
            finally:
                dst.close()
        finally:
            src.close()
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    previous = sorted((path for path in out_dir.glob('wardenbot-*.db') if path != target),
                      key=lambda path: (path.stat().st_mtime_ns, path.name))
    obsolete = previous if keep == 1 else previous[:-(keep - 1)]
    for old in obsolete:
        old.unlink()
    return target


if __name__ == '__main__':
    db = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/wardenbot.db')
    out = Path(sys.argv[2] if len(sys.argv) > 2 else 'backups')
    print(backup(db, out))
