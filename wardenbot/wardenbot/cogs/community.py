"""Discord adapter for tickets, immediate groups and private activity rooms."""
import asyncio
import logging
import re

import discord
from discord.ext import commands, tasks

from .. import community as data, repo
from ..operation_service import OperationError
from ..perms import can_staff
from ..services import reply, interaction_errors, get_text_channel

log = logging.getLogger(__name__)


class Form(discord.ui.Modal):
    def __init__(self, kind, record=None):
        super().__init__(title='Небольшая группа · начинаем сейчас' if kind == 'group' else 'Помощь или отзыв')
        self.kind, self.pid = kind, record['id'] if record else None
        self.title_input = discord.ui.TextInput(
            label='Краткое название', max_length=70, default=record['title'] if record else None,
            placeholder='Два танка на вылазку' if kind == 'group' else 'Нужна помощь с доступом')
        self.body = discord.ui.TextInput(
            label='Что планируем?' if kind == 'group' else 'Опишите вопрос или отзыв',
            style=discord.TextStyle.paragraph, max_length=500 if kind == 'group' else 1000,
            default=record['body'] if record else None)
        self.add_item(self.title_input)
        self.add_item(self.body)
        if kind == 'group':
            self.capacity = discord.ui.TextInput(label='Всего человек, включая вас (1–20)',
                                                default=str(record['capacity']) if record else '4', max_length=2)
            self.add_item(self.capacity)

    @interaction_errors
    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog('CommunityCog')
        await cog.check(interaction)
        try:
            capacity = int(self.capacity.value) if self.kind == 'group' else 1
        except ValueError:
            raise OperationError('Количество должно быть целым числом от 1 до 20.')
        guild, owner = interaction.guild.id, interaction.user.id
        staff = can_staff(cog.bot, interaction.user)
        async with cog.lock:
            if self.pid:
                await cog.bot.db.write(data.edit, self.pid, guild, owner, staff,
                                       self.title_input.value, self.body.value, capacity)
                pid = self.pid
            else:
                pid = await cog.bot.db.write(data.create, guild, owner, self.kind,
                                             self.title_input.value, self.body.value, capacity, staff, str(interaction.id))
            await reply(interaction, 'Сохранено. Обновляю чат и карточку; при сбое бот повторит попытку автоматически.')
            await cog._post(pid)
        row = await cog.bot.db.read(data.get, pid)
        if row['channel_id'] and row['message_id']:
            await reply(interaction, f"Готово: https://discord.com/channels/{guild}/{row['channel_id']}/{row['message_id']}")


class Button(discord.ui.DynamicItem[discord.ui.Button],
             template=r'wb:community:(?P<id>\d+):(?P<action>ticket|group|join|leave|edit|close)'):
    def __init__(self, pid, action):
        labels = {'ticket': 'Помощь / оставить отзыв', 'group': 'Собрать небольшую группу',
                  'join': 'Присоединиться', 'leave': 'Выйти', 'edit': 'Изменить', 'close': 'Завершить / закрыть'}
        super().__init__(discord.ui.Button(
            label=labels[action], custom_id=f'wb:community:{pid}:{action}',
            style=discord.ButtonStyle.primary if action in ('group', 'ticket', 'join') else discord.ButtonStyle.secondary))
        self.pid, self.action = pid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['id']), match['action'])

    @interaction_errors
    async def callback(self, interaction):
        cog = interaction.client.get_cog('CommunityCog')
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise OperationError('Откройте кнопку на сервере.')
        if self.action in ('ticket', 'group', 'edit'):
            await cog.check(interaction)
            record = None
            if self.action == 'edit':
                record = await cog.bot.db.read(data.manageable, self.pid, interaction.guild.id,
                                                interaction.user.id, can_staff(cog.bot, interaction.user))
            await interaction.response.send_modal(Form('group' if record else self.action, record))
            return
        await interaction.response.defer(ephemeral=True)
        async with cog.lock:
            await cog.bot.db.write(data.act, self.pid, interaction.guild.id, interaction.user.id,
                                   can_staff(cog.bot, interaction.user), self.action)
            await cog._post(self.pid)
        await reply(interaction, 'Готово.')


def view(pid, actions):
    result = discord.ui.View(timeout=None)
    for action in actions:
        result.add_item(Button(pid, action))
    return result


class CommunityCog(commands.Cog):
    def __init__(self, bot):
        self.bot, self.lock = bot, asyncio.Lock()
        # Avoid re-fetching unchanged posts on every reconciliation tick. Reset on restart.
        self.synced = {}

    async def cog_load(self):
        self.bot.add_dynamic_items(Button)
        self.reconcile.start()

    async def cog_unload(self):
        self.reconcile.cancel()
        self.bot.remove_dynamic_items(Button)

    async def check(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise OperationError('Откройте форму на сервере.')
        await self.bot.db.read(data.require_access, interaction.user.id, can_staff(self.bot, interaction.user))

    def permissions(self, guild, users, closed=False):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, read_message_history=True, connect=True, move_members=True),
        }
        for key in ('command', 'staff'):
            rid = self.bot.cfg(guild.id).get('role.' + key)
            role = guild.get_role(int(rid)) if rid and rid.isdigit() else None
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                              read_message_history=True, connect=True)
        for uid in set(users):
            member = guild.get_member(uid)
            if member and member != guild.me:
                writable = not closed or can_staff(self.bot, member)
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=writable,
                    read_message_history=True, connect=writable, speak=writable, attach_files=True, embed_links=True)
        if len(overwrites) > 100:
            raise OperationError('Слишком много участников для приватных каналов Discord (100 разрешений).')
        return overwrites

    async def admitted_users(self, guild, users):
        admitted = await self.bot.db.read(lambda conn: {
            uid for uid in users if (user := repo.user_get(conn, uid)) and user['status'] == 'admitted'})
        return [uid for uid in users if uid in admitted or
                ((member := guild.get_member(uid)) and can_staff(self.bot, member))]

    async def category(self, guild, key, name):
        stored = self.bot.cfg(guild.id).get('cat.' + key)
        found = guild.get_channel(int(stored)) if stored and stored.isdigit() else None
        if found is None:
            found = discord.utils.get(guild.categories, name=name)
        if found is None:
            found = await guild.create_category(name, overwrites=self.permissions(guild, []))
        if str(found.id) != stored:
            await self.bot.set_cfg(guild.id, 'cat.' + key, found.id)
        return found

    async def room(self, guild, source, pid, role, title, users, category, closed=False):
        stored = await self.bot.db.read(lambda conn: conn.execute(
            'SELECT channel_id FROM activity_rooms WHERE guild_id=? AND source=? AND source_id=? AND role=?',
            (guild.id, source, pid, role)).fetchone())
        channel = guild.get_channel(stored[0]) if stored else None
        if stored and channel is None:
            try:
                channel = await guild.fetch_channel(stored[0])
            except discord.NotFound:
                pass
        name = f'{title[:55]} {role} · {pid}'
        is_text = role in ('подготовка', 'тикет')
        if is_text:
            name = re.sub(r'[^\w-]+', '-', name.lower()).strip('-')
        marker = f'wardenbot:{source}:{pid}:{role}'
        if channel is None:
            channel = next((c for c in category.channels if getattr(c, 'topic', None) == marker or
                            c.name == name or (not is_text and c.name.endswith(f' {role} · {pid}'))), None)
        perms = self.permissions(guild, users, closed)
        if channel is None:
            if closed:
                return None
            if is_text:
                channel = await guild.create_text_channel(name, category=category, topic=marker, overwrites=perms)
            else:
                channel = await guild.create_voice_channel(name, category=category, overwrites=perms)
        changes = {}
        if channel.overwrites != perms:
            changes['overwrites'] = perms
        if channel.name != name:
            changes['name'] = name
        if changes:
            await channel.edit(**changes)
        # Removing CONNECT does not disconnect a user already in voice.
        if not is_text:
            for member in list(channel.members):
                if member.id != guild.me.id and not can_staff(self.bot, member) and (closed or member.id not in users):
                    await member.move_to(None, reason='Доступ к группе или операции отозван')
        if not stored or stored[0] != channel.id:
            await self.bot.db.write(lambda conn: conn.execute(
                'INSERT INTO activity_rooms VALUES (?,?,?,?,?) ON CONFLICT(guild_id,source,source_id,role) DO UPDATE SET channel_id=excluded.channel_id',
                (guild.id, source, pid, role, channel.id)))
        return channel

    async def panels(self, guild):
        async with self.lock:
            support = get_text_channel(self.bot, guild, 'support')
            if support is None:
                support = discord.utils.get(guild.text_channels, name='помощь-и-отзывы')
            overwrites = self.permissions(guild, [])
            for key in ('recruit', 'private', 'sergeant'):
                rid = self.bot.cfg(guild.id).get('role.' + key)
                role = guild.get_role(int(rid)) if rid and rid.isdigit() else None
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
            if support is None:
                support = await guild.create_text_channel('помощь-и-отзывы', overwrites=overwrites)
            else:
                await support.edit(name='помощь-и-отзывы', overwrites=overwrites)
            await self.bot.set_cfg(guild.id, 'ch.support', support.id)
            setup = self.bot.get_cog('SetupCog')
            panels = [
                ('support', support, 'ticket', 'Нужна помощь или хотите оставить отзыв? Создайте приватный чат с модерацией.'),
                ('lfg', get_text_channel(self.bot, guild, 'lfg'), 'group',
                 'Соберите небольшую группу прямо сейчас: короткая форма, запись и отдельный войс. Вы уже включены в число участников.'),
            ]
            for key, channel, action, description in panels:
                if isinstance(channel, discord.TextChannel):
                    await setup._upsert(guild, key, channel, embeds=[discord.Embed(description=description, color=0x2F6FB5)], view=view(0, [action]))

    async def post(self, pid):
        async with self.lock:
            await self._post(pid)

    async def _post(self, pid):
        row = await self.bot.db.read(data.get, pid)
        if row is None or (guild := self.bot.get_guild(row['guild_id'])) is None:
            return
        staff_ids = {member.id for member in guild.members if can_staff(self.bot, member)}
        await self.bot.db.write(data.reconcile_access, pid, staff_ids)
        row = await self.bot.db.read(data.get, pid)
        closed = row['state'] != 'open'
        users = [row['owner_id']] if row['kind'] == 'ticket' else await self.bot.db.read(data.members, pid)
        if row['kind'] == 'ticket':
            category = await self.category(guild, 'tickets', 'Обращения к модерации')
            channel = await self.room(guild, 'ticket', pid, 'тикет', row['title'],
                                      await self.admitted_users(guild, users), category, closed)
            if channel is None:
                return
            description = row['body'] + f"\nАвтор: <@{row['owner_id']}>"
        else:
            category = await self.category(guild, 'small_groups', 'Небольшие группы')
            allowed = await self.admitted_users(guild, users)
            voice = await self.room(guild, 'group', pid, 'Сбор', row['title'], allowed, category, closed)
            channel = get_text_channel(self.bot, guild, 'lfg')
            if not channel:
                raise OperationError('Не найден канал поиска группы. Администратору нужно выполнить /setup run.')
            if not voice and not closed:
                return
            voice_text = voice.mention if voice else 'закрыт'
            description = (f"{row['body']}\nВойс: {voice_text}\nАвтор: <@{row['owner_id']}>\n"
                           f"Участники {len(users)}/{row['capacity']}: " + ' '.join(f'<@{uid}>' for uid in users))
        title = ('Закрыто · ' if closed else 'Идёт сейчас · ' if row['kind'] == 'group' else '') + row['title']
        embed = discord.Embed(title=title, description=description, color=0x2F6FB5)
        marker = f'wb:community:{pid}'
        embed.set_footer(text=marker)
        signature = (channel.id, row['message_id'], embed.to_dict())
        if self.synced.get(pid) == signature:
            return
        message = None
        if row['message_id'] and row['channel_id'] == channel.id:
            try:
                message = await channel.fetch_message(row['message_id'])
            except discord.NotFound:
                pass
        if message is None:
            # Reconcile a send that reached Discord before the process lost its acknowledgement.
            request_key = row['request_key']
            after = discord.Object(id=int(request_key)) if request_key and request_key.isdigit() else None
            async for candidate in channel.history(limit=None, after=after):
                if candidate.author.id == self.bot.user.id and any(e.footer.text == marker for e in candidate.embeds):
                    message = candidate
                    break
        controls = None if closed else view(pid, ['close'] if row['kind'] == 'ticket' else ['join', 'leave', 'edit', 'close'])
        if message:
            await message.edit(embed=embed, view=controls, allowed_mentions=discord.AllowedMentions.none())
        else:
            message = await channel.send(embed=embed, view=controls, allowed_mentions=discord.AllowedMentions.none())
        await self.bot.db.write(lambda conn: conn.execute('UPDATE community_posts SET channel_id=?,message_id=? WHERE id=?',
                                                         (channel.id, message.id, pid)))
        self.synced[pid] = (channel.id, message.id, embed.to_dict())

    async def sync_operation(self, op_id):
        """Run after publication, membership changes and edits; retry on the periodic loop."""
        async with self.lock:
            await self._operation_rooms(op_id)

    async def _operation_rooms(self, op_id):
        op = await self.bot.db.read(repo.op_get, op_id)
        if not op or not op['thread_id'] or (guild := self.bot.get_guild(op['guild_id'])) is None:
            return
        closed = op['status'] not in ('open', 'running')
        users = [op['organizer_id']]
        if not closed:
            users += [s['user_id'] for s in await self.bot.db.read(repo.op_signups, op_id)]
        users = await self.admitted_users(guild, users)
        category = await self.category(guild, 'operation_rooms', 'Каналы операций')
        for role in ('подготовка', 'Войс 1', 'Войс 2'):
            await self.room(guild, 'operation', op_id, role, op['title'], users, category, closed)

    @tasks.loop(seconds=30)
    async def reconcile(self):
        try:
            async with self.lock:
                rows = await self.bot.db.read(lambda conn: conn.execute('SELECT id FROM community_posts').fetchall())
                for row in rows:
                    try:
                        await self._post(row['id'])
                    except Exception:
                        log.exception('Community post %s awaits retry', row['id'])
                operations = await self.bot.db.read(lambda conn: conn.execute('SELECT id FROM operations WHERE thread_id IS NOT NULL').fetchall())
                for op in operations:
                    try:
                        await self._operation_rooms(op['id'])
                    except Exception:
                        log.exception('Operation rooms %s await retry', op['id'])
        except Exception:
            log.exception('Community reconciliation awaits retry')

    @reconcile.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CommunityCog(bot))
