"""HQ objectives: up to five bilingual posts in the objectives channel, edited in place."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import repo
from ..bot import WardenBot
from ..i18n import lang_of, t
from ..perms import can_staff
from ..services import BLUE, audit, get_text_channel, reply, report_error, utcnow

log = logging.getLogger(__name__)
SLOTS = 5


def priority_embed(row) -> discord.Embed:
    embed = discord.Embed(title=f"{row['slot']}. {row['title_ru']} · {row['title_en']}"[:256], color=BLUE)
    embed.add_field(name="RU", value=row["body_ru"][:1024] or "—", inline=False)
    embed.add_field(name="EN", value=row["body_en"][:1024] or "—", inline=False)
    embed.set_footer(text=f"{row['updated_at']} UTC")
    return embed


class PriorityModal(discord.ui.Modal):
    def __init__(self, cog: "PrioritiesCog", slot: int, lang: str, row) -> None:
        super().__init__(title=f"{t('pri_modal_title', lang)} {slot}", timeout=900)
        self.cog, self.slot, self.lang = cog, slot, lang
        para = discord.TextStyle.paragraph
        g = (lambda k: row[k] if row is not None else None)
        self.title_ru = discord.ui.TextInput(label=t("pri_f_title_ru", lang), default=g("title_ru"), max_length=80)
        self.title_en = discord.ui.TextInput(label=t("pri_f_title_en", lang), default=g("title_en"), max_length=80)
        self.body_ru = discord.ui.TextInput(label=t("pri_f_body_ru", lang), default=g("body_ru"), style=para, max_length=900)
        self.body_en = discord.ui.TextInput(label=t("pri_f_body_en", lang), default=g("body_en"), style=para, max_length=900)
        for item in (self.title_ru, self.title_en, self.body_ru, self.body_en):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        channel = get_text_channel(self.cog.bot, guild, "objectives")
        if not isinstance(channel, discord.TextChannel):
            await reply(interaction, t("pri_no_channel", self.lang))
            return
        await interaction.response.defer(ephemeral=True)
        bot = self.cog.bot
        now = utcnow()
        await bot.db.write(
            repo.priority_set, guild.id, self.slot, self.title_ru.value.strip(), self.title_en.value.strip(),
            self.body_ru.value.strip(), self.body_en.value.strip(), interaction.user.id, now,
        )
        row = await bot.db.read(repo.priority_get, guild.id, self.slot)
        embed = priority_embed(row)
        message = None
        if row["message_id"]:
            try:
                message = await channel.fetch_message(row["message_id"])
                await message.edit(embed=embed)
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(embed=embed)
            await bot.db.write(repo.priority_set_message, guild.id, self.slot, channel.id, message.id)
        await audit(bot, guild, interaction.user, "Objective updated", f"#{self.slot}", self.title_en.value[:100])
        await interaction.followup.send(t("pri_saved", self.lang, slot=self.slot), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, "priority modal")  # type: ignore[arg-type]


class PrioritiesCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    priority = app_commands.Group(name="priority", description="HQ objectives · Цели штаба", guild_only=True)

    @priority.command(name="set", description="Publish or edit an objective · Опубликовать или изменить цель")
    @app_commands.describe(slot="Objective number 1-5 · Номер цели")
    async def set_(self, interaction: discord.Interaction, slot: app_commands.Range[int, 1, SLOTS]) -> None:
        lang = lang_of(interaction)
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None or not can_staff(self.bot, interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        row = await self.bot.db.read(repo.priority_get, interaction.guild.id, slot)
        await interaction.response.send_modal(PriorityModal(self, slot, lang, row))

    @priority.command(name="clear", description="Remove an objective · Удалить цель")
    async def clear(self, interaction: discord.Interaction, slot: app_commands.Range[int, 1, SLOTS]) -> None:
        lang = lang_of(interaction)
        guild = interaction.guild
        if not isinstance(interaction.user, discord.Member) or guild is None or not can_staff(self.bot, interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        row = await self.bot.db.write(repo.priority_delete, guild.id, slot)
        if row is None:
            await reply(interaction, t("pri_none", lang))
            return
        channel = get_text_channel(self.bot, guild, "objectives")
        if isinstance(channel, discord.TextChannel) and row["message_id"]:
            try:
                await channel.get_partial_message(row["message_id"]).delete()
            except discord.HTTPException:
                pass
        await audit(self.bot, guild, interaction.user, "Objective removed", f"#{slot}")
        await reply(interaction, t("pri_cleared", lang, slot=slot))


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(PrioritiesCog(bot))
