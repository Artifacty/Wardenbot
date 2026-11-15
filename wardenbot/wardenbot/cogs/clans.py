"""Russian staff registry; representatives share one guild-scoped clan record."""
import discord
from discord import app_commands
from discord.ext import commands
from .. import clans
from ..perms import can_staff, has_any
from ..services import reply, audit, sync_aux_roles
from ..i18n import user_language

ERRORS = {
    'name': ('Название: от 2 до 80 символов.', 'Use a name between 2 and 80 characters.'),
    'missing': ('Клан или представитель не найден.', 'Clan or representative not found.'),
    'permission': ('Недостаточно полномочий.', 'You do not have permission.'),
    'unverified': ('Сначала участник должен пройти проверку.', 'The member must complete verification first.'),
    'leader_exists': ('У клана уже есть глава. Сначала снимите его полномочия.', 'This clan already has a leader. Remove that assignment first.'),
    'duty': ('Неизвестные полномочия.', 'Invalid assignment.'),
}


class ClansCog(commands.Cog):
    def __init__(self, bot):
        self.bot=bot

    clan = app_commands.Group(name='clan',description='Clans and their representatives',guild_only=True)

    async def error(self,interaction,exc):
        lang=await user_language(interaction)
        await reply(interaction,ERRORS[str(exc)][0 if lang=='ru' else 1])

    @clan.command(name='register',description='Register a clan')
    async def register(self,interaction:discord.Interaction,name:app_commands.Range[str,2,80]):
        if not isinstance(interaction.user,discord.Member) or not can_staff(self.bot,interaction.user):
            await self.error(interaction,clans.ClanError('permission'))
            return
        try:
            cid=await self.bot.db.write(clans.register,interaction.guild.id,name)
        except clans.ClanError as exc:
            await self.error(interaction,exc)
            return
        await reply(interaction,f'Клан #{cid}: {discord.utils.escape_markdown(name)}',allowed_mentions=discord.AllowedMentions.none())
        await audit(self.bot,interaction.guild,interaction.user,'Регистрация клана',f'#{cid}',name)

    @clan.command(name='assign',description='Assign a clan representative')
    @app_commands.choices(duty=[app_commands.Choice(name='Clan Leader',value='leader'),
                               app_commands.Choice(name='Clan Delegate',value='delegate')])
    async def assign(self,interaction:discord.Interaction,clan:int,player:discord.Member,duty:app_commands.Choice[str]):
        actor=interaction.user
        if not isinstance(actor,discord.Member) or player.bot or not (can_staff(self.bot,actor) or has_any(self.bot,actor,('regiment_co',))):
            await self.error(interaction,clans.ClanError('permission'))
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.db.write(clans.assign,interaction.guild.id,actor.id,can_staff(self.bot,actor),clan,player.id,duty.value)
        except clans.ClanError as exc:
            await self.error(interaction,exc)
            return
        ready=await sync_aux_roles(self.bot,interaction.guild,player.id,'clan')
        lang=await user_language(interaction)
        await reply(interaction,('Назначение сохранено.' if ready else 'Назначение сохранено; роль ожидает повтора.') if lang=='ru'
                    else ('Assignment saved.' if ready else 'Assignment saved; role update will be retried.'))
        await audit(self.bot,interaction.guild,actor,'Назначение представителя',f'<@{player.id}>',f'Клан #{clan}; {duty.name}')

    @clan.command(name='remove',description='Remove a clan representative')
    async def remove(self,interaction:discord.Interaction,player:discord.Member):
        actor=interaction.user
        if not isinstance(actor,discord.Member) or not (can_staff(self.bot,actor) or has_any(self.bot,actor,('regiment_co',))):
            await self.error(interaction,clans.ClanError('permission'))
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.db.write(clans.remove,interaction.guild.id,actor.id,can_staff(self.bot,actor),player.id)
        except clans.ClanError as exc:
            await self.error(interaction,exc)
            return
        await sync_aux_roles(self.bot,interaction.guild,player.id,'clan')
        await reply(interaction,'Полномочия отозваны.' if await user_language(interaction)=='ru' else 'Authority revoked.')
        await audit(self.bot,interaction.guild,actor,'Снятие полномочий клана',f'<@{player.id}>')


async def setup(bot):
    await bot.add_cog(ClansCog(bot))
