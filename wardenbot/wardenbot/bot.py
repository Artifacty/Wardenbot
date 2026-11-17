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
    "wardenbot.cogs.menu",
    "wardenbot.cogs.community",
    "wardenbot.cogs.moderation",
    "wardenbot.cogs.clans",
    "wardenbot.cogs.housekeeping",
)


class WardenBot(commands.Bot):
    def __init__(self, config: Config, db: Database, *, force_command_sync: bool = False) -> None:
        intents = discord.Intents.default()
        intents.message_content = config.message_content_intent
        intents.members = True  # privileged: enable "Server Members Intent" in the Developer Portal
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.config = config
        self.force_command_sync = force_command_sync
        self.db = db
        self.rank_rules = RankRules()
        self.role_sync_locks: dict[tuple, asyncio.Lock] = {}
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
        await self._sync_commands(force=self.force_command_sync)

    async def _sync_commands(self, force: bool = False) -> None:
        """Check remote registration even when local definitions have not changed."""
        from .command_locale import CommandTranslator
        if self.tree.translator is None:
            await self.tree.set_translator(CommandTranslator())
        payload = [await c.get_translated_payload(self.tree, self.tree.translator) for c in self.tree.get_commands()]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        scope = self.config.dev_guild_id or 0
        label = f"server {scope} (DEV_GUILD_ID)" if scope else "global (all servers)"
        log.info("Checking slash commands: application=%s, scope=%s, force=%s", self.application_id, label, force)
        cache_key = f"commands_hash.{self.application_id}.{scope}"
        stored = self.cfg(0).get(cache_key)
        guild = discord.Object(id=scope) if scope else None
        if guild is not None:
            self.tree.copy_global_to(guild=guild)

        needs_sync = force or stored != digest
        if not needs_sync:
            try:
                remote = await self.tree.fetch_commands(guild=guild)
            except discord.HTTPException as error:
                log.warning("Could not verify slash commands (%s, Discord code %s). "
                            "Keeping cached registration; use --sync-commands to repair.", label, error.code)
                return
            expected = {(item['name'], item.get('type', 1)) for item in payload}
            actual = {(command.name, command.type.value) for command in remote}
            needs_sync = actual != expected
            if needs_sync:
                log.warning("Discord command list differs from local cache (%s); restoring registration", label)
            else:
                log.info("Verified %d slash commands on Discord (%s)", len(remote), label)
        if needs_sync:
            synced = await self.tree.sync(guild=guild) if guild else await self.tree.sync()
            await self.set_cfg(0, cache_key, digest)
            log.info("Synced %d slash commands (%s)", len(synced), label)

        # Remove an obsolete test-server registration only AFTER its replacement is ready.
        old_scope = self.cfg(0).get(f'commands_scope.{self.application_id}')
        legacy_scopes = {int(k.split('.')[-1]) for k in self.cfg(0)
                         if k.startswith('commands_hash.') and len(k.split('.')) == 2 and k.split('.')[-1].isdigit()}
        stale = ({int(old_scope)} if old_scope and old_scope.isdigit() else legacy_scopes) - {0, scope}
        cleaned = True
        for old in stale:
            old_guild = discord.Object(id=old)
            self.tree.clear_commands(guild=old_guild)
            try:
                await self.tree.sync(guild=old_guild)
            except discord.HTTPException:
                cleaned = False
                log.warning('Could not clear old command scope %s; retry with /setup sync', old)
        if cleaned:
            await self.set_cfg(0, f'commands_scope.{self.application_id}', scope)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d guild(s)", self.user, getattr(self.user, "id", "?"), len(self.guilds))
        for guild in self.guilds:
            log.info("Discord server: %s (id=%s)", guild.name, guild.id)
        scope = self.config.dev_guild_id
        if scope:
            if self.get_guild(scope) is None:
                log.warning("DEV_GUILD_ID=%s is not among connected servers. Clear DEV_GUILD_ID for global commands.", scope)
            elif len(self.guilds) > 1:
                log.warning("Commands are registered for server %s only. Clear DEV_GUILD_ID for commands on all servers.", scope)

    async def _on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        from .operation_service import OperationError
        if isinstance(original, OperationError):
            from .services import reply
            await reply(interaction, str(original))
            return
        name = interaction.command.qualified_name if interaction.command else "?"
        await report_error(self, interaction, original, f"/{name}")
