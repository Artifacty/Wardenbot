"""Background chores: hourly promotion check, join/leave logging, cleanup on leave."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from .. import repo
from ..bot import WardenBot
from ..services import audit, evaluate_member, sync_member_roles

log = logging.getLogger(__name__)


class HousekeepingCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.promotions.start()
        self.role_sync.start()

    async def cog_unload(self) -> None:
        self.promotions.cancel()
        self.role_sync.cancel()

    @tasks.loop(hours=1)
    async def promotions(self) -> None:
        """Reconcile ranks from confirmed attendance totals."""
        users = await self.bot.db.read(repo.users_with_rank, ("recruit", "private", "sergeant"))
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

    @tasks.loop(seconds=30)
    async def role_sync(self) -> None:
        for job in await self.bot.db.read(repo.role_jobs_pending):
            guild = self.bot.get_guild(job['guild_id'])
            if guild is None:
                continue
            try:
                await sync_member_roles(self.bot, guild, job['user_id'])
            except Exception:
                log.exception("Role synchronization failed for %s", job['user_id'])

    @role_sync.before_loop
    async def _before_roles(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        def queue(conn):
            user = repo.user_get(conn, member.id)
            target = (user['rank'] or 'recruit') if user and user['status'] == 'admitted' else (
                'rejected' if user and user['status'] in ('rejected', 'banned') else 'unverified')
            repo.role_job_set(conn, member.guild.id, member.id, target)
        await self.bot.db.write(queue)
        await sync_member_roles(self.bot, member.guild, member.id)
        age = (discord.utils.utcnow() - member.created_at).days
        await audit(self.bot, member.guild, None, "Вход на сервер", member.mention, f"Возраст аккаунта: {age} д.")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        await audit(self.bot, member.guild, None, "Выход с сервера", f"{member} ({member.id})")


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(HousekeepingCog(bot))
