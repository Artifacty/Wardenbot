"""Regression coverage for the v0.3 Missing Permissions startup reports."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from test_community import discord_fixture, NOW
from wardenbot import community as data, repo
from wardenbot.cogs.community import CommunityCog
from wardenbot.invite import required_permissions
from wardenbot.operation_service import OperationError
from wardenbot.slots import SlotSpec


def test_invite_covers_all_private_room_overwrites_without_admin():
    async def go():
        bot, guild, _, _, _ = await discord_fixture()
        cog = CommunityCog(bot)
        permissions = required_permissions()
        assert not permissions.administrator
        assert permissions.connect and permissions.speak and permissions.move_members
        for closed in (False, True):
            for overwrite in cog.permissions(guild, [10, 20, 99], closed).values():
                allowed, _ = overwrite.pair()
                assert allowed.value & permissions.value == allowed.value
        bot.db.close()
    asyncio.run(go())


def test_missing_permissions_are_explained_before_http_request():
    async def go():
        bot, guild, _, _, _ = await discord_fixture()
        cog = CommunityCog(bot)
        guild.me.guild_permissions.connect = False
        with pytest.raises(OperationError, match='connect'):
            await cog.category(guild, 'operation_rooms', 'Каналы операций')
        guild.create_category.assert_not_awaited()
        assert 'cat.operation_rooms' not in bot.cfg(1)
        guild.me.guild_permissions.connect = True
        # Move Members is for disconnecting users, not for creating an empty category.
        guild.me.guild_permissions.move_members = False
        category = await cog.category(guild, 'operation_rooms', 'Каналы операций')
        assert category.overwrites[guild.me].move_members is None
        guild.create_category.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('status', ['done', 'cancelled'])
def test_legacy_closed_operation_creates_no_category(status):
    async def go():
        bot, guild, _, _, _ = await discord_fixture()
        oid = await bot.db.write(lambda conn: repo.op_create(conn, guild_id=1, organizer_id=10,
            title='Старая операция', region='', type='defense', language='ru', start_at=NOW,
            duration_min=60, requirements='', briefing_public='', briefing_closed='',
            slots=[SlotSpec('Пехота', 5)], supply=[], now=NOW))
        await bot.db.write(repo.op_update, oid, status=status, thread_id=42)
        guild.me.guild_permissions = discord.Permissions.none()
        await CommunityCog(bot).sync_operation(oid)
        guild.create_category.assert_not_awaited()
        guild.create_text_channel.assert_not_awaited()
        guild.create_voice_channel.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_forbidden_backoff_does_not_stop_other_jobs_and_recovers_on_role_change(caplog):
    async def go():
        bot, guild, _, _, _ = await discord_fixture()
        cog = CommunityCog(bot)
        response = SimpleNamespace(status=403, reason='Forbidden')
        action = AsyncMock(side_effect=discord.Forbidden(response, {'code': 50013, 'message': 'Missing Permissions'}))
        row = {'id': 1, 'guild_id': 1}
        await cog.retry_job('Operation rooms', row, action)
        await cog.retry_job('Operation rooms', row, action)
        action.assert_awaited_once_with(1)
        assert '50013' in caplog.text and 'Retry in 5 minutes' in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        other = AsyncMock()
        await cog.retry_job('Operation rooms', {'id': 2, 'guild_id': 1}, other)
        other.assert_awaited_once_with(2)
        # A changed bot role bypasses the delay on the next reconciliation tick.
        guild.me.guild_permissions.move_members = False
        action.side_effect = None
        await cog.retry_job('Operation rooms', row, action)
        assert action.await_count == 2
        assert not cog.retry_after
        bot.db.close()
    asyncio.run(go())


def test_retry_after_channel_permissions_fixed_without_role_change():
    async def go():
        bot, _, _, _, _ = await discord_fixture()
        cog = CommunityCog(bot)
        action = AsyncMock(side_effect=OperationError('Недостаточно прав'))
        row = {'id': 1, 'guild_id': 1}
        await cog.retry_job('Community post', row, action)
        await cog.retry_job('Community post', row, action)
        assert action.await_count == 1
        key = ('Community post', 1)
        cog.retry_after[key] = (0, cog.retry_after[key][1])
        action.side_effect = None
        await cog.retry_job('Community post', row, action)
        assert action.await_count == 2 and not cog.retry_after
        bot.db.close()
    asyncio.run(go())


def test_missing_move_permission_still_revokes_channel_access():
    async def go():
        bot, guild, players, channels, _ = await discord_fixture()
        cog = CommunityCog(bot)
        pid = await bot.db.write(data.create, 1, 10, 'group', 'Танки', 'Выезжаем', 3)
        await bot.db.write(data.act, pid, 1, 20, False, 'join')
        await cog.post(pid)
        voice = next(ch for ch in channels.values() if isinstance(ch, discord.VoiceChannel))
        voice.members = [players[20]]
        guild.me.guild_permissions.move_members = False
        await bot.db.write(data.act, pid, 1, 20, False, 'leave')
        with pytest.raises(OperationError, match='Перемещать участников'):
            await cog.post(pid)
        assert players[20] not in voice.overwrites
        players[20].move_to.assert_not_awaited()
        guild.me.guild_permissions.move_members = True
        await cog.post(pid)
        players[20].move_to.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())
