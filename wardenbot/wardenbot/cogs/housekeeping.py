"""Background chores: hourly promotion check, join/leave logging, cleanup on leave."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from .. import repo
from ..bot import WardenBot
from ..services import audit, evaluate_member

log = logging.getLogger(__name__)


class HousekeepingCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.promotions.start()

    async def cog_unload(self) -> None:
        self.promotions.cancel()

    @tasks.loop(hours=1)
    async def promotions(self) -> None:
        """Private after N days with a valid vouch; Sergeant after enough operations."""
        users = await self.bot.db.read(repo.users_with_rank, ("recruit", "private"))
        for guild in self.bot.guilds:
            for row in users:
                if guild.get_member(row["discord_id"]) is None:
                    continue
                try:
                    await evaluate_member(self.bot, guild, row["discord_id"])
                except Exception:  # noqa: BLE001
                    log.exception("promotion check failed for %s", row["discord_id"])

    @promotions.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        age = (discord.utils.utcnow() - member.created_at).days
        await audit(self.bot, member.guild, None, "Join", member.mention, f"account age: {age} d")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        await audit(self.bot, member.guild, None, "Leave", f"{member} ({member.id})")


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(HousekeepingCog(bot))
