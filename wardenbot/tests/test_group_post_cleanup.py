import asyncio
from types import SimpleNamespace

import discord
import pytest

from test_community import discord_fixture
from wardenbot import community as data
from wardenbot.cogs.community import CommunityCog
from wardenbot.db import Database


async def group(bot, cog):
    pid = await bot.db.write(data.create, 1, 10, 'group', 'Вылазка', 'Два танка', 4)
    await cog.post(pid)
    return pid


def test_close_deletes_only_group_advert_and_restart_never_reposts(tmp_path):
    async def go():
        path = tmp_path / 'cleanup.db'
        bot, guild, _, channels, lfg = await discord_fixture(path)
        cog = CommunityCog(bot)
        pid = await group(bot, cog)
        advert = next(iter(lfg.messages.values()))
        panel = await lfg.send(embed=discord.Embed(description='Панель поиска группы'), view=None)
        other = await bot.db.write(data.create, 1, 20, 'group', 'Другая группа', 'Оборона', 3)
        await cog.post(other)
        other_message = (await bot.db.read(data.get, other))['message_id']
        sent = lfg.send.await_count
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        await cog.post(pid)
        assert advert.id not in lfg.messages
        assert panel.id in lfg.messages and other_message in lfg.messages
        assert (await bot.db.read(data.get, pid))['message_deleted'] == 1
        bot.db.close()
        bot.db = Database(str(path))
        await CommunityCog(bot).post(pid)
        assert lfg.send.await_count == sent
        advert.delete.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_failed_message_delete_keeps_job_but_does_not_block_voice_cleanup(tmp_path):
    async def go():
        path = tmp_path / 'retry.db'
        bot, guild, _, channels, lfg = await discord_fixture(path)
        cog = CommunityCog(bot)
        pid = await group(bot, cog)
        message = next(iter(lfg.messages.values()))
        remove = message.delete.side_effect
        message.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Permissions')
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        with pytest.raises(discord.Forbidden):
            await cog.post(pid)
        assert message.id in lfg.messages
        assert not any(isinstance(ch, discord.VoiceChannel) for ch in channels.values())
        assert (await bot.db.read(data.get, pid))['message_deleted'] == 0
        bot.db.close()
        bot.db = Database(str(path))
        message.delete.side_effect = remove
        await CommunityCog(bot).post(pid)
        assert not lfg.messages
        assert lfg.send.await_count == 1
        bot.db.close()
    asyncio.run(go())


def test_receipt_loss_after_delete_recovers_404_without_reposting(tmp_path):
    async def go():
        path = tmp_path / 'receipt.db'
        bot, _, _, _, lfg = await discord_fixture(path)
        cog = CommunityCog(bot)
        pid = await group(bot, cog)
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        write = bot.db.write
        async def fail_receipt(fn, *args, **kwargs):
            if fn is data.message_deleted:
                raise RuntimeError('Process failed after Discord deletion')
            return await write(fn, *args, **kwargs)
        bot.db.write = fail_receipt
        with pytest.raises(RuntimeError):
            await cog.post(pid)
        assert not lfg.messages
        bot.db.close()
        bot.db = Database(str(path))
        await CommunityCog(bot).post(pid)
        assert (await bot.db.read(data.get, pid))['message_deleted'] == 1
        lfg.send.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_lost_publication_receipt_finds_own_marker_and_preserves_user_message():
    async def go():
        bot, _, _, _, lfg = await discord_fixture()
        cog = CommunityCog(bot)
        pid = await group(bot, cog)
        advert = next(iter(lfg.messages.values()))
        forged = await lfg.send(embed=advert.embeds[0].copy(), view=None)
        forged.author = SimpleNamespace(id=12345)
        await bot.db.write(lambda conn: conn.execute('UPDATE community_posts SET channel_id=NULL,message_id=NULL WHERE id=?', (pid,)))
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        await cog.post(pid)
        assert advert.id not in lfg.messages and forged.id in lfg.messages
        forged.delete.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_cleanup_uses_original_channel_after_lfg_setting_changes():
    async def go():
        bot, guild, _, _, original = await discord_fixture()
        cog = CommunityCog(bot)
        pid = await group(bot, cog)
        replacement = await guild.create_text_channel('новый-поиск', category=guild.categories[0], topic='')
        bot.cfg(1)['ch.lfg'] = str(replacement.id)
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        await cog.post(pid)
        assert not original.messages
        replacement.send.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_close_unpublished_group_never_creates_advert_or_voice():
    async def go():
        bot, guild, _, _, lfg = await discord_fixture()
        pid = await bot.db.write(data.create, 1, 10, 'group', 'Группа', 'План', 4)
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        await CommunityCog(bot).post(pid)
        lfg.send.assert_not_awaited()
        guild.create_voice_channel.assert_not_awaited()
        assert (await bot.db.read(data.get, pid))['message_deleted'] == 1
        bot.db.close()
    asyncio.run(go())


def test_already_deleted_channel_completes_cleanup():
    async def go():
        bot, guild, _, _, lfg = await discord_fixture()
        cog = CommunityCog(bot)
        pid = await group(bot, cog)
        await bot.db.write(data.act, pid, 1, 10, False, 'close')
        await lfg.delete()
        await cog.post(pid)
        assert (await bot.db.read(data.get, pid))['message_deleted'] == 1
        lfg.send.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())
