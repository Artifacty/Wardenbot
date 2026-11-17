import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from wardenbot.bot import WardenBot
from wardenbot.config import Config
from wardenbot.db import Database


def make_bot(scope=None):
    bot = WardenBot(Config(token='unused', dev_guild_id=scope), Database(':memory:'))
    @bot.tree.command(name='menu', description='Открыть меню')
    async def menu(interaction):
        pass
    remote = [SimpleNamespace(name='menu', type=discord.AppCommandType.chat_input)]
    bot.tree.sync = AsyncMock(return_value=remote)
    bot.tree.fetch_commands = AsyncMock(return_value=remote)
    return bot


@pytest.mark.parametrize('scope', [None, 42])
def test_deleted_remote_commands_restored_despite_unchanged_local_cache(scope):
    async def go():
        bot = make_bot(scope)
        await bot._sync_commands()
        bot.tree.sync.reset_mock()
        bot.tree.fetch_commands.return_value = []
        await bot._sync_commands()
        bot.tree.sync.assert_awaited_once()
        target = bot.tree.sync.call_args.kwargs.get('guild')
        assert getattr(target, 'id', None) == scope
        bot.db.close()
    asyncio.run(go())


def test_verified_command_list_does_not_repeat_registration():
    async def go():
        bot = make_bot(42)
        await bot._sync_commands()
        bot.tree.sync.reset_mock()
        await bot._sync_commands()
        bot.tree.fetch_commands.assert_awaited_once()
        bot.tree.sync.assert_not_awaited()
        assert bot.tree.get_command('menu', guild=discord.Object(id=42)) is not None
        bot.db.close()
    asyncio.run(go())


def test_force_registration_works_without_using_remote_cache():
    async def go():
        bot = make_bot()
        await bot._sync_commands()
        bot.tree.sync.reset_mock()
        await bot._sync_commands(force=True)
        bot.tree.sync.assert_awaited_once_with()
        bot.tree.fetch_commands.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_failed_target_registration_preserves_previous_server_registration():
    async def go():
        bot = make_bot(43)
        bot.cfg(0)['commands_scope.None'] = '42'
        bot.tree.sync.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Access')
        with pytest.raises(discord.Forbidden):
            await bot._sync_commands()
        assert [call.kwargs['guild'].id for call in bot.tree.sync.call_args_list] == [43]
        assert bot.cfg(0)['commands_scope.None'] == '42'
        bot.db.close()
    asyncio.run(go())


def test_cached_scope_switch_updates_scope_marker_after_cleanup():
    async def go():
        bot = make_bot(42)
        await bot._sync_commands()
        bot.cfg(0)['commands_scope.None'] = '43'
        bot.tree.sync.reset_mock()
        await bot._sync_commands()
        assert bot.tree.sync.call_args.kwargs['guild'].id == 43
        assert bot.cfg(0)['commands_scope.None'] == '42'
        bot.tree.sync.reset_mock()
        await bot._sync_commands()
        bot.tree.sync.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_failed_remote_check_does_not_delete_or_rewrite_commands(caplog):
    async def go():
        bot = make_bot()
        await bot._sync_commands()
        bot.tree.sync.reset_mock()
        bot.tree.fetch_commands.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason='Unavailable'), 'Unavailable')
        await bot._sync_commands()
        bot.tree.sync.assert_not_awaited()
        assert 'Could not verify' in caplog.text
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('args,forced', [([], False), (['--sync-commands'], True)])
def test_startup_cli_passes_force_flag(monkeypatch, args, forced):
    import wardenbot.__main__ as entry
    config = Config(token='unused', db_path=':memory:')
    monkeypatch.setattr(entry, 'load_config', lambda: config)
    constructor = MagicMock()
    monkeypatch.setattr(entry, 'WardenBot', constructor)
    assert entry.main(args) == 0
    assert constructor.call_args.kwargs == {'force_command_sync': forced}
    constructor.return_value.run.assert_called_once_with('unused', log_handler=None)


def test_startup_lists_servers_and_warns_about_single_server_scope(caplog):
    async def go():
        bot = make_bot(42)
        bot._connection._guilds = {
            42: SimpleNamespace(id=42, name='Основной сервер'),
            43: SimpleNamespace(id=43, name='Тестовый сервер'),
        }
        with caplog.at_level('INFO'):
            await bot.on_ready()
        assert 'Основной сервер' in caplog.text and 'Тестовый сервер' in caplog.text
        assert 'Commands are registered for server 42 only' in caplog.text
        bot.db.close()
    asyncio.run(go())
