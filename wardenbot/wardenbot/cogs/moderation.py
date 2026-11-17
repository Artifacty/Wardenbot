"""Administrative access lifecycle and Discord moderation with hierarchy checks."""
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from .. import access, repo
from ..perms import can_staff, can_admin
from ..services import reply, audit, utcnow, sync_member_roles, sync_aux_roles


def allowed(bot,actor,target,admin=False):
    if not isinstance(actor,discord.Member) or not (can_admin(bot,actor) if admin else can_staff(bot,actor)):
        return False
    return (target.id not in (actor.id,actor.guild.owner_id,bot.user.id)
        and (actor.id==actor.guild.owner_id or actor.top_role>target.top_role)
        and actor.guild.me.top_role>target.top_role)


class WarConfirm(discord.ui.View):
    def __init__(self,bot,number):
        super().__init__(timeout=300)
        self.bot,self.number=bot,number

    @discord.ui.button(label='Начать повторную проверку',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        if not isinstance(interaction.user,discord.Member) or not can_admin(self.bot,interaction.user):
            await reply(interaction,'Нужны права Admin.'); return
        await interaction.response.defer(ephemeral=True)
        ids=[m.id for m in interaction.guild.members if not m.bot and not can_staff(self.bot,m)]
        try:
            n=await self.bot.db.write(access.new_war,interaction.guild.id,self.number,ids,interaction.user.id,utcnow())
        except ValueError as exc:
            await reply(interaction,str(exc)); return
        await self.bot._load_cfg()
        await interaction.edit_original_response(content=f'Война {self.number}: повторная проверка для {n} участников. Роли обновляются очередью.',view=None)


class ModerationCog(commands.Cog):
    def __init__(self,bot): self.bot=bot
    moderation=app_commands.Group(name='moderation',description='Модерация и доступ',guild_only=True)

    @moderation.command(name='revoke',description='Отозвать допуск игрока; разрешить новую проверку')
    async def revoke(self,interaction:discord.Interaction,player:discord.Member,reason:app_commands.Range[str,1,500]):
        if not allowed(self.bot,interaction.user,player) or can_staff(self.bot,player):
            await reply(interaction,'Недостаточно прав. Для отзыва допуска администрации сначала снимите её управленческие роли.'); return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.write(access.revoke,interaction.guild.id,player.id,interaction.user.id,reason,utcnow())
        await sync_member_roles(self.bot,interaction.guild,player.id)
        await sync_aux_roles(self.bot,interaction.guild,player.id,'clan')
        await reply(interaction,'Допуск отозван. Записи сняты; закрытый брифинг недоступен. Игрок может пройти проверку заново.')

    @moderation.command(name='newwar',description='Запросить повторную проверку участников при новой войне')
    async def newwar(self,interaction:discord.Interaction,number:app_commands.Range[int,1,100000]):
        if not isinstance(interaction.user,discord.Member) or not can_admin(self.bot,interaction.user):
            await reply(interaction,'Нужны права Admin.'); return
        await reply(interaction,f'Война {number}: отозвать допуск у участников, кроме администрации? Записи на активные операции будут сняты; история участия сохранится.',view=WarConfirm(self.bot,number))

    @moderation.command(name='timeout',description='Ограничить общение на заданное число минут; 0 снимает мут')
    async def timeout(self,interaction:discord.Interaction,player:discord.Member,minutes:app_commands.Range[int,0,40320],reason:app_commands.Range[str,1,500]):
        if not allowed(self.bot,interaction.user,player) or player.guild_permissions.administrator:
            await reply(interaction,'Недостаточно прав или нарушена иерархия ролей.'); return
        await interaction.response.defer(ephemeral=True)
        await player.timeout(timedelta(minutes=minutes) if minutes else None,reason=reason)
        await audit(self.bot,interaction.guild,interaction.user,'Мут' if minutes else 'Снятие мута',str(player.id),f'{minutes} мин.; {reason}')
        await reply(interaction,'Ограничение обновлено.')

    @moderation.command(name='ban',description='Забанить участника с причиной; история сообщений сохраняется')
    async def ban(self,interaction:discord.Interaction,player:discord.Member,reason:app_commands.Range[str,1,500]):
        if not allowed(self.bot,interaction.user,player,admin=True):
            await reply(interaction,'Недостаточно прав или нарушена иерархия ролей.'); return
        await interaction.response.defer(ephemeral=True)
        await interaction.guild.ban(player,reason=reason,delete_message_seconds=0)
        await self.bot.db.write(access.revoke,interaction.guild.id,player.id,interaction.user.id,reason,utcnow(),True)
        await audit(self.bot,interaction.guild,interaction.user,'Бан',str(player.id),reason)
        await reply(interaction,'Участник заблокирован.')

    @moderation.command(name='unban',description='Разбанить игрока по Discord ID; допуск потребуется заново')
    async def unban(self,interaction:discord.Interaction,user_id:str,reason:app_commands.Range[str,1,500]):
        if not isinstance(interaction.user,discord.Member) or not can_admin(self.bot,interaction.user) or not user_id.isdigit():
            await reply(interaction,'Нужны права Admin и числовой Discord ID.'); return
        await interaction.response.defer(ephemeral=True)
        await interaction.guild.unban(discord.Object(id=int(user_id)),reason=reason)
        await self.bot.db.write(repo.user_update,int(user_id),utcnow(),status='new',rank=None)
        await audit(self.bot,interaction.guild,interaction.user,'Разбан',user_id,reason)
        await reply(interaction,'Блокировка снята. Требуется повторная проверка.')


async def setup(bot): await bot.add_cog(ModerationCog(bot))
