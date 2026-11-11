"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file. Lines starting with # are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    token: str
    db_path: str = "data/wardenbot.db"
    default_tz: str = "Europe/Moscow"
    dev_guild_id: int | None = None
    channel_name_style: str = "full"  # full | ru | en
    log_level: str = "INFO"
    # Rank rules (starting values from the project plan).
    private_after_days: int = 7
    sergeant_after_ops: int = 10
    noshow_limit: int = 3
    noshow_block_days: int = 7
    # Verification.
    young_account_days: int = 14
    reject_cooldown_hours: int = 24
    # Operations.
    briefing_open_minutes: int = 60
    auto_close_hours: int = 6


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{key} must not be negative")
    return value


def load_config(env: Mapping[str, str] | None = None, env_file: str | Path = ".env") -> Config:
    """Build the config. Real environment variables win over the .env file."""
    merged: dict[str, str] = dict(read_env_file(Path(env_file)))
    merged.update(os.environ if env is None else env)

    token = merged.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    style = merged.get("CHANNEL_NAME_STYLE", "full").strip().lower() or "full"
    if style not in {"full", "ru", "en"}:
        raise ConfigError("CHANNEL_NAME_STYLE must be one of: full, ru, en")

    dev_guild = merged.get("DEV_GUILD_ID", "").strip()
    return Config(
        token=token,
        db_path=merged.get("DB_PATH", "data/wardenbot.db").strip() or "data/wardenbot.db",
        default_tz=merged.get("DEFAULT_TZ", "Europe/Moscow").strip() or "Europe/Moscow",
        dev_guild_id=int(dev_guild) if dev_guild.isdigit() else None,
        channel_name_style=style,
        log_level=(merged.get("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
        private_after_days=_int(merged, "PRIVATE_AFTER_DAYS", 7),
        sergeant_after_ops=_int(merged, "SERGEANT_AFTER_OPS", 10),
        noshow_limit=_int(merged, "NOSHOW_LIMIT", 3),
        noshow_block_days=_int(merged, "NOSHOW_BLOCK_DAYS", 7),
        young_account_days=_int(merged, "YOUNG_ACCOUNT_DAYS", 14),
        reject_cooldown_hours=_int(merged, "REJECT_COOLDOWN_HOURS", 24),
        briefing_open_minutes=_int(merged, "BRIEFING_OPEN_MINUTES", 60),
        auto_close_hours=_int(merged, "AUTO_CLOSE_HOURS", 6),
    )
