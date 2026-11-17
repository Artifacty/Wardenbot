import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
import pytest
from wardenbot import repo, role_jobs, services
from wardenbot.bot import WardenBot
from wardenbot.config import Config
from wardenbot.db import Database
from wardenbot.i18n import user_language
from wardenbot.cogs.menu import MenuView, MenuCog
from wardenbot.cogs.selfroles import RoleButton

NOW = datetime.now(UTC)


def inter(bot, names=(), locale='ru'):
    i = MagicMock()
    i.client = bot
    i.guild = SimpleNamespace(id=1)
    i.channel_id = 99
    i.locale = locale
    i.user = MagicMock(spec=discord.Member)
    i.user.id = 10
    i.user.roles = [SimpleNamespace(id=n+100, name=name) for n,name in enumerate(names)]
    i.response.is_done.return_value = False
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


@pytest.mark.parametrize('names,saved,status,expected', [
    (['ru'],'en','admitted','ru'), (['engl'],'ru','admitted','en'),
    (['ru','engl'],'en','admitted','en'), (['ru','engl'],'ru','admitted','ru'),
    ([], 'ru','admitted','ru'), (['ru'],'ru','pending','en'),
    (['ru'],'ru','rejected','en'), (['en'],'ru','admitted','en'),
])
def test_russian_interface_ignores_legacy_roles_and_client_locale(names,saved,status,expected):
    async def go():
        bot = WardenBot(Config(token='unused'), Database(':memory:'))
        await bot.db.write(repo.user_update,10,NOW,language=saved,status=status)
        i = inter(bot,names,locale='ru' if expected == 'en' else 'en-US')
        assert await user_language(i) == 'ru'
        if status == 'admitted':
            menu = MenuView(10,await user_language(i))
            assert menu.children[0].item.label == 'Создать операцию'
        bot.cfg(1)['ch.screening'] = '99'
        assert await user_language(i) == 'ru'
        bot.db.close()
    asyncio.run(go())


def test_configured_role_id_survives_rename_and_failed_change_overrides_old_role():
    async def go():
        bot = WardenBot(Config(token='unused'), Database(':memory:'))
        await bot.db.write(repo.user_update,10,NOW,status='admitted',language='en')
        i = inter(bot,['renamed'])
        bot.cfg(1)['role.lang_ru'] = '100'
        assert await user_language(i) == 'ru'
        await bot.db.write(role_jobs.enqueue,1,10,'language','en')
        assert await user_language(i) == 'ru'
        bot.db.close()
    asyncio.run(go())


def test_old_language_button_no_longer_changes_preferences(monkeypatch,tmp_path):
    async def go():
        path = str(tmp_path/'db')
        bot = WardenBot(Config(token='unused'),Database(path))
        await bot.db.write(repo.user_update,10,NOW,status='admitted',language='en')
        i = inter(bot,['engl'])
        from wardenbot.cogs import selfroles
        monkeypatch.setattr(selfroles,'sync_aux_roles',AsyncMock(return_value=False))
        await RoleButton('lang_ru').callback(i)
        bot.db.close()
        bot = WardenBot(Config(token='unused'),Database(path))
        assert (await bot.db.read(repo.user_get,10))['language'] == 'en'
        job = await bot.db.read(role_jobs.get,1,10,'language')
        assert job is None
        bot.db.close()
    asyncio.run(go())


def test_language_sync_removes_opposite_role_and_does_not_repeat():
    async def go():
        bot = WardenBot(Config(token='unused'),Database(':memory:'))
        ru,en = SimpleNamespace(id=100,name='ru'),SimpleNamespace(id=101,name='engl')
        member = SimpleNamespace(roles=[en],remove_roles=AsyncMock(),add_roles=AsyncMock())
        guild = SimpleNamespace(id=1,roles=[ru,en],fetch_member=AsyncMock(return_value=member),
                                get_role=lambda rid: ru if rid==100 else en)
        bot.cfg(1).update({'role.lang_ru':'100','role.lang_en':'101'})
        await bot.db.write(role_jobs.enqueue,1,10,'language','ru')
        assert await services.sync_aux_roles(bot,guild,10,'language')
        assert await services.sync_aux_roles(bot,guild,10,'language')
        member.remove_roles.assert_awaited_once_with(en,reason='Смена роли')
        member.add_roles.assert_awaited_once_with(ru,reason='Смена роли')
        bot.db.close()
    asyncio.run(go())


def test_native_localization_and_preverification_exception():
    async def go():
        from wardenbot.bot import EXTENSIONS
        from wardenbot.command_locale import CommandTranslator
        bot = WardenBot(Config(token='unused'),Database(':memory:'))
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        translator = CommandTranslator()
        menu = await bot.tree.get_command('menu').get_translated_payload(bot.tree,translator)
        assert not menu['name_localizations']
        assert bot.tree.get_command('language') is None
        assert bot.tree.get_command('setup').default_permissions is None
        def check_names(command):
            assert not command.get('name_localizations')
            for child in command.get('options', []):
                check_names(child)
        for command in bot.tree.get_commands():
            check_names(await command.get_translated_payload(bot.tree,translator))
        verify = await bot.tree.get_command('verify').get_translated_payload(bot.tree,translator)
        assert not verify['name_localizations']
        for opt in verify['options']:
            assert not opt['name_localizations']
        for name in list(bot.cogs):
            await bot.remove_cog(name)
        bot.db.close()
    asyncio.run(go())
