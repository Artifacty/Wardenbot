"""Self-service roles: language and ping roles as persistent toggle buttons."""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from .. import repo, role_jobs
from ..bot import WardenBot
from ..i18n import user_language, t
from ..layout import ROLE_BY_KEY
from ..perms import can_staff
from ..services import interaction_errors, get_role, report_error, reply, utcnow, sync_aux_roles

log = logging.getLogger(__name__)
SELF_ROLES = ("ping_ops", "ping_logi")
LABELS = {
    "lang_ru": ("Русский", discord.ButtonStyle.primary),
    "lang_en": ("English", discord.ButtonStyle.primary),
    "ping_ops": ("Пинг операций", discord.ButtonStyle.secondary),
    "ping_logi": ("Пинг логистики", discord.ButtonStyle.secondary),
}


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wb:role:(?P<key>lang_ru|lang_en|ping_ops|ping_logi)"):
    def __init__(self, key: str) -> None:
        label, style = LABELS[key]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"wb:role:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["key"])

    @interaction_errors
    async def callback(self, interaction: discord.Interaction) -> None:
        bot: WardenBot = interaction.client  # type: ignore[assignment]
        lang = await user_language(interaction)
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_guild_only", lang))
            return
        user = await bot.db.read(repo.user_get, member.id)
        if not can_staff(bot, member) and (user is None or user['status'] != 'admitted'):
            await reply(interaction, "Сначала пройдите проверку.")
            return
        if self.key in ('lang_ru', 'lang_en'):
            await reply(interaction, 'Сейчас сообщество использует русский язык.')
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



async def setup(bot: WardenBot) -> None:
    await bot.add_cog(SelfRolesCog(bot))
