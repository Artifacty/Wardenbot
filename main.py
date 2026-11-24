"""Root entry point for bot hosting panels that auto-detect main.py."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "wardenbot"
sys.path.insert(0, str(APP))
os.chdir(APP)
# BotHost keeps /app/data across restarts; keep the SQLite file there.
if ROOT == Path("/app"):
    os.environ.setdefault("DB_PATH", str(ROOT / "data" / "wardenbot.db"))

from wardenbot.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
