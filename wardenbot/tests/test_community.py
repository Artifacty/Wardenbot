import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from wardenbot import community as service, repo
from wardenbot.cogs.community import CommunityCog, Form, Button, view
from wardenbot.db import Database
from wardenbot.operation_service import OperationError
from wardenbot.timeutil import parse_local_datetime, resolve_tz, ParseError
from wardenbot.layout import CATEGORIES
from wardenbot.slots import SlotSpec

NOW = datetime(2026, 11, 17, 22, 30, tzinfo=UTC)


async def database(path=':memory:'):
    db = Database(str(path))
    for uid in (10, 20, 30):
        await db.write(repo.user_update, uid, NOW, status='admitted', rank='recruit')
    return db


def test_concurrent_join_capacity_duplicate_leave_and_edit():
    async def go():
        db = await database()
        pid = await db.write(service.create, 1, 10, 'group', 'Танки', 'Выезжаем', 2)
        results = await asyncio.gather(*(db.write(service.act, pid, 1, uid, False, 'join') for uid in (20, 30)), return_exceptions=True)
        assert sum(isinstance(result, OperationError) for result in results) == 1
        joined = next(uid for uid in await db.read(service.members, pid) if uid != 10)
        await db.write(service.act, pid, 1, joined, False, 'join')
        assert len(await db.read(service.members, pid)) == 2
        with pytest.raises(OperationError):
            await db.write(service.edit, pid, 1, 10, False, 'Танки', 'Новый текст', 1)
        with pytest.raises(OperationError):
            await db.write(service.edit, pid, 1, joined, False, 'Чужая группа', 'Текст', 3)
        await db.write(service.edit, pid, 1, 10, False, 'Два танка', 'Едем', 3)
        await db.write(service.act, pid, 1, joined, False, 'leave')
        await db.write(service.act, pid, 1, joined, False, 'leave')
        assert await db.read(service.members, pid) == [10]
        with pytest.raises(OperationError):
            await db.write(service.act, pid, 1, 10, False, 'leave')
        db.close()
    asyncio.run(go())


def test_permissions_revocation_and_closed_posts():
    async def go():
        db = await database()
        with pytest.raises(OperationError):
            await db.write(service.create, 1, 999, 'ticket', 'Помощь', 'Вопрос')
        pid = await db.write(service.create, 1, 10, 'group', 'Группа', 'Описание', 4)
        for uid, guild, action in ((20, 1, 'close'), (10, 2, 'close'), (999, 1, 'join'), (10, 1, 'oops')):
            with pytest.raises(OperationError):
                await db.write(service.act, pid, guild, uid, False, action)
        await db.write(service.act, pid, 1, 20, False, 'join')
        await db.write(repo.user_update, 20, NOW, status='banned')
        with pytest.raises(OperationError):
            await db.write(service.act, pid, 1, 20, False, 'join')
        await db.write(service.reconcile_access, pid, set())
        assert await db.read(service.members, pid) == [10]
        await db.write(repo.user_update, 10, NOW, status='pending')
        await db.write(service.reconcile_access, pid, set())
        assert (await db.read(service.get, pid))['state'] == 'closed'
        with pytest.raises(OperationError):
            await db.write(service.act, pid, 1, 30, False, 'join')
        # An administrator without verification can organize a group, and its status survives reconciliation.
        admin_pid = await db.write(service.create, 1, 99, 'group', 'Общая', 'Описание', 4, True)
        await db.write(service.reconcile_access, admin_pid, {99})
        assert (await db.read(service.get, admin_pid))['state'] == 'open'
        await db.write(service.act, admin_pid, 1, 99, True, 'close')
        await db.write(service.act, admin_pid, 1, 99, True, 'close')
        db.close()
    asyncio.run(go())


def test_ticket_limits_idempotent_submission_and_restart(tmp_path):
    async def go():
        path = tmp_path / 'community.db'
        db = await database(path)
        pid = await db.write(service.create, 1, 10, 'ticket', 'Помощь', 'Закрытый текст', 1, False, 'interaction-1')
        assert await db.write(service.create, 1, 10, 'ticket', 'Помощь', 'Закрытый текст', 1, False, 'interaction-1') == pid
        with pytest.raises(OperationError):
            await db.write(service.create, 1, 10, 'ticket', 'Ещё', 'Текст')
        db.close()
        db = Database(str(path))
        assert (await db.read(service.get, pid))['body'] == 'Закрытый текст'
        with pytest.raises(OperationError):
            await db.write(service.act, pid, 1, 20, False, 'join')
        await db.write(service.act, pid, 1, 99, True, 'close')
        assert await db.write(service.create, 1, 10, 'ticket', 'Другой вопрос', 'Текст') != pid
        db.close()
    asyncio.run(go())


@pytest.mark.parametrize('text,delta', [('через 30 минут', 30), ('через 2 часа', 120), ('через 1 час', 60), ('через 5 ч', 300), ('через 15мин', 15)])
def test_easy_time(text, delta):
    assert parse_local_datetime(text, resolve_tz('МСК')[0], NOW) == NOW + timedelta(minutes=delta)


@pytest.mark.parametrize('text', ['через 0 минут', 'через -1 час', 'через 999 часов', '', None])
def test_bad_relative_time(text):
    with pytest.raises(ParseError):
        parse_local_datetime(text, UTC, NOW)


def test_channel_names_and_simple_persistent_forms():
    async def go():
        assert len(Form('group').children) == 3
        assert len(Form('ticket').children) == 2
        for action in ('ticket', 'group', 'join', 'leave', 'edit', 'close'):
            button = Button(12, action)
            pattern = re.compile(r'wb:community:(?P<id>\d+):(?P<action>\w+)')
            restored = await Button.from_custom_id(None, button.item, pattern.fullmatch(button.item.custom_id))
            assert (restored.pid, restored.action) == (12, action)
        assert view(12, ['join', 'leave', 'edit', 'close']).is_persistent()
        for category in CATEGORIES:
            if category.key != 'entry':
                assert category.en not in category.name
            for channel in category.channels:
                assert channel.name('full') == channel.name('ru') == channel.name('en')
                if channel.access != 'public':
                    assert channel.name().split('┃')[1].startswith(channel.ru)
    asyncio.run(go())


async def discord_fixture(path=':memory:'):
    """In-memory Discord transport; channel creation, messages, and permission edits are observable."""
    db = await database(path)
    guild = MagicMock(spec=discord.Guild)
    guild.id, guild.categories, guild.text_channels = 1, [], []
    guild.default_role = MagicMock(spec=discord.Role)
    staff = MagicMock(spec=discord.Role)
    staff.id = 77
    players = {}
    for uid in (10, 20, 30, 99, 999):
        member = MagicMock(spec=discord.Member)
        member.id, member.guild, member.roles = uid, guild, [staff] if uid == 99 else []
        member.guild_permissions = discord.Permissions.none()
        member.move_to = AsyncMock()
        players[uid] = member
    guild.members = list(players.values())
    guild.me = players[999]
    guild.get_member.side_effect = players.get
    guild.get_role.side_effect = lambda rid: staff if rid == 77 else None
    channels, next_id = {}, 100
    guild.get_channel.side_effect = channels.get
    cfg = {'role.staff': '77'}
    bot = SimpleNamespace(db=db, user=players[999], cfg=lambda gid: cfg, get_guild=lambda gid: guild if gid == 1 else None)
    async def set_cfg(gid, key, value):
        cfg[key] = str(value)
    bot.set_cfg = set_cfg
    async def create(name, **kwargs):
        nonlocal next_id
        next_id += 1
        is_category = 'category' not in kwargs
        cls = discord.CategoryChannel if is_category else discord.TextChannel if 'topic' in kwargs else discord.VoiceChannel
        ch = MagicMock(spec=cls)
        ch.id, ch.name, ch.mention = next_id, name, f'<#{next_id}>'
        ch.overwrites = kwargs.get('overwrites', {})
        ch.topic, ch.members, ch.channels = kwargs.get('topic'), [], []
        async def edit(**changes):
            for key, value in changes.items():
                setattr(ch, key, value)
            return ch
        ch.edit = AsyncMock(side_effect=edit)
        channels[ch.id] = ch
        if is_category:
            guild.categories.append(ch)
        else:
            kwargs['category'].channels.append(ch)
            if cls == discord.TextChannel:
                guild.text_channels.append(ch)
        messages = {}
        async def send(**values):
            msg = SimpleNamespace(id=len(messages) + 500, author=bot.user, embeds=[values['embed']], controls=values['view'])
            async def edit_message(**changes):
                msg.embeds, msg.controls = [changes['embed']], changes['view']
            msg.edit = AsyncMock(side_effect=edit_message)
            messages[msg.id] = msg
            return msg
        async def fetch(mid):
            return messages[mid]
        async def history(limit=None, after=None):
            for message in reversed(list(messages.values())):
                yield message
        ch.send, ch.fetch_message, ch.history = AsyncMock(side_effect=send), AsyncMock(side_effect=fetch), history
        ch.messages = messages
        return ch
    guild.create_category = AsyncMock(side_effect=create)
    guild.create_text_channel = AsyncMock(side_effect=create)
    guild.create_voice_channel = AsyncMock(side_effect=create)
    cat = await create('Сообщество')
    lfg = await create('ищу-группу', category=cat, topic='')
    cfg['ch.lfg'] = str(lfg.id)
    return bot, guild, players, channels, lfg


def test_group_voice_permissions_join_leave_close_and_restart(tmp_path):
    async def go():
        path = tmp_path / 'group.db'
        bot, guild, players, channels, lfg = await discord_fixture(path)
        cog = CommunityCog(bot)
        pid = await bot.db.write(service.create, 1, 10, 'group', 'Танковая вылазка', 'Играем сейчас', 3)
        await cog.post(pid)
        assert guild.create_voice_channel.await_count == 1
        voice = next(ch for ch in channels.values() if isinstance(ch, discord.VoiceChannel))
        assert voice.overwrites[guild.default_role].view_channel is False
        assert players[10] in voice.overwrites and players[20] not in voice.overwrites
        msg = next(iter(lfg.messages.values()))
        assert voice.mention in msg.embeds[0].description
        await bot.db.write(service.act, pid, 1, 20, False, 'join')
        await cog.post(pid)
        assert players[20] in voice.overwrites
        voice.members = [players[20]]
        await bot.db.write(service.act, pid, 1, 20, False, 'leave')
        await cog.post(pid)
        assert players[20] not in voice.overwrites
        players[20].move_to.assert_awaited_once()
        voice.members = []
        await bot.db.write(service.edit, pid, 1, 10, False, 'Строим оборону', 'Новый план', 4)
        await cog.post(pid)
        assert voice.name.startswith('Строим оборону')
        assert len(lfg.messages) == 1
        bot.db.close()
        bot.db = Database(str(path))
        cog = CommunityCog(bot)
        await cog.post(pid)
        assert guild.create_voice_channel.await_count == 1
        assert len(lfg.messages) == 1
        # Lost Discord acknowledgement: recover both identities without creating duplicate rooms or posts.
        await bot.db.write(lambda conn: conn.execute('DELETE FROM activity_rooms'))
        await bot.db.write(lambda conn: conn.execute('UPDATE community_posts SET message_id=NULL'))
        await CommunityCog(bot).post(pid)
        assert guild.create_voice_channel.await_count == 1
        assert (await bot.db.read(service.get, pid))['message_id'] == msg.id
        await bot.db.write(service.act, pid, 1, 10, False, 'close')
        await cog.post(pid)
        assert msg.controls is None
        assert voice.overwrites[players[10]].connect is False
        bot.db.close()
    asyncio.run(go())


def test_ticket_is_private_and_closing_preserves_moderator_access():
    async def go():
        bot, guild, players, channels, lfg = await discord_fixture()
        cog = CommunityCog(bot)
        pid = await bot.db.write(service.create, 1, 99, 'ticket', 'Личный вопрос', 'СЕКРЕТ', 1, True)
        await cog.post(pid)
        record = await bot.db.read(service.get, pid)
        ticket = channels[record['channel_id']]
        assert ticket != lfg and not lfg.messages
        assert ticket.overwrites[guild.default_role].view_channel is False
        assert players[10] not in ticket.overwrites
        assert 'СЕКРЕТ' in next(iter(ticket.messages.values())).embeds[0].description
        await bot.db.write(service.act, pid, 1, 99, True, 'close')
        await cog.post(pid)
        assert ticket.overwrites[players[99]].send_messages is True
        bot.db.close()
    asyncio.run(go())


def test_operation_creates_three_private_rooms_and_recovers_partial_failure():
    async def go():
        bot, guild, players, channels, lfg = await discord_fixture()
        cog = CommunityCog(bot)
        oid = await bot.db.write(lambda conn: repo.op_create(conn, guild_id=1, organizer_id=10,
            title='Оборона', region='Регион', type='defense', language='ru', start_at=NOW + timedelta(hours=1),
            duration_min=60, requirements='', briefing_public='Общее', briefing_closed='СЕКРЕТНЫЙ ПЛАН',
            slots=[SlotSpec('Пехота', 5)], supply=[], now=NOW))
        await bot.db.write(repo.op_update, oid, thread_id=42)
        original = guild.create_voice_channel.side_effect
        guild.create_voice_channel.side_effect = RuntimeError('Network failure')
        with pytest.raises(RuntimeError):
            await cog.sync_operation(oid)
        assert len(await bot.db.read(lambda conn: conn.execute('SELECT * FROM activity_rooms').fetchall())) == 1
        guild.create_voice_channel.side_effect = original
        await cog.sync_operation(oid)
        rooms = await bot.db.read(lambda conn: conn.execute('SELECT * FROM activity_rooms').fetchall())
        assert {r['role'] for r in rooms} == {'подготовка', 'Войс 1', 'Войс 2'}
        await cog.sync_operation(oid)
        assert len([ch for ch in channels.values() if isinstance(ch, discord.VoiceChannel)]) == 2
        for room in rooms:
            ch = channels[room['channel_id']]
            assert players[10] in ch.overwrites and players[20] not in ch.overwrites
            assert 'СЕКРЕТНЫЙ ПЛАН' not in str(ch.topic)
        await bot.db.write(repo.user_update, 10, NOW, status='banned')
        await cog.sync_operation(oid)
        assert all(players[10] not in channels[r['channel_id']].overwrites for r in rooms)
        await bot.db.write(repo.op_update, oid, status='done')
        await cog.sync_operation(oid)
        assert len(await bot.db.read(lambda conn: conn.execute('SELECT * FROM activity_rooms').fetchall())) == 3
        bot.db.close()
    asyncio.run(go())


def test_upgrade_v7_preserves_users_and_operations(tmp_path, monkeypatch):
    import wardenbot.db as storage
    async def go():
        migrations = storage.MIGRATIONS
        path = tmp_path / 'upgrade.db'
        monkeypatch.setattr(storage, 'MIGRATIONS', migrations[:7])
        old = await database(path)
        oid = await old.write(lambda conn: repo.op_create(conn, guild_id=1, organizer_id=10,
            title='Старая операция', region='', type='defense', language='ru', start_at=NOW,
            duration_min=60, requirements='', briefing_public='', briefing_closed='План',
            slots=[SlotSpec('Пехота', 5)], supply=[], now=NOW))
        assert old.conn.execute('PRAGMA user_version').fetchone()[0] == 7
        old.close()
        monkeypatch.setattr(storage, 'MIGRATIONS', migrations)
        updated = Database(str(path))
        assert updated.conn.execute('PRAGMA user_version').fetchone()[0] == 8
        assert (await updated.read(repo.op_get, oid))['briefing_closed'] == 'План'
        assert (await updated.read(repo.user_get, 10))['status'] == 'admitted'
        pid = await updated.write(service.create, 1, 10, 'ticket', 'Вопрос', 'Работает после миграции')
        updated.close()
        reopened = Database(str(path))
        assert (await reopened.read(service.get, pid))['title'] == 'Вопрос'
        reopened.close()
    asyncio.run(go())


def test_revoked_form_submission_and_normal_ticket_closure():
    async def go():
        bot, guild, players, channels, _ = await discord_fixture()
        cog = CommunityCog(bot)
        bot.get_cog = lambda name: cog
        form = Form('group')
        form.title_input._value, form.body._value, form.capacity._value = 'Группа', 'Описание', '4'
        interaction = SimpleNamespace(client=bot, guild=guild, user=players[10], id=5000,
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
            followup=SimpleNamespace(send=AsyncMock()))
        await bot.db.write(repo.user_update, 10, NOW, status='banned')
        await form.on_submit(interaction)
        assert not await bot.db.read(lambda conn: conn.execute('SELECT id FROM community_posts').fetchall())
        assert 'проверку' in interaction.followup.send.call_args.args[0]
        await bot.db.write(repo.user_update, 10, NOW, status='admitted')
        pid = await bot.db.write(service.create, 1, 10, 'ticket', 'Вопрос', 'Текст')
        await cog.post(pid)
        row = await bot.db.read(service.get, pid)
        channel = channels[row['channel_id']]
        assert channel.overwrites[players[10]].send_messages is True
        await bot.db.write(service.act, pid, 1, 10, False, 'close')
        await cog.post(pid)
        assert channel.overwrites[players[10]].view_channel is True
        assert channel.overwrites[players[10]].send_messages is False
        assert channel.overwrites[guild.get_role(77)].send_messages is True
        bot.db.close()
    asyncio.run(go())
