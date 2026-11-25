"""Entry point: python -m wardenbot"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .bot import WardenBot
from .config import ConfigError, load_config
from .db import Database
from .instance_lock import instance_lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wardenbot: запуск Discord-бота")
    parser.add_argument("--sync-commands", action="store_true",
                        help="Принудительно зарегистрировать slash-команды перед подключением")
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        with instance_lock(config.db_path):
            db = Database(config.db_path)
            user_count = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            logging.getLogger(__name__).info("SQLite database: %s; player records: %d",
                                             Path(config.db_path).resolve(), user_count)
            bot = WardenBot(config, db, force_command_sync=args.sync_commands)
            try:
                bot.run(config.token, log_handler=None)
            finally:
                db.close()
    except RuntimeError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
