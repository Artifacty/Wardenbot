"""Self-service roles: language and ping roles as persistent toggle buttons."""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from ..bot import WardenBot
from ..i18n import lang_of, t
from ..layout import ROLE_BY_KEY
from ..services import get_role, report_error, reply

log = logging.getLogger(__name__)
SELF_ROLES = ("lang_ru", "lang_en", "ping_ops", "ping_logi")
LABELS = {
    "lang_ru": ("Русский", discord.ButtonStyle.primary),
    "lang_en": ("English", discord.ButtonStyle.primary),
    "ping_ops": ("Пинг операций · Ops ping", discord.ButtonStyle.secondary),
    "ping_logi": ("Пинг логистики · Logi ping", discord.ButtonStyle.secondary),
}


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wb:role:(?P<key>lang_ru|lang_en|ping_ops|ping_logi)"):
    def __init__(self, key: str) -> None:
        label, style = LABELS[key]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"wb:role:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: WardenBot = interaction.client  # type: ignore[assignment]
        lang = lang_of(interaction)
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_guild_only", lang))
            return
        role = get_role(bot, interaction.guild, self.key)
        if role is None:
            await reply(interaction, t("role_missing", lang))
            return
        name = ROLE_BY_KEY[self.key].ru if lang == "ru" else ROLE_BY_KEY[self.key].en
        if role in member.roles:
            await member.remove_roles(role, reason="Self role")
            await reply(interaction, t("role_removed", lang, role=name))
        else:
            await member.add_roles(role, reason="Self role")
            await reply(interaction, t("role_added", lang, role=name))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, "self role")  # type: ignore[arg-type]


def build_roles_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for key in SELF_ROLES:
        view.add_item(RoleButton(key))
    return view


class SelfRolesCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton)


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(SelfRolesCog(bot))
