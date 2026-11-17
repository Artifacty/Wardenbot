"""Entry point: python -m wardenbot"""

from __future__ import annotations

import logging
import sys

from .bot import WardenBot
from .config import ConfigError, load_config
from .db import Database
from .instance_lock import instance_lock


def main() -> int:
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
            bot = WardenBot(config, db)
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
