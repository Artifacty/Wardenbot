"""Discord adapter for tickets, immediate groups and private activity rooms."""
import asyncio
import logging
import re
import time

import discord
from discord.ext import commands, tasks

from .. import community as data, repo
from ..operation_service import OperationError
from ..perms import can_staff
from ..services import reply, interaction_errors, get_text_channel

log = logging.getLogger(__name__)


class Form(discord.ui.Modal):
    def __init__(self, kind, record=None):
        ticket_type = 'complaint' if kind == 'complaint' else 'question'
        title = 'Небольшая группа · начинаем сейчас' if kind == 'group' else (
            'Жалоба модерации' if ticket_type == 'complaint' else 'Вопрос модерации')
        super().__init__(title=title)
        self.ticket_type = ticket_type
        self.kind, self.pid = 'group' if kind == 'group' else 'ticket', record['id'] if record else None
        self.title_input = discord.ui.TextInput(
            label='Краткое название', max_length=70, default=record['title'] if record else None,
            placeholder='Два танка на вылазку' if kind == 'group' else 'На что или кого жалоба?' if ticket_type == 'complaint' else 'Нужна помощь с доступом')
        self.body = discord.ui.TextInput(
            label='Что планируем?' if kind == 'group' else 'Что произошло? Добавьте ссылки' if ticket_type == 'complaint' else 'Опишите ваш вопрос',
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
                                             self.title_input.value, self.body.value, capacity, staff, str(interaction.id), self.ticket_type)
            await reply(interaction, 'Сохранено. Обновляю чат и карточку; при сбое бот повторит попытку автоматически.')
            await cog._post(pid)
        row = await cog.bot.db.read(data.get, pid)
        if row['channel_id'] and row['message_id']:
            await reply(interaction, f"Готово: https://discord.com/channels/{guild}/{row['channel_id']}/{row['message_id']}")


class Button(discord.ui.DynamicItem[discord.ui.Button],
             template=r'wb:community:(?P<id>\d+):(?P<action>ticket|question|complaint|group|join|leave|edit|close)'):
    def __init__(self, pid, action):
        labels = {'ticket': 'Вопрос', 'question': 'Вопрос', 'complaint': 'Жалоба', 'group': 'Собрать небольшую группу',
                  'join': 'Присоединиться', 'leave': 'Выйти', 'edit': 'Изменить', 'close': 'Завершить / закрыть'}
        super().__init__(discord.ui.Button(
            label=labels[action], custom_id=f'wb:community:{pid}:{action}',
            style=discord.ButtonStyle.primary if action in ('group', 'ticket', 'question', 'complaint', 'join') else discord.ButtonStyle.secondary))
        self.pid, self.action = pid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['id']), match['action'])

    @interaction_errors
    async def callback(self, interaction):
        cog = interaction.client.get_cog('CommunityCog')
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise OperationError('Откройте кнопку на сервере.')
        if self.action in ('ticket', 'question', 'complaint', 'group', 'edit'):
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
        self.retry_after = {}


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
                manage_messages=True, read_message_history=True, connect=True),
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

    def check_channel_permissions(self, guild, overwrites):
        """Discord rejects creation if the bot tries to grant permissions it does not hold."""
        actual = guild.me.guild_permissions
        if actual.administrator:
            return
        needed = discord.Permissions(manage_channels=True, manage_roles=True)
        for overwrite in overwrites.values():
            allow, _ = overwrite.pair()
            needed |= allow
        missing = [name for name, enabled in needed if enabled and not getattr(actual, name)]
        if missing:
            raise OperationError(
                f'Боту на сервере {guild.id} не хватает прав: {", ".join(missing)}. '
                'Обновите права роли бота через ссылку python -m wardenbot.invite APPLICATION_ID, '
                'затем выполните /setup run. Ваши личные права администратора не заменяют права бота.')

    async def category(self, guild, key, name):
        stored = self.bot.cfg(guild.id).get('cat.' + key)
        found = guild.get_channel(int(stored)) if stored and stored.isdigit() else None
        if found is None:
            found = discord.utils.get(guild.categories, name=name)
        if found is None:
            overwrites = self.permissions(guild, [])
            self.check_channel_permissions(guild, overwrites)
            found = await guild.create_category(name, overwrites=overwrites)
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
        is_text = role == 'тикет'
        if is_text:
            name = re.sub(r'[^\w-]+', '-', name.lower()).strip('-')
        marker = f'wardenbot:{source}:{pid}:{role}'
        if channel is None:
            channel = next((c for c in category.channels if getattr(c, 'topic', None) == marker or
                            c.name == name or (not is_text and c.name.endswith(f' {role} · {pid}'))), None)
        if source == 'operation' and role == 'подготовка' and isinstance(channel, discord.TextChannel):
            # Discord cannot convert a text channel to voice. Delete only the tracked/marked old room.
            await self.delete_room(guild, source, pid, channel)
            channel = None
            stored = None
        perms = self.permissions(guild, users, closed)
        if channel is None:
            if closed:
                return None
            self.check_channel_permissions(guild, perms)
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
                    if not channel.permissions_for(guild.me).move_members:
                        raise OperationError(
                            'Доступ к войсу закрыт, но для отключения уже подключённого игрока '
                            'боту нужно право «Перемещать участников» (move_members).')
                    await member.move_to(None, reason='Доступ к группе или операции отозван')
        if not stored or stored[0] != channel.id:
            await self.bot.db.write(lambda conn: conn.execute(
                'INSERT INTO activity_rooms VALUES (?,?,?,?,?) ON CONFLICT(guild_id,source,source_id,role) DO UPDATE SET channel_id=excluded.channel_id',
                (guild.id, source, pid, role, channel.id)))
        return channel

    async def delete_room(self, guild, source, pid, channel):
        try:
            await channel.delete(reason='Операция завершена или комната подготовки заменяется голосовой')
        except discord.NotFound:
            pass
        await self.bot.db.write(lambda conn: conn.execute(
            'DELETE FROM activity_rooms WHERE guild_id=? AND source=? AND source_id=? AND channel_id=?',
            (guild.id, source, pid, channel.id)))

    async def delete_rooms(self, guild, source, pid):
        """Terminal state is the durable deletion job, including lost creation/deletion receipts."""
        rows = await self.bot.db.read(lambda conn: conn.execute(
            'SELECT * FROM activity_rooms WHERE guild_id=? AND source=? AND source_id=?',
            (guild.id, source, pid)).fetchall())
        channels = {}
        failures = []
        for row in rows:
            channel = guild.get_channel(row['channel_id'])
            if channel is None:
                try:
                    channel = await guild.fetch_channel(row['channel_id'])
                except discord.NotFound:
                    await self.bot.db.write(lambda conn, cid=row['channel_id']: conn.execute(
                        'DELETE FROM activity_rooms WHERE guild_id=? AND source=? AND source_id=? AND channel_id=?',
                        (guild.id, source, pid, cid)))
                    continue
                except discord.HTTPException as error:
                    failures.append(error)
                    continue
            channels[channel.id] = channel
        key = 'operation_rooms' if source == 'operation' else 'small_groups'
        stored = self.bot.cfg(guild.id).get('cat.' + key)
        category = guild.get_channel(int(stored)) if stored and stored.isdigit() else None
        if category is None:
            name = 'Каналы операций' if source == 'operation' else 'Небольшие группы'
            category = discord.utils.get(guild.categories, name=name)
        if isinstance(category, discord.CategoryChannel):
            roles = ('подготовка', 'Войс 1', 'Войс 2') if source == 'operation' else ('Сбор',)
            for channel in category.channels:
                if any(getattr(channel, 'topic', None) == f'wardenbot:{source}:{pid}:{role}' or
                       (isinstance(channel, discord.VoiceChannel) and channel.name.endswith(f' {role} · {pid}'))
                       for role in roles):
                    channels[channel.id] = channel
        for channel in channels.values():
            try:
                await self.delete_room(guild, source, pid, channel)
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]

    async def delete_group_message(self, guild, row):
        if row['message_deleted']:
            return
        channel = None
        if row['channel_id']:
            channel = guild.get_channel(row['channel_id'])
            if channel is None:
                try:
                    channel = await guild.fetch_channel(row['channel_id'])
                except discord.NotFound:
                    await self.bot.db.write(data.message_deleted, row['id'])
                    return
        else:
            channel = get_text_channel(self.bot, guild, 'lfg')
        if channel is None:
            raise OperationError('Не найден канал объявления группы. Восстановите настройку через /setup run.')
        marker = f"wb:community:{row['id']}"
        def matches(message):
            return message.author.id == self.bot.user.id and any(embed.footer.text == marker for embed in message.embeds)
        messages = []
        if row['message_id']:
            try:
                message = await channel.fetch_message(row['message_id'])
            except discord.NotFound:
                pass
            else:
                if not matches(message):
                    raise OperationError('Сохранённое сообщение не является объявлением этой группы; удаление остановлено.')
                messages.append(message)
        else:
            # A send may have reached Discord without its message ID reaching SQLite.
            key = row['request_key']
            after = discord.Object(id=int(key)) if key and key.isdigit() else None
            async for message in channel.history(limit=None, after=after):
                if matches(message):
                    messages.append(message)
        for message in messages:
            try:
                await message.delete()
            except discord.NotFound:
                pass
        await self.bot.db.write(data.message_deleted, row['id'])

    async def panels(self, guild):
        async with self.lock:
            support = get_text_channel(self.bot, guild, 'support')
            if support is None:
                support = next((ch for ch in guild.text_channels if ch.name in ('вопросы-и-жалобы', 'помощь-и-отзывы')), None)
            overwrites = self.permissions(guild, [])
            for key in ('recruit', 'private', 'sergeant'):
                rid = self.bot.cfg(guild.id).get('role.' + key)
                role = guild.get_role(int(rid)) if rid and rid.isdigit() else None
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
            if support is None:
                self.check_channel_permissions(guild, overwrites)
                support = await guild.create_text_channel('вопросы-и-жалобы', overwrites=overwrites)
            else:
                await support.edit(name='вопросы-и-жалобы', overwrites=overwrites)
            await self.bot.set_cfg(guild.id, 'ch.support', support.id)
            setup = self.bot.get_cog('SetupCog')
            panels = [
                ('support', support, ['question', 'complaint'], 'Выберите «Вопрос» или «Жалоба»: откроется приватный чат с модерацией. Идеи публикуйте в форуме «предложения» в категории «ШТАБ».'),
                ('lfg', get_text_channel(self.bot, guild, 'lfg'), ['group'],
                 'Соберите небольшую группу прямо сейчас: короткая форма, запись и отдельный войс. Вы уже включены в число участников.'),
            ]
            for key, channel, action, description in panels:
                if isinstance(channel, discord.TextChannel):
                    await setup._upsert(guild, key, channel, embeds=[discord.Embed(description=description, color=0x2F6FB5)], view=view(0, action))

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
        if closed and row['kind'] == 'group':
            failures = []
            # A failure deleting voice must not leave a finished group's advert on the board.
            for cleanup in (lambda: self.delete_group_message(guild, row),
                            lambda: self.delete_rooms(guild, 'group', pid)):
                try:
                    await cleanup()
                except Exception as error:
                    failures.append(error)
            self.synced.pop(pid, None)
            if failures:
                raise failures[0]
            return
        users = [row['owner_id']] if row['kind'] == 'ticket' else await self.bot.db.read(data.members, pid)
        if row['kind'] == 'ticket':
            category = await self.category(guild, 'tickets', 'Обращения к модерации')
            channel = await self.room(guild, 'ticket', pid, 'тикет', row['title'],
                                      await self.admitted_users(guild, users), category, closed)
            if channel is None:
                return
            topic = 'Жалоба' if row['ticket_type'] == 'complaint' else 'Вопрос'
            description = f"**{topic}**\n{row['body']}\nАвтор: <@{row['owner_id']}>"
        else:
            category = await self.category(guild, 'small_groups', 'Небольшие группы')
            allowed = await self.admitted_users(guild, users)
            voice = await self.room(guild, 'group', pid, 'Сбор', row['title'], allowed, category)
            channel = get_text_channel(self.bot, guild, 'lfg')
            if not channel:
                raise OperationError('Не найден канал поиска группы. Администратору нужно выполнить /setup run.')
            if not voice:
                return
            voice_text = voice.mention
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
        if closed:
            await self.delete_rooms(guild, 'operation', op_id)
            return
        users = [op['organizer_id']]
        if not closed:
            users += [s['user_id'] for s in await self.bot.db.read(repo.op_signups, op_id)]
        users = await self.admitted_users(guild, users)
        category = await self.category(guild, 'operation_rooms', 'Каналы операций')
        for role in ('подготовка', 'Войс 1', 'Войс 2'):
            await self.room(guild, 'operation', op_id, role, op['title'], users, category, closed)

    async def retry_job(self, kind, row, action):
        """Back off individual denied jobs; other guilds and operations keep working."""
        key = (kind, row['id'])
        guild = self.bot.get_guild(row['guild_id'])
        if guild is None or guild.me is None:
            return
        permissions = guild.me.guild_permissions.value
        previous = self.retry_after.get(key)
        if previous and previous[0] > time.monotonic() and previous[1] == permissions:
            return
        try:
            await action(row['id'])
        except (discord.Forbidden, OperationError) as error:
            self.retry_after[key] = (time.monotonic() + 300, permissions)
            if isinstance(error, discord.Forbidden):
                log.warning(
                    'Server %s: %s %s denied by Discord (403, code %s). '
                    'Check bot role permissions and category/channel overrides; '
                    'update the invite with python -m wardenbot.invite APPLICATION_ID. Retry in 5 minutes.',
                    guild.id, kind, row['id'], error.code)
            else:
                log.warning('Server %s: %s %s: %s Retry in 5 minutes.', guild.id, kind, row['id'], error)
        except Exception:
            log.exception('%s %s awaits retry', kind, row['id'])
        else:
            self.retry_after.pop(key, None)

    @tasks.loop(seconds=30)
    async def reconcile(self):
        try:
            async with self.lock:
                rows = await self.bot.db.read(lambda conn: conn.execute('SELECT id,guild_id FROM community_posts').fetchall())
                for row in rows:
                    await self.retry_job('Community post', row, self._post)
                operations = await self.bot.db.read(lambda conn: conn.execute(
                    'SELECT id,guild_id FROM operations WHERE thread_id IS NOT NULL').fetchall())
                for op in operations:
                    await self.retry_job('Operation rooms', op, self._operation_rooms)
        except Exception:
            log.exception('Community reconciliation awaits retry')

    @reconcile.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CommunityCog(bot))
