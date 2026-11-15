import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from wardenbot import db as storage, repo, services
from wardenbot.bot import WardenBot
from wardenbot.config import Config
from wardenbot.cogs import verification
from wardenbot.cogs.setup_cog import SetupCog
from wardenbot.i18n import user_language
from wardenbot.layout import CATEGORIES, CHANNEL_BY_KEY, ROLES, LEGACY_ROLE_KEYS, role_display_name, viewer_roles
from test_verification_safety import interaction

NOW = datetime.now(UTC)


def test_language_migration_preserves_existing_data(tmp_path, monkeypatch):
    path = str(tmp_path / 'old.db')
    migrations = storage.MIGRATIONS
    monkeypatch.setattr(storage, 'MIGRATIONS', migrations[:1])
    c = storage.connect(path)
    repo.user_update(c, 10, NOW, status='admitted', rank='private', steam_key='original')
    repo.cfg_set(c, 1, 'ch.sell', 321)
    c.close()
    monkeypatch.setattr(storage, 'MIGRATIONS', migrations)
    c = storage.connect(path)
    assert repo.user_get(c, 10)['steam_key'] == 'original'
    assert repo.user_get(c, 10)['language'] == 'en'
    assert repo.cfg_get(c, 1, 'ch.sell') == '321'
    assert repo.role_jobs_pending(c) == []
    c.close()


def test_required_languages_and_access_layout():
    names = [role_display_name(r) for r in ROLES]
    assert len(names) == len(set(names))
    assert not any(re.search('[А-Яа-яЁё]', n) for n in names)
    assert {'Admin','Moderator','Clan Leader','Clan Delegate','Unverified',
            'Verification Rejected','Verified','Member','Veteran'} <= set(names)
    assert set(viewer_roles('screening')) == {'staff', 'command'}
    assert {c.key for cat in CATEGORIES for c in cat.channels if c.access == 'public'} == {'rules','start','verify'}
    for key in ('rules','start','verify'):
        ch = CHANNEL_BY_KEY[key]
        assert not re.search('[А-Яа-яЁё]', ch.name() + ch.topic)
    for key in ('mod','screening','audit_log','bot_admin'):
        assert not re.search('[a-zA-Z]', CHANNEL_BY_KEY[key].name())


def test_verification_form_is_russian_without_language_choice():
    async def go():
        form = verification.VerifyModal('ru')
        assert form.title == 'Допуск'
        assert not hasattr(form, 'language')
        assert any('Имя' in label.text for label in form.children)
        bot = WardenBot(Config(token='unused'), storage.Database(':memory:'))
        i = interaction(bot)
        i.user.id = 10
        form.name._value = 'Player'
        await form.on_submit(i)
        req = await bot.db.read(repo.req_latest_for_user, 10)
        assert req['language'] == 'ru' and req['steam_key'] == ''
        text = i.followup.send.call_args.args[0]
        assert 'скриншот' in text and 'F1' in text and req['code'] in text
        assert await user_language(i) == 'ru'
        await bot.db.write(repo.user_update, 10, NOW, status='admitted')
        assert await user_language(i) == 'ru'
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('decision, expected', [('approved','Допуск одобрен'), ('rejected','Допуск отклонён')])
def test_decision_language_and_durable_role_job(monkeypatch, decision, expected):
    async def go():
        bot = WardenBot(Config(token='unused'), storage.Database(':memory:'))
        rid = await bot.db.write(repo.req_create, 1, 10, 'WH-ABCD', '', '', 'Player', '', '', '', NOW, 'ru')
        await bot.db.write(repo.req_attach_review, rid, 'awaiting_screenshot', None, 'a.png', 50, 100)
        i = interaction(bot)
        i.guild.get_member = lambda uid: SimpleNamespace(id=uid, created_at=NOW-timedelta(days=30))
        i.message = SimpleNamespace(id=100, edit=AsyncMock())
        dm = AsyncMock()
        monkeypatch.setattr(verification, 'safe_dm', dm)
        monkeypatch.setattr(verification, 'sync_member_roles', AsyncMock(return_value=False))
        monkeypatch.setattr(verification, 'audit', AsyncMock())
        await verification.decide(i, rid, decision, 'Invalid screenshot')
        assert expected in dm.call_args.args[1]
        job = await bot.db.read(repo.role_job_get, 1, 10)
        assert job['target'] == ('recruit' if decision == 'approved' else 'rejected')
        assert job['applied_revision'] == 0
        assert 'ожидает повтора' in i.followup.send.call_args.args[0]
        bot.db.close()
    asyncio.run(go())


def test_role_sync_retries_after_restart_and_does_not_repeat_success(tmp_path, monkeypatch):
    async def go():
        path = str(tmp_path / 'roles.db')
        bot = WardenBot(Config(token='unused'), storage.Database(path))
        guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=SimpleNamespace(id=10)))
        await bot.db.write(repo.role_job_set, 1, 10, 'recruit')
        apply = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing permissions'))
        monkeypatch.setattr(services, 'set_rank_roles', apply)
        assert not await services.sync_member_roles(bot, guild, 10)
        bot.db.close()
        bot = WardenBot(Config(token='unused'), storage.Database(path))
        apply.side_effect = None
        assert await services.sync_member_roles(bot, guild, 10)
        assert await services.sync_member_roles(bot, guild, 10)
        assert apply.await_count == 2
        assert await bot.db.read(repo.role_jobs_pending) == []
        bot.db.close()
    asyncio.run(go())


def test_newer_role_job_survives_older_ack(monkeypatch):
    async def go():
        bot = WardenBot(Config(token='unused'), storage.Database(':memory:'))
        await bot.db.write(repo.role_job_set, 1, 10, 'recruit')
        async def apply(*args, **kwargs):
            await bot.db.write(repo.role_job_set, 1, 10, 'rejected')
        monkeypatch.setattr(services, 'set_rank_roles', apply)
        guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=SimpleNamespace(id=10)))
        await services.sync_member_roles(bot, guild, 10)
        jobs = await bot.db.read(repo.role_jobs_pending)
        assert len(jobs) == 1 and jobs[0]['target'] == 'rejected'
        bot.db.close()
    asyncio.run(go())


def test_missing_target_does_not_remove_existing_roles():
    async def go():
        bot = WardenBot(Config(token='unused'), storage.Database(':memory:'))
        member = SimpleNamespace(guild=SimpleNamespace(id=1), roles=[], remove_roles=AsyncMock())
        with pytest.raises(RuntimeError):
            await services.set_rank_roles(bot, member, 'recruit', 'test')
        member.remove_roles.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_setup_reuses_role_ids_and_preserves_trading_channels():
    async def go():
        bot = WardenBot(Config(token='unused'), storage.Database(':memory:'))
        guild = MagicMock()
        guild.id = 1
        guild.roles = []
        guild.me.top_role.position = 1
        guild.get_role.side_effect = lambda rid: next((r for r in guild.roles if r.id == rid), None)
        async def create(**kwargs):
            r = MagicMock()
            r.id, r.name = len(guild.roles)+100, kwargs['name']
            async def edit(**changes):
                r.name = changes['name']
                return r
            r.edit = AsyncMock(side_effect=edit)
            guild.roles.append(r)
            return r
        guild.create_role = AsyncMock(side_effect=create)
        cog = SetupCog(bot)
        roles = await cog._ensure_roles(guild, [])
        roles['recruit'].name = '› Рекрут · Recruit'
        await cog._ensure_roles(guild, [])
        assert roles['recruit'].name == 'Verified'
        assert guild.create_role.await_count == len(ROLES) - len(LEGACY_ROLE_KEYS)
        existing = MagicMock(spec=discord.ForumChannel)
        existing.edit = AsyncMock()
        guild.get_channel.return_value = existing
        bot.cfg(1)['ch.sell'] = '123'
        assert await cog._ensure_channel(guild, MagicMock(), CHANNEL_BY_KEY['sell'], roles, True, []) is existing
        existing.edit.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


def test_dev_command_tree_restored_when_remote_sync_is_cached(tmp_path):
    async def go():
        path = str(tmp_path / 'sync.db')
        for expected_syncs in (1, 0):
            bot = WardenBot(Config(token='unused', dev_guild_id=42), storage.Database(path))
            await bot._load_cfg()
            @bot.tree.command(name='probe', description='Check local dispatch')
            async def probe(interaction):
                pass
            bot.tree.sync = AsyncMock(return_value=[])
            await bot._sync_commands()
            assert bot.tree.sync.await_count == expected_syncs
            assert bot.tree.get_command('probe', guild=discord.Object(id=42)) is not None
            bot.db.close()
    asyncio.run(go())
