import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
import pytest
from discord import app_commands
from test_discord_layer import make_bot, fake_member, fake_interaction
from wardenbot.cogs.menu import MenuView, MenuCog, OperationOptions
from wardenbot.cogs.operations import OperationsCog
from wardenbot.cogs.setup_cog import SetupCog
from wardenbot.perms import can_admin
from wardenbot.timeutil import ParseError
from wardenbot import repo
from wardenbot.slots import SlotSpec
from datetime import timedelta
from wardenbot.services import utcnow

@pytest.mark.parametrize('native', [True,False])
def test_admin_can_open_menu_and_create_without_verification_or_clan(native):
    async def go():
        bot = make_bot()
        bot.cfg(1)['role.command'] = '501'
        user = fake_member(2, [] if native else [501])
        user.guild_permissions.administrator = native
        i = fake_interaction(user,bot=bot)
        i.response.send_modal = AsyncMock()
        forum = MagicMock(spec=discord.ForumChannel)
        forum.id = 55
        i.guild.get_channel = lambda cid:forum
        bot.cfg(1)['ch.ops_board'] = '55'
        assert can_admin(bot,user)
        assert await MenuView(2,'ru').interaction_check(i)
        await MenuCog.menu.callback(MenuCog(), i)
        assert isinstance(i.followup.send.call_args.kwargs['view'],MenuView)
        cog = OperationsCog(bot)
        await cog.create.callback(cog,i,app_commands.Choice(name='Наступление',value='offense'))
        assert i.response.send_modal.called
        draft = cog.drafts[2]
        assert draft.clan_id is None and draft.language == 'ru'
        draft.start_at = utcnow() + timedelta(hours=2)
        draft.slots = [SlotSpec('Пехота', 3)]
        draft.raw1 = {'title': 'Операция сообщества'}
        forum.available_tags = []
        forum.create_thread = AsyncMock(return_value=SimpleNamespace(
            thread=SimpleNamespace(id=10,jump_url='https://discord.com/channels/1/10'),
            message=SimpleNamespace(id=11)))
        assert await cog.publish_draft(i,draft) == 'https://discord.com/channels/1/10'
        stored = await bot.db.read(repo.op_get,1)
        assert stored['organizer_id'] == 2 and stored['clan_id'] is None
        user.guild_permissions.administrator = False
        user.roles = []
        with pytest.raises(ParseError):
            await cog.publish_draft(i,draft)
        assert await cog.creator_clan(i) is None
        bot.db.close()
    asyncio.run(go())


def test_menu_has_no_language_selector():
    async def go():
        assert [b.item.label for b in MenuView(1,'ru').children] == ['Создать операцию','Доска операций','Помощь']
        assert len(OperationOptions(1,'ru').children) == 2
    asyncio.run(go())


def test_start_cleanup_deletes_only_recorded_bot_panel():
    async def go():
        bot = make_bot()
        bot._connection.user = SimpleNamespace(id=999)
        bot.cfg(1)['msg.start'] = '100'
        channel = MagicMock(spec=discord.TextChannel)
        message = SimpleNamespace(author=SimpleNamespace(id=999),delete=AsyncMock())
        channel.fetch_message = AsyncMock(return_value=message)
        cog = SetupCog(bot)
        await cog.remove_panel(SimpleNamespace(id=1),'start',channel)
        message.delete.assert_awaited_once()
        assert bot.cfg(1)['msg.start'] == ''
        bot.cfg(1)['msg.start'] = '101'
        message.author.id = 5
        await cog.remove_panel(SimpleNamespace(id=1),'start',channel)
        assert message.delete.await_count == 1
        bot.db.close()
    asyncio.run(go())
