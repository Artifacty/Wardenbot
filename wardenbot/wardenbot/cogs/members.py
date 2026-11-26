"""Members: manual admission, ranks, vouching, time zone, help."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import repo
from ..bot import WardenBot
from ..i18n import user_language, parse_error_text, t
from ..layout import ROLE_BY_KEY
from ..perms import can_screen, can_staff, can_vouch, is_admitted
from ..services import audit, evaluate_member, reply, safe_dm, set_rank_roles, sync_member_roles, utcnow
from ..timeutil import ParseError, resolve_tz, to_iso, display_tz

log = logging.getLogger(__name__)
RANK_CHOICES = [app_commands.Choice(name=ROLE_BY_KEY[k].name, value=k) for k in ("recruit", "private", "sergeant")]


class MembersCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    member = app_commands.Group(name="member", description="Members · Участники", guild_only=True)
    timezone = app_commands.Group(name="timezone", description="Your time zone · Часовой пояс", guild_only=True)

    @member.command(name="admit", description="Admit a player without the screenshot flow · Допустить вручную")
    @app_commands.choices(rank=RANK_CHOICES)
    async def admit(self, interaction: discord.Interaction, player: discord.Member, rank: app_commands.Choice[str] | None = None) -> None:
        lang = await user_language(interaction)
        if not isinstance(interaction.user, discord.Member) or not can_screen(self.bot, interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        key = rank.value if rank else "recruit"
        now = utcnow()

        def work(conn):
            repo.user_update(
                conn, player.id, now, status="admitted", rank=key, admitted_at=to_iso(now), admitted_by=interaction.user.id,
            )
            repo.role_job_set(conn, interaction.guild.id, player.id, key)

        await interaction.response.defer(ephemeral=True)
        await self.bot.db.write(work)
        await sync_member_roles(self.bot, interaction.guild, player.id)
        await audit(self.bot, interaction.guild, interaction.user, "Ручной допуск", player.mention, key)  # type: ignore[arg-type]
        await interaction.followup.send(t("mem_admitted", lang, user=player.mention, rank=ROLE_BY_KEY[key].name),
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @member.command(name="rank", description="Set the rank · Установить звание")
    @app_commands.choices(rank=RANK_CHOICES)
    async def rank(self, interaction: discord.Interaction, player: discord.Member, rank: app_commands.Choice[str]) -> None:
        lang = await user_language(interaction)
        if not isinstance(interaction.user, discord.Member) or not can_staff(self.bot, interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        now = utcnow()
        def work(conn):
            user=repo.user_get(conn,player.id)
            if user is None or user['status']!='admitted':
                from ..operation_service import OperationError
                raise OperationError('Сначала допустите игрока через проверку или /member admit.')
            repo.user_update(conn, player.id, now, rank=rank.value)
            repo.role_job_set(conn, interaction.guild.id, player.id, rank.value)
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.write(work)
        await sync_member_roles(self.bot, interaction.guild, player.id)
        await audit(self.bot, interaction.guild, interaction.user, "Назначение роли", player.mention, rank.value)  # type: ignore[arg-type]
        await interaction.followup.send(t("mem_rank_set", lang, user=player.mention, rank=ROLE_BY_KEY[rank.value].name),
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @member.command(name="info", description="Show a player's record · Данные игрока")
    async def info(self, interaction: discord.Interaction, player: discord.Member) -> None:
        lang = await user_language(interaction)
        if not isinstance(interaction.user, discord.Member) or not can_screen(self.bot, interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return

        def work(conn):
            return repo.user_get(conn, player.id), repo.attended_count(conn, player.id), repo.vouch_valid_count(conn, player.id)

        await interaction.response.defer(ephemeral=True)
        user, ops, vouches = await self.bot.db.read(work)
        if user is None:
            await reply(interaction, t("mem_unknown", lang))
            return
        text = t(
            "mem_info", lang, user=player.mention, rank=user["rank"] or "—", status=user["status"],
            admitted=user["admitted_at"] or "—", ops=ops, vouches=vouches, steam=user["steam_url"] or "—",
            name=user["ingame_name"] or "—", regiment=user["regiment"] or "—",
        )
        await reply(interaction, text, allowed_mentions=discord.AllowedMentions.none())

    @member.command(name="vouch", description="Vouch for a player · Поручиться за игрока")
    async def vouch(self, interaction: discord.Interaction, player: discord.Member) -> None:
        lang = await user_language(interaction)
        voucher = interaction.user
        if not isinstance(voucher, discord.Member) or not can_vouch(self.bot, voucher):
            await reply(interaction, t("err_no_perm", lang))
            return
        if player.id == voucher.id:
            await reply(interaction, t("mem_vouch_self", lang))
            return
        if player.bot or not is_admitted(self.bot, player):
            await reply(interaction, t("mem_vouch_not_recruit", lang))
            return
        await interaction.response.defer(ephemeral=True)
        added = await self.bot.db.write(repo.vouch_add, voucher.id, player.id, True, utcnow())
        if not added:
            await reply(interaction, t("mem_vouch_dup", lang))
            return
        await audit(self.bot, interaction.guild, voucher, "Поручительство", player.mention)  # type: ignore[arg-type]
        await reply(interaction, t("mem_vouched", lang, user=player.mention), allowed_mentions=discord.AllowedMentions.none())
        await evaluate_member(self.bot, interaction.guild, player.id)  # type: ignore[arg-type]

    # ------------------------------------------------------------ time zone --
    @timezone.command(name="set", description="Set your time zone · Задать часовой пояс")
    @app_commands.describe(zone="МСК, UTC+3 …")
    async def tz_set(self, interaction: discord.Interaction, zone: str) -> None:
        lang = await user_language(interaction)
        try:
            _, name = resolve_tz(zone)
        except ParseError as exc:
            await reply(interaction, parse_error_text(exc, lang))
            return
        now = utcnow()
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.write(lambda conn: repo.user_update(conn, interaction.user.id, now, tz=name))
        await reply(interaction, t("tz_set", lang, tz=display_tz(name)))

    @timezone.command(name="show", description="Show your time zone · Показать часовой пояс")
    async def tz_show(self, interaction: discord.Interaction) -> None:
        lang = await user_language(interaction)
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.db.read(repo.user_get, interaction.user.id)
        name = (user["tz"] if user is not None and user["tz"] else None) or self.bot.config.default_tz
        await reply(interaction, t("tz_show", lang, tz=display_tz(name)))

    @app_commands.command(name="help", description="Commands and tips · Команды и подсказки")
    async def help(self, interaction: discord.Interaction) -> None:
        await reply(interaction, t("help_text", await user_language(interaction)))


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(MembersCog(bot))
