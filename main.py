"""Root entry point for bot hosting panels that auto-detect main.py."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "wardenbot"
sys.path.insert(0, str(APP))
os.chdir(APP)
# BotHost keeps /app/data across restarts. Panel variables can override the
# Dockerfile default with a relative path inside the replaceable app directory.
if ROOT == Path("/app"):
    from wardenbot.host_storage import prepare_bot_host_db

    prepare_bot_host_db(ROOT, os.environ)

from wardenbot.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
