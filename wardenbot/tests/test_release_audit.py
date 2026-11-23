import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from test_community import discord_fixture, NOW
from test_release_rooms import operation
from wardenbot import repo, community as data
from wardenbot.cogs.community import CommunityCog
from wardenbot.cogs.housekeeping import HousekeepingCog
from wardenbot.cogs.setup_cog import SetupCog
from wardenbot.db import Database
from wardenbot.slots import parse_slots
from wardenbot.timeutil import ParseError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import backup as backup_tool


def test_backups_same_instant_do_not_overwrite_and_support_uri_characters(tmp_path, monkeypatch):
    class FixedClock:
        @staticmethod
        def now(zone):
            return datetime(2026, 11, 22, 12, tzinfo=UTC)
    monkeypatch.setattr(backup_tool, 'datetime', FixedClock)
    source = tmp_path / 'данные #1?.db'
    db = Database(str(source))
    copies = []
    for number in range(3):
        repo.cfg_set(db.conn, 1, 'test', number)
        copies.append(backup_tool.backup(source, tmp_path / 'backup', keep=3))
    assert len(set(copies)) == 3
    for number, copy in enumerate(copies):
        with sqlite3.connect(copy) as conn:
            assert conn.execute('SELECT value FROM guild_config').fetchone()[0] == str(number)
            assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert not list((tmp_path / 'backup').glob('*.partial'))
    db.close()


def test_failed_backup_never_prunes_previous_valid_copy(tmp_path):
    db_path = tmp_path / 'database.db'
    Database(str(db_path)).close()
    good = backup_tool.backup(db_path, tmp_path / 'backups', keep=1)
    bad = tmp_path / 'corrupt.db'
    bad.write_text('not sqlite')
    with pytest.raises(sqlite3.DatabaseError):
        backup_tool.backup(bad, tmp_path / 'backups', keep=1)
    assert good.exists()
    assert len(list((tmp_path / 'backups').iterdir())) == 1
    with pytest.raises(ValueError):
        backup_tool.backup(db_path, tmp_path / 'backups', keep=0)


def test_capacity_is_limited_across_roles_and_legacy_last_place_is_atomic():
    async def go():
        assert sum(slot.capacity for slot in parse_slots('Пехота: 90\nТанкисты: 5')) == 95
        with pytest.raises(ParseError, match='too_many_places'):
            parse_slots('Пехота: 90\nТанкисты: 6')
        bot, _, _, _, _ = await discord_fixture()
        oid = await operation(bot)
        slot = (await bot.db.read(repo.op_slots, oid))[0]['id']
        await bot.db.write(lambda conn: conn.execute('UPDATE op_slots SET capacity=200 WHERE id=?', (slot,)))
        for uid in range(1000, 1094):
            await bot.db.write(repo.signup, oid, uid, slot, NOW)
        results = await asyncio.gather(*(bot.db.write(repo.signup, oid, uid, slot, NOW) for uid in (1094, 1095)))
        assert sorted(result.status for result in results) == ['ok', 'operation_full']
        assert len(await bot.db.read(repo.op_signups, oid)) == 95
        alternate = await bot.db.write(lambda conn: conn.execute(
            'INSERT INTO op_slots(op_id,position,name,capacity) VALUES (?,1,?,1)', (oid, 'Медик')).lastrowid)
        assert (await bot.db.write(repo.signup, oid, 1000, alternate, NOW)).status == 'moved'
        bot.db.close()
    asyncio.run(go())


def test_maximum_operation_fits_discord_permission_budget():
    async def go():
        bot, guild, players, _, _ = await discord_fixture()
        staff = guild.get_role(77)
        command = MagicMock(spec=discord.Role)
        command.id = 78
        bot.cfg(1)['role.command'] = '78'
        guild.get_role.side_effect = lambda rid: {77: staff, 78: command}.get(rid)
        for uid in range(100, 195):
            member = MagicMock(spec=discord.Member)
            member.id, member.guild, member.roles = uid, guild, []
            member.guild_permissions = discord.Permissions.none()
            players[uid] = member
        permissions = CommunityCog(bot).permissions(guild, [10, *range(100, 195)])
        assert len(permissions) == 100
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('name', ['role_sync', 'promotions'])
def test_background_role_cycles_survive_transient_database_errors(name):
    async def go():
        bot = SimpleNamespace(db=SimpleNamespace(read=AsyncMock(side_effect=sqlite3.OperationalError('temporary'))), guilds=[])
        cog = HousekeepingCog(bot)
        await getattr(cog, name)()
        bot.db.read.side_effect = None
        bot.db.read.return_value = []
        await getattr(cog, name)()
        assert bot.db.read.await_count >= 2
    asyncio.run(go())


def test_panel_receipt_loss_and_concurrent_setup_do_not_duplicate():
    async def go():
        bot, guild, _, _, channel = await discord_fixture()
        setup = SetupCog(bot)
        sent = []
        async def history(**kwargs):
            for msg in sent:
                yield msg
        async def send(**kwargs):
            msg = SimpleNamespace(id=700, author=bot.user, embeds=kwargs['embeds'], edit=AsyncMock())
            sent.append(msg)
            return msg
        channel.history, channel.send = history, AsyncMock(side_effect=send)
        channel.fetch_message = AsyncMock(side_effect=lambda mid: sent[0])
        persist = bot.set_cfg
        bot.set_cfg = AsyncMock(side_effect=RuntimeError('Receipt lost'))
        embed = discord.Embed(description='Панель')
        with pytest.raises(RuntimeError):
            await setup._upsert(guild, 'support', channel, embeds=[embed])
        bot.set_cfg = persist
        await asyncio.gather(*(setup._upsert(guild, 'support', channel, embeds=[embed]) for _ in range(2)))
        channel.send.assert_awaited_once()
        assert bot.cfg(1)['msg.support'] == '700'
        bot.db.close()
    asyncio.run(go())


def test_ticket_panel_has_two_buttons_and_reuses_channel_id():
    async def go():
        bot, guild, _, channels, _ = await discord_fixture()
        support = await guild.create_text_channel('помощь-и-отзывы', category=guild.categories[0], topic='')
        bot.cfg(1)['ch.support'] = str(support.id)
        setup = SimpleNamespace(_upsert=AsyncMock())
        bot.get_cog = lambda name: setup
        cog = CommunityCog(bot)
        await cog.panels(guild)
        await cog.panels(guild)
        assert bot.cfg(1)['ch.support'] == str(support.id)
        assert support.name == 'вопросы-и-жалобы'
        calls = [call for call in setup._upsert.call_args_list if call.args[1] == 'support']
        assert [item.item.label for item in calls[-1].kwargs['view'].children] == ['Вопрос', 'Жалоба']
        assert calls[-1].kwargs['view'].is_persistent()
        guild.create_text_channel.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_v8_ticket_and_room_records_survive_upgrade(tmp_path, monkeypatch):
    import wardenbot.db as storage
    async def go():
        migrations = storage.MIGRATIONS
        path = tmp_path / 'v8.db'
        monkeypatch.setattr(storage, 'MIGRATIONS', migrations[:8])
        old = Database(str(path))
        old.conn.execute("INSERT INTO community_posts(id,guild_id,owner_id,kind,title,body,channel_id,message_id) VALUES (1,1,10,'ticket','Старый','Текст',501,502)")
        old.conn.execute("INSERT INTO activity_rooms VALUES (1,'ticket',1,'тикет',501)")
        old.close()
        monkeypatch.setattr(storage, 'MIGRATIONS', migrations)
        updated = Database(str(path))
        row = await updated.read(data.get, 1)
        assert row['ticket_type'] == 'question'
        assert row['channel_id'] == 501 and row['message_id'] == 502
        assert updated.conn.execute('SELECT channel_id FROM activity_rooms').fetchone()[0] == 501
        updated.close()
    asyncio.run(go())


@pytest.mark.parametrize('command', ['admit', 'rank', 'info', 'tz_set', 'tz_show'])
def test_member_commands_acknowledge_before_database(monkeypatch, command):
    from discord import app_commands
    from test_discord_layer import make_bot, fake_member, fake_interaction
    import wardenbot.cogs.members as members
    async def go():
        bot = make_bot()
        actor, player = fake_member(99), fake_member(10)
        actor.guild_permissions.administrator = True
        await bot.db.write(repo.user_update, 10, NOW, status='admitted', rank='recruit')
        interaction = fake_interaction(actor, bot=bot)
        read, write = bot.db.read, bot.db.write
        async def checked_read(*args, **kwargs):
            assert interaction.response.is_done()
            return await read(*args, **kwargs)
        async def checked_write(*args, **kwargs):
            assert interaction.response.is_done()
            return await write(*args, **kwargs)
        bot.db.read, bot.db.write = checked_read, checked_write
        monkeypatch.setattr(members, 'sync_member_roles', AsyncMock())
        monkeypatch.setattr(members, 'audit', AsyncMock())
        arguments = {'admit': (player,), 'rank': (player, app_commands.Choice(name='Verified', value='recruit')),
                     'info': (player,), 'tz_set': ('МСК',), 'tz_show': ()}[command]
        cog = members.MembersCog(bot)
        await getattr(cog, command).callback(cog, interaction, *arguments)
        interaction.response.defer.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())
