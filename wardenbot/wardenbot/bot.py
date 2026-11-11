"""The bot class: configuration cache, command sync and error handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from . import repo
from .config import Config
from .db import Database
from .ranks import RankRules
from .services import report_error

log = logging.getLogger(__name__)

EXTENSIONS = (
    "wardenbot.cogs.setup_cog",
    "wardenbot.cogs.verification",
    "wardenbot.cogs.operations",
    "wardenbot.cogs.priorities",
    "wardenbot.cogs.members",
    "wardenbot.cogs.selfroles",
    "wardenbot.cogs.housekeeping",
)


class WardenBot(commands.Bot):
    def __init__(self, config: Config, db: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True  # privileged: enable "Server Members Intent" in the Developer Portal
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.config = config
        self.db = db
        self.rank_rules = RankRules()
        self.role_sync_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._cfg: dict[int, dict[str, str]] = {}
        self.tree.on_error = self._on_tree_error  # type: ignore[assignment]

    # ----------------------------------------------------- guild config cache
    def cfg(self, guild_id: int) -> dict[str, str]:
        return self._cfg.setdefault(guild_id, {})

    async def set_cfg(self, guild_id: int, key: str, value: object) -> None:
        await self.db.write(repo.cfg_set, guild_id, key, value)
        self.cfg(guild_id)[key] = str(value)

    async def _load_cfg(self) -> None:
        def load(conn):
            rows = conn.execute("SELECT guild_id, key, value FROM guild_config").fetchall()
            return [(r["guild_id"], r["key"], r["value"]) for r in rows]

        for guild_id, key, value in await self.db.read(load):
            self.cfg(guild_id)[key] = value

    # ---------------------------------------------------------------- startup
    async def setup_hook(self) -> None:
        await self._load_cfg()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """Sync slash commands only when their definition changed (Discord rate-limits creation)."""
        payload = [c.to_dict(self.tree) for c in self.tree.get_commands()]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        scope = self.config.dev_guild_id or 0
        stored = self.cfg(0).get(f"commands_hash.{scope}")
        guild = discord.Object(id=self.config.dev_guild_id) if self.config.dev_guild_id else None
        if guild is not None:
            self.tree.copy_global_to(guild=guild)
        if stored == digest:
            log.info("Slash commands unchanged; skipping sync")
            return
        if guild is not None:
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        await self.set_cfg(0, f"commands_hash.{scope}", digest)
        log.info("Synced %d slash commands (%s)", len(synced), "dev guild" if scope else "global")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d guild(s)", self.user, getattr(self.user, "id", "?"), len(self.guilds))

    async def _on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        name = interaction.command.qualified_name if interaction.command else "?"
        await report_error(self, interaction, original, f"/{name}")
