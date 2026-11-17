import asyncio
import re
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_discord_layer import make_bot, fake_member, fake_interaction
from wardenbot import repo
from wardenbot.cogs.menu import MenuCog, MenuButton, MenuView
from wardenbot.instance_lock import instance_lock


def test_menu_acknowledges_before_waiting_for_database():
    async def go():
        bot=make_bot()
        interaction=fake_interaction(fake_member(10),bot=bot)
        async def slow_read(*args):
            assert interaction.response.defer.await_count==1
            assert interaction.response.is_done()
            return None
        bot.db.read=slow_read
        await MenuCog.menu.callback(MenuCog(),interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
        view=interaction.followup.send.call_args.kwargs['view']
        assert [b.action for b in view.children]==['verify','help']
        bot.db.close()
    asyncio.run(go())


def test_menu_button_reconstructed_after_restart_rechecks_access():
    async def go():
        bot=make_bot()
        i=fake_interaction(fake_member(10),bot=bot)
        item=MenuView(10).children[1].item
        restored=await MenuButton.from_custom_id(i,item,re.fullmatch(r'wb:menu:(?P<owner>\d+):(?P<action>\w+)',item.custom_id))
        await bot.db.write(repo.user_update,10,datetime.now(UTC),status='new')
        await restored.callback(i)
        assert 'отозван' in i.followup.send.call_args.args[0]
        other=fake_interaction(fake_member(11),bot=bot)
        await restored.callback(other)
        assert 'собственное' in other.response.send_message.call_args.args[0]
        bot.db.close()
    asyncio.run(go())


def test_second_process_cannot_use_database(tmp_path):
    path=str(tmp_path/'bot.db')
    script='from wardenbot.instance_lock import instance_lock\nimport sys\nwith instance_lock(sys.argv[1]): pass'
    with instance_lock(path):
        result=subprocess.run([sys.executable,'-c',script,path],capture_output=True,text=True)
        assert result.returncode!=0 and 'RuntimeError' in result.stderr
    with instance_lock(path): pass


def test_double_publication_returns_same_operation():
    from test_ru_admin import discord
    from unittest.mock import MagicMock
    from datetime import timedelta
    from wardenbot.cogs.operations import OperationsCog,Draft
    from wardenbot.slots import SlotSpec
    from wardenbot.services import utcnow
    async def go():
        bot=make_bot()
        user=fake_member(99)
        user.guild_permissions.administrator=True
        inter=fake_interaction(user,bot=bot)
        forum=MagicMock(spec=discord.ForumChannel)
        forum.id=55; forum.available_tags=[]
        forum.create_thread=AsyncMock(return_value=SimpleNamespace(thread=SimpleNamespace(id=123,jump_url='link'),message=SimpleNamespace(id=123)))
        inter.guild.get_channel=lambda cid:forum
        bot.cfg(1)['ch.ops_board']='55'
        cog=OperationsCog(bot)
        draft=Draft(1,99,'offense','ru','UTC',utcnow(),raw1={'title':'T'},start_at=utcnow()+timedelta(hours=1),slots=[SlotSpec('Inf',2)])
        result=await asyncio.gather(cog.publish_draft(inter,draft),cog.publish_draft(inter,draft))
        assert result==['link','link']
        forum.create_thread.assert_awaited_once()
        assert await bot.db.read(lambda c:c.execute('SELECT COUNT(*) FROM operations').fetchone()[0])==1
        bot.db.close()
    asyncio.run(go())


def test_command_scope_change_clears_old_guild_commands():
    from wardenbot.bot import WardenBot
    from wardenbot.config import Config
    from wardenbot.db import Database
    async def go():
        bot=WardenBot(Config(token='unused'),Database(':memory:'))
        bot.cfg(0)['commands_hash.42']='old'
        bot.tree.sync=AsyncMock(return_value=[])
        await bot._sync_commands()
        assert not bot.tree.sync.call_args_list[0].kwargs
        assert bot.tree.sync.call_args_list[-1].kwargs['guild'].id==42
        bot.db.close()
    asyncio.run(go())
