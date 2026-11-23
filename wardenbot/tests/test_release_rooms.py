"""Release regressions: room deletion, migration, ticket categories and suggestions."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from test_community import discord_fixture, NOW
from wardenbot import community as data, repo
from wardenbot.cogs.community import CommunityCog, Button, Form
from wardenbot.cogs.setup_cog import SetupCog
from wardenbot.db import Database
from wardenbot.layout import CHANNEL_BY_KEY, CATEGORIES
from wardenbot.operation_service import OperationError
from wardenbot.perm_layout import channel_overwrites
from wardenbot.slots import SlotSpec


async def operation(bot):
    oid = await bot.db.write(lambda conn: repo.op_create(conn, guild_id=1, organizer_id=10,
        title='Клановая операция', region='', type='defense', language='ru', start_at=NOW,
        duration_min=60, requirements='', briefing_public='', briefing_closed='Не публиковать',
        slots=[SlotSpec('Пехота', 5)], supply=[], now=NOW))
    await bot.db.write(repo.op_update, oid, thread_id=500)
    return oid


async def room_rows(bot, oid):
    return await bot.db.read(lambda conn: conn.execute(
        "SELECT * FROM activity_rooms WHERE source='operation' AND source_id=? ORDER BY role", (oid,)).fetchall())


@pytest.mark.parametrize('status', ['done', 'cancelled'])
def test_operation_deletes_all_three_voices_and_never_recreates(tmp_path, status):
    async def go():
        path = tmp_path / 'rooms.db'
        bot, guild, _, channels, _ = await discord_fixture(path)
        oid = await operation(bot)
        cog = CommunityCog(bot)
        await cog.sync_operation(oid)
        rooms = [channels[row['channel_id']] for row in await room_rows(bot, oid)]
        assert len(rooms) == 3 and all(isinstance(ch, discord.VoiceChannel) for ch in rooms)
        category = guild.get_channel(int(bot.cfg(1)['cat.operation_rooms']))
        unrelated = await guild.create_voice_channel('Чужой войс', category=category)
        before = guild.create_voice_channel.await_count
        await bot.db.write(repo.op_update, oid, status=status)
        await cog.sync_operation(oid)
        assert not await room_rows(bot, oid)
        assert all(ch.id not in channels for ch in rooms)
        assert unrelated.id in channels
        bot.db.close()
        bot.db = Database(str(path))
        await CommunityCog(bot).sync_operation(oid)
        assert guild.create_voice_channel.await_count == before
        assert all(ch.delete.await_count == 1 for ch in rooms)
        bot.db.close()
    asyncio.run(go())


def test_partial_delete_failure_keeps_job_and_recovers_after_restart(tmp_path):
    async def go():
        path = tmp_path / 'retry.db'
        bot, guild, _, channels, _ = await discord_fixture(path)
        oid = await operation(bot)
        cog = CommunityCog(bot)
        await cog.sync_operation(oid)
        rooms = await room_rows(bot, oid)
        broken = channels[rooms[0]['channel_id']]
        delete = broken.delete.side_effect
        broken.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Permissions')
        await bot.db.write(repo.op_update, oid, status='done')
        with pytest.raises(discord.Forbidden):
            await cog.sync_operation(oid)
        assert [row['channel_id'] for row in await room_rows(bot, oid)] == [broken.id]
        assert all(row['channel_id'] not in channels for row in rooms[1:])
        bot.db.close()
        bot.db = Database(str(path))
        broken.delete.side_effect = delete
        await CommunityCog(bot).sync_operation(oid)
        assert not await room_rows(bot, oid)
        assert guild.create_voice_channel.await_count == 3
        bot.db.close()
    asyncio.run(go())


def test_lost_delete_receipt_is_recovered_with_not_found(tmp_path):
    async def go():
        bot, _, _, channels, _ = await discord_fixture(tmp_path / 'lost.db')
        oid = await operation(bot)
        cog = CommunityCog(bot)
        await cog.sync_operation(oid)
        first = channels[(await room_rows(bot, oid))[0]['channel_id']]
        delete = first.delete.side_effect
        async def lost_receipt(**kwargs):
            await delete(**kwargs)
            raise RuntimeError('Connection lost after deletion')
        first.delete.side_effect = lost_receipt
        await bot.db.write(repo.op_update, oid, status='cancelled')
        with pytest.raises(RuntimeError):
            await cog.sync_operation(oid)
        assert first.id not in channels and len(await room_rows(bot, oid)) == 1
        await CommunityCog(bot).sync_operation(oid)
        assert not await room_rows(bot, oid)
        bot.db.close()
    asyncio.run(go())


def test_old_text_prep_becomes_voice_and_failed_creation_is_retryable():
    async def go():
        bot, guild, _, channels, _ = await discord_fixture()
        oid = await operation(bot)
        cog = CommunityCog(bot)
        category = await cog.category(guild, 'operation_rooms', 'Каналы операций')
        old = await guild.create_text_channel('старая-подготовка', category=category,
                                              topic=f'wardenbot:operation:{oid}:подготовка')
        await bot.db.write(lambda conn: conn.execute('INSERT INTO activity_rooms VALUES (?,?,?,?,?)',
                                                     (1, 'operation', oid, 'подготовка', old.id)))
        create = guild.create_voice_channel.side_effect
        guild.create_voice_channel.side_effect = RuntimeError('Network')
        with pytest.raises(RuntimeError):
            await cog.sync_operation(oid)
        old.delete.assert_awaited_once()
        assert old.id not in channels
        guild.create_voice_channel.side_effect = create
        await CommunityCog(bot).sync_operation(oid)
        rooms = await room_rows(bot, oid)
        assert len(rooms) == 3
        assert all(isinstance(channels[row['channel_id']], discord.VoiceChannel) for row in rooms)
        bot.db.close()
    asyncio.run(go())


def test_closed_operation_removes_unrecorded_room_but_preserves_other_ids():
    async def go():
        bot, guild, _, channels, _ = await discord_fixture()
        oid = await operation(bot)
        cog = CommunityCog(bot)
        category = await cog.category(guild, 'operation_rooms', 'Каналы операций')
        orphan = await guild.create_voice_channel(f'Другая надпись Войс 1 · {oid}', category=category)
        other = await guild.create_voice_channel(f'Другая надпись Войс 1 · {oid}1', category=category)
        old_text = await guild.create_text_channel('старый-чат', category=category,
                                                   topic=f'wardenbot:operation:{oid}:подготовка')
        await bot.db.write(repo.op_update, oid, status='done')
        await cog.sync_operation(oid)
        assert orphan.id not in channels and old_text.id not in channels
        assert other.id in channels
        bot.db.close()
    asyncio.run(go())


def test_group_close_recovers_after_delete_failure_and_removes_voice_link(tmp_path):
    async def go():
        path = tmp_path / 'group.db'
        bot, guild, _, channels, lfg = await discord_fixture(path)
        pid = await bot.db.write(data.create, 1, 10, 'group', 'Дуо', 'Строим', 2)
        cog = CommunityCog(bot)
        await cog.post(pid)
        voice = next(ch for ch in channels.values() if isinstance(ch, discord.VoiceChannel))
        delete = voice.delete.side_effect
        voice.delete.side_effect = RuntimeError('Network')
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        with pytest.raises(RuntimeError):
            await cog.post(pid)
        assert (await bot.db.read(data.get, pid))['state'] == 'closed'
        bot.db.close()
        bot.db = Database(str(path))
        voice.delete.side_effect = delete
        cog = CommunityCog(bot)
        await cog.post(pid)
        await cog.post(pid)
        assert voice.id not in channels
        assert guild.create_voice_channel.await_count == 1
        msg = next(iter(lfg.messages.values()))
        assert voice.mention not in msg.embeds[0].description and msg.controls is None
        bot.db.close()
    asyncio.run(go())


def test_ticket_types_persist_and_old_button_still_opens_question(tmp_path):
    async def go():
        path = tmp_path / 'tickets.db'
        bot, guild, players, channels, lfg = await discord_fixture(path)
        cog = CommunityCog(bot)
        bot.get_cog = lambda name: cog
        for action, uid, title in [('question', 10, 'Вопрос'), ('complaint', 20, 'Жалоба')]:
            item = Button(0, action)
            restored = await Button.from_custom_id(None, item.item, re.fullmatch(r'wb:community:(?P<id>\d+):(?P<action>\w+)', item.item.custom_id))
            inter = SimpleNamespace(client=bot, guild=guild, user=players[uid], id=9000 + uid,
                response=SimpleNamespace(send_modal=AsyncMock(), defer=AsyncMock(), is_done=lambda: True),
                followup=SimpleNamespace(send=AsyncMock()))
            await restored.callback(inter)
            form = inter.response.send_modal.call_args.args[0]
            assert len(form.children) == 2
            form.title_input._value, form.body._value = title, 'Приватное сообщение'
            await form.on_submit(inter)
        bot.db.close()
        bot.db = Database(str(path))
        rows = await bot.db.read(lambda conn: conn.execute('SELECT * FROM community_posts ORDER BY id').fetchall())
        assert [row['ticket_type'] for row in rows] == ['question', 'complaint']
        for row in rows:
            channel = channels[row['channel_id']]
            assert channel.overwrites[guild.default_role].view_channel is False
            assert '**' + row['title'] + '**' in next(iter(channel.messages.values())).embeds[0].description
        assert not lfg.messages
        with pytest.raises(OperationError):
            await bot.db.write(data.create, 1, 10, 'ticket', 'Жалоба', 'Текст', ticket_type='complaint')
        assert Form('ticket').ticket_type == 'question'
        bot.db.close()
    asyncio.run(go())


def test_feedback_forum_is_moved_without_replacing_posts_and_access_is_verified():
    async def go():
        bot, guild, _, _, _ = await discord_fixture()
        bot.config = SimpleNamespace(channel_name_style='ru')
        category = MagicMock(spec=discord.CategoryChannel)
        forum = MagicMock(spec=discord.ForumChannel)
        forum.id = 88
        forum.edit = AsyncMock()
        bot.cfg(1)['ch.feedback'] = '88'
        guild.get_channel.side_effect = lambda cid: forum if cid == 88 else None
        setup = SetupCog(bot)
        ch = CHANNEL_BY_KEY['feedback']
        role = MagicMock(spec=discord.Role)
        result = await setup._ensure_channel(guild, category, ch, {'recruit': role}, False, [])
        assert result is forum
        changes = forum.edit.call_args.kwargs
        assert changes['category'] is category
        assert changes['overwrites'][role].send_messages is True
        assert changes['overwrites'][guild.default_role].view_channel is False
        assert ch.launch and ch.kind == 'forum'
        assert next(cat.key for cat in CATEGORIES if ch in cat.channels) == 'hq'
        guild.create_text_channel.assert_not_awaited()
        assert channel_overwrites(ch)['recruit'].send_messages is True
        bot.db.close()
    asyncio.run(go())
