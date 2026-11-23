import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
import pytest
from wardenbot import archives, repo
from wardenbot.db import Database
from wardenbot.operation_archive import OperationArchiver
from wardenbot.slots import SlotSpec

NOW = datetime.now(UTC)

async def stream(items):
    for item in items:
        yield item

async def fixture(path):
    db = Database(str(path))
    op_id = await db.write(lambda c: repo.op_create(c, guild_id=1, organizer_id=2,
        title='Archive test', region='R', type='offense', language='ru', start_at=NOW,
        duration_min=60, requirements='', briefing_public='Public plan', briefing_closed='PRIVATE SECRET',
        slots=[SlotSpec('Inf',1)], supply=[], now=NOW))
    await db.write(repo.op_finish, op_id, NOW)
    op = await db.read(repo.op_get, op_id)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 55
    sent = []
    async def send(**kw):
        msg = SimpleNamespace(id=100,author=SimpleNamespace(id=999),embeds=[kw['embed']])
        sent.append(msg)
        return msg
    channel.send = AsyncMock(side_effect=send)
    channel.history = lambda **kw: stream(sent)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: sent[0])
    guild = SimpleNamespace(id=1, filesize_limit=10_000_000, get_channel=lambda cid: channel)
    bot = SimpleNamespace(intents=SimpleNamespace(message_content=True),db=db,user=SimpleNamespace(id=999),get_guild=lambda gid:guild,
                          cfg=lambda gid:{'ch.ops_archive':'55'})
    thread = MagicMock(spec=discord.Thread)
    thread.edit = AsyncMock()
    thread.delete = AsyncMock()
    thread.history = lambda **kw: stream([SimpleNamespace(id=77,author=SimpleNamespace(id=3),
        created_at=NOW,content='Public discussion',embeds=[],attachments=[])])
    return bot, op, channel, thread, sent


def test_archive_precedes_delete_and_survives_restart(tmp_path):
    async def go():
        path = tmp_path/'db'
        bot, op, channel, thread, sent = await fixture(path)
        async def delete(**kw):
            assert sent and (await bot.db.read(archives.get,op['id']))['message_id'] == 100
        thread.delete.side_effect = delete
        await OperationArchiver(bot).run(op,thread)
        job = await bot.db.read(archives.get,op['id'])
        assert 'PRIVATE SECRET' not in job['payload']
        payload = json.loads(job['payload'])
        assert 'discussion' not in payload
        assert 'file' not in channel.send.call_args.kwargs
        assert 'files' not in channel.send.call_args.kwargs
        assert payload['operation']['briefing_public'] == 'Public plan'
        assert job['source_deleted']
        bot.db.close()
        bot.db = Database(str(path))
        assert await bot.db.read(archives.pending) == []
        await OperationArchiver(bot).run(op,thread)
        channel.send.assert_awaited_once()
        thread.delete.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_failed_send_never_deletes_source(tmp_path):
    async def go():
        bot,op,channel,thread,_ = await fixture(tmp_path/'db')
        send = channel.send.side_effect
        channel.send.side_effect = RuntimeError('network')
        with pytest.raises(RuntimeError):
            await OperationArchiver(bot).run(op,thread)
        thread.delete.assert_not_awaited()
        assert len(await bot.db.read(archives.pending)) == 1
        channel.send.side_effect = send
        await OperationArchiver(bot).run(op,thread)
        thread.delete.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_receipt_loss_reconciles_without_duplicate(tmp_path,monkeypatch):
    async def go():
        bot,op,channel,thread,_ = await fixture(tmp_path/'db')
        real = archives.receipt
        def crash(*a):
            raise RuntimeError('crash after send')
        monkeypatch.setattr(archives,'receipt',crash)
        with pytest.raises(RuntimeError):
            await OperationArchiver(bot).run(op,thread)
        thread.delete.assert_not_awaited()
        monkeypatch.setattr(archives,'receipt',real)
        bot.db.close()
        bot.db = Database(str(tmp_path/'db'))
        await OperationArchiver(bot).run(op,thread)
        channel.send.assert_awaited_once()
        thread.delete.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_failed_delete_retries_without_resending(tmp_path):
    async def go():
        bot,op,channel,thread,_ = await fixture(tmp_path/'db')
        thread.delete.side_effect = RuntimeError('permission')
        with pytest.raises(RuntimeError):
            await OperationArchiver(bot).run(op,thread)
        assert not (await bot.db.read(archives.get,op['id']))['source_deleted']
        thread.delete.side_effect = None
        await OperationArchiver(bot).run(op,thread)
        channel.send.assert_awaited_once()
        assert thread.delete.await_count == 2
        bot.db.close()
    asyncio.run(go())


def test_missing_archive_leaves_board_intact(tmp_path):
    async def go():
        bot,op,channel,thread,_ = await fixture(tmp_path/'db')
        bot.cfg = lambda gid:{}
        await OperationArchiver(bot).run(op,thread)
        channel.send.assert_not_awaited()
        thread.delete.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_archive_ignores_discussion_and_needs_no_message_content_intent(tmp_path):
    async def go():
        bot,op,channel,thread,_ = await fixture(tmp_path/'db')
        bot.intents.message_content = False
        thread.history = MagicMock(side_effect=AssertionError('Discussion must not be read'))
        await OperationArchiver(bot).run(op,thread)
        channel.send.assert_awaited_once()
        assert 'file' not in channel.send.call_args.kwargs
        thread.history.assert_not_called()
        thread.delete.assert_awaited_once()
        assert (await bot.db.read(archives.get,op['id']))['source_deleted']
        bot.db.close()
    asyncio.run(go())


def test_bot_only_post_archives_without_message_content_intent(tmp_path):
    async def go():
        bot,op,channel,thread,_ = await fixture(tmp_path/'db')
        bot.intents.message_content = False
        thread.history = lambda **kw: stream([SimpleNamespace(id=77,author=bot.user,
            created_at=NOW,content='Объявление бота',embeds=[],attachments=[])])
        await OperationArchiver(bot).run(op,thread)
        channel.send.assert_awaited_once()
        thread.delete.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_old_pending_snapshot_sends_card_without_legacy_discussion_file(tmp_path):
    async def go():
        bot, op, channel, thread, _ = await fixture(tmp_path / 'old_archive.db')
        payload = {'card': discord.Embed(title='Итоги').to_dict(),
                   'discussion': [{'content': 'Не публиковать текст переписки'}]}
        await bot.db.write(archives.save, op['id'], channel.id, payload)
        bot.intents.message_content = False
        await OperationArchiver(bot).run(op, thread)
        sent = channel.send.call_args.kwargs
        assert 'file' not in sent and 'files' not in sent
        assert 'Не публиковать' not in str(sent)
        thread.delete.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())
