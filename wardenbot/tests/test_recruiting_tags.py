import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from test_release_operations import case, ADMIN, NOW
from wardenbot import repo, operation_service as ops
from wardenbot.cogs.operations import OperationsCog, ManageView, wanted_tag_names
from wardenbot.db import Database
from wardenbot.opsmeta import ALL_FORUM_TAGS, CLOSED_SIGNUP_TAG, STATUS_TAG


@pytest.mark.parametrize('status', ['open', 'running', 'done', 'cancelled'])
@pytest.mark.parametrize('opened', [True, False])
def test_recruiting_tag_matches_state_and_fits_discord_limit(status, opened):
    names = wanted_tag_names(SimpleNamespace(status=status, signup_open=opened, urgent=True,
                                             type='offense', language='ru'))
    assert len(names) <= 5
    assert all(len(name) <= 20 and name in ALL_FORUM_TAGS for name in names)
    if status in ('done', 'cancelled'):
        assert STATUS_TAG[status] in names
        assert CLOSED_SIGNUP_TAG not in names and STATUS_TAG['open'] not in names
    else:
        assert (CLOSED_SIGNUP_TAG in names) == (not opened)
        assert (STATUS_TAG['open'] in names) == opened
        if status == 'running':
            assert STATUS_TAG['running'] in names


async def transport(db, oid):
    guild = MagicMock(spec=discord.Guild)
    forum = MagicMock(spec=discord.ForumChannel)
    forum.id, forum.guild = 55, guild
    def tagged(name, tid):
        tag = discord.ForumTag(name=name)
        tag.id = tid
        return tag
    forum.available_tags = [tagged(name, i+1)
                            for i, name in enumerate(ALL_FORUM_TAGS) if name != CLOSED_SIGNUP_TAG]
    async def create_tag(**kwargs):
        tag = tagged(kwargs['name'], 99)
        forum.available_tags.append(tag)
        return tag
    forum.create_tag = AsyncMock(side_effect=create_tag)
    guild.fetch_channel = AsyncMock(return_value=forum)
    thread = MagicMock(spec=discord.Thread)
    thread.parent, thread.name, thread.applied_tags = forum, 'Operation', []
    async def edit(**kwargs):
        for key, value in kwargs.items():
            setattr(thread, key, value)
    thread.edit = AsyncMock(side_effect=edit)
    thread.get_partial_message.return_value = SimpleNamespace(edit=AsyncMock())
    guild.get_thread.return_value = thread
    bot = SimpleNamespace(db=db, get_guild=lambda gid: guild, get_cog=lambda name: None)
    await db.write(repo.op_update, oid, message_id=456)
    return bot, forum, thread


@pytest.mark.parametrize('status', ['open', 'running'])
def test_button_closes_and_reopens_tag_on_existing_forum(status):
    async def go():
        db, oid, _, _ = await case()
        await db.write(repo.op_update, oid, status=status, urgent=1)
        bot, forum, thread = await transport(db, oid)
        cog = OperationsCog(bot)
        cog.actor = lambda interaction: ADMIN
        cog.allowed_manage = AsyncMock(return_value=True)
        cog.request_refresh = MagicMock()
        panel = ManageView(cog, oid, 'ru', status, True)
        interaction = SimpleNamespace(user=SimpleNamespace(id=99), response=SimpleNamespace(
            defer=AsyncMock(), is_done=lambda: True), followup=SimpleNamespace(send=AsyncMock()))
        for closed in (True, False):
            await panel.recruiting(interaction)
            cog.request_refresh.assert_called_with(oid)
            await cog.refresh_card(oid)
            names = [tag.name for tag in thread.applied_tags]
            assert (CLOSED_SIGNUP_TAG in names) == closed
            assert (STATUS_TAG['open'] in names) == (not closed)
            assert not await db.read(repo.dirty_cards)
        forum.create_tag.assert_awaited_once()
        db.close()
    asyncio.run(go())


def test_failed_tag_edit_retries_after_restart(tmp_path):
    async def go():
        path = tmp_path / 'tags.db'
        db, oid, _, _ = await case(path)
        bot, forum, thread = await transport(db, oid)
        await db.write(ops.set_recruiting, ADMIN, oid, False, NOW)
        edit = thread.edit.side_effect
        async def failing(**kwargs):
            if 'applied_tags' in kwargs:
                raise discord.HTTPException(SimpleNamespace(status=503, reason='Unavailable'), 'Retry')
            await edit(**kwargs)
        thread.edit.side_effect = failing
        await OperationsCog(bot).refresh_card(oid)
        assert await db.read(repo.dirty_cards)
        db.close()
        bot.db = Database(str(path))
        thread.edit.side_effect = edit
        await OperationsCog(bot).refresh_card(oid)
        assert CLOSED_SIGNUP_TAG in [tag.name for tag in thread.applied_tags]
        assert not await bot.db.read(repo.dirty_cards)
        forum.create_tag.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())


def test_startup_repairs_previously_acknowledged_closed_recruitment():
    async def go():
        db, oid, _, _ = await case()
        await db.write(ops.set_recruiting, ADMIN, oid, False, NOW)
        row = await db.read(repo.op_get, oid)
        await db.write(repo.card_ack, oid, row['card_revision'])
        assert not await db.read(repo.dirty_cards)
        bot = SimpleNamespace(db=db, add_dynamic_items=MagicMock())
        cog = OperationsCog(bot)
        cog.tick, cog.archive_tick = MagicMock(), MagicMock()
        await cog.cog_load()
        assert [row['id'] for row in await db.read(repo.dirty_cards)] == [oid]
        db.close()
    asyncio.run(go())
