"""Regression tests for stale interactions, parallel uploads and persistence."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from wardenbot import repo
from wardenbot.bot import WardenBot
from wardenbot.config import Config
from wardenbot.db import Database
from wardenbot.cogs import verification

NOW = datetime.now(UTC)


def request(conn):
    return repo.req_create(conn, 1, 10, 'WH-TEST', 'id64:76561198000000000',
                           'https://steamcommunity.com/profiles/76561198000000000',
                           'Player', '', '', '', NOW)


def interaction(bot, moderator=True):
    i = MagicMock()
    i.client = bot
    i.user = MagicMock(spec=discord.Member)
    i.user.id = 99
    i.user.guild_permissions = SimpleNamespace(administrator=moderator)
    i.user.roles = []
    i.user.created_at = NOW - timedelta(days=30)
    i.guild = SimpleNamespace(id=1)
    i.locale = 'en-US'
    i.response.is_done.return_value = True
    i.followup.send = AsyncMock()
    i.message = SimpleNamespace(id=100)
    return i


@pytest.mark.parametrize('decision', ['approved', 'rejected'])
def test_delayed_screenshot_cannot_reopen_decided_request(tmp_path, decision):
    path = str(tmp_path / 'db.sqlite')
    db = Database(path)
    rid = request(db.conn)
    assert repo.req_attach_review(db.conn, rid, 'awaiting_screenshot', None, 'a.png', 50, 100)
    assert repo.req_decide(db.conn, rid, decision, 99, '', NOW)
    db.close()
    db = Database(path)
    try:
        assert not repo.req_attach_review(db.conn, rid, 'awaiting_screenshot', None, 'b.png', 50, 101)
        assert not repo.req_set_screenshot(db.conn, rid, 'b.png')
        row = repo.req_get(db.conn, rid)
        assert (row['status'], row['review_message_id']) == (decision, 100)
    finally:
        db.close()


def test_parallel_uploads_have_one_winner():
    async def go():
        db = Database(':memory:')
        rid = await db.write(request)
        results = await asyncio.gather(*[
            db.write(repo.req_attach_review, rid, 'awaiting_screenshot', None, 'a.png', 50, mid)
            for mid in (100, 101)
        ])
        assert sorted(results) == [False, True]
        row = await db.read(repo.req_get, rid)
        assert row['review_message_id'] == (100 if results[0] else 101)
        db.close()
    asyncio.run(go())


def test_need_info_allows_new_screenshot_but_not_old_card():
    db = Database(':memory:')
    try:
        rid = request(db.conn)
        assert repo.req_attach_review(db.conn, rid, 'awaiting_screenshot', None, 'a.png', 50, 100)
        assert repo.req_decide(db.conn, rid, 'needs_info', 99, 'More detail', NOW)
        assert repo.req_attach_review(db.conn, rid, 'needs_info', 100, 'b.png', 50, 101)
        assert not repo.req_attach_review(db.conn, rid, 'needs_info', 100, 'c.png', 50, 102)
    finally:
        db.close()


@pytest.mark.parametrize('scenario', ['permission_removed', 'wrong_guild', 'stale_card'])
def test_decision_rechecks_authority_and_card(scenario):
    async def go():
        db = Database(':memory:')
        bot = WardenBot(Config(token='unused'), db)
        rid = await db.write(request)
        await db.write(repo.req_attach_review, rid, 'awaiting_screenshot', None, 'a.png', 50, 100)
        i = interaction(bot, moderator=scenario != 'permission_removed')
        if scenario == 'wrong_guild':
            i.guild.id = 2
        if scenario == 'stale_card':
            i.message.id = 101
        await verification.decide(i, rid, 'rejected', 'No')
        assert (await db.read(repo.req_get, rid))['status'] == 'pending'
        assert await db.read(repo.user_get, 10) is None
        db.close()
    asyncio.run(go())


@pytest.mark.parametrize('status', ['admitted', 'banned', 'rejected', 'pending'])
def test_previously_opened_modal_cannot_bypass_current_state(status):
    async def go():
        db = Database(':memory:')
        bot = WardenBot(Config(token='unused'), db)
        i = interaction(bot)
        i.user.id = 10
        await db.write(repo.user_update, 10, NOW, status=status,
                       last_reject_at=NOW.isoformat() if status == 'rejected' else None)
        if status == 'pending':
            rid = await db.write(request)
            await db.write(repo.req_set_screenshot, rid, 'a.png')
        modal = verification.VerifyModal('en')
        modal.steam._value = '76561198000000000'
        modal.name._value = 'Player'
        await modal.on_submit(i)
        assert (await db.read(repo.user_get, 10))['status'] == status
        count = db.conn.execute('SELECT COUNT(*) FROM verification_requests').fetchone()[0]
        assert count == (1 if status == 'pending' else 0)
        assert 'WH-' not in i.followup.send.call_args.args[0]
        db.close()
    asyncio.run(go())


def test_losing_upload_deletes_extra_card(monkeypatch):
    async def go():
        db = Database(':memory:')
        bot = WardenBot(Config(token='unused'), db)
        rid = await db.write(request)
        i = interaction(bot)
        i.user.id = 10
        i.response.defer = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 50
        extra = SimpleNamespace(id=101, delete=AsyncMock())

        async def send(**kwargs):
            # An earlier upload completed while this one awaited Discord.
            assert await db.write(repo.req_attach_review, rid, 'awaiting_screenshot', None,
                                  'first.png', 50, 100)
            return extra

        channel.send = AsyncMock(side_effect=send)
        monkeypatch.setattr(verification, 'get_text_channel', lambda *args: channel)
        image = SimpleNamespace(content_type='image/png', size=100, filename='img.png',
                                to_file=AsyncMock(return_value=SimpleNamespace(filename='screenshot.png')))
        cog = verification.VerificationCog(bot)
        await cog.screenshot.callback(cog, i, image)
        extra.delete.assert_awaited_once()
        row = await db.read(repo.req_get, rid)
        assert row['review_message_id'] == 100
        assert row['screenshot_url'] == 'first.png'
        assert 'sent for review' not in i.followup.send.call_args.args[0]
        db.close()
    asyncio.run(go())


@pytest.mark.parametrize('decision, user_status', [('approved', 'admitted'), ('rejected', 'rejected')])
def test_current_card_keeps_screenshot_and_can_be_decided_once(monkeypatch, decision, user_status):
    async def go():
        db = Database(':memory:')
        bot = WardenBot(Config(token='unused'), db)
        rid = await db.write(request)
        await db.write(repo.req_attach_review, rid, 'awaiting_screenshot', None, 'a.png', 50, 100)
        i = interaction(bot)
        applicant = SimpleNamespace(id=10, created_at=NOW - timedelta(days=30))
        i.guild.get_member = lambda uid: applicant
        card = SimpleNamespace(id=100, edit=AsyncMock(),
                               attachments=[SimpleNamespace(filename='screenshot.png')])
        i.message = card if decision == 'approved' else None
        roles = AsyncMock()
        monkeypatch.setattr(verification, 'sync_member_roles', roles)
        monkeypatch.setattr(verification, 'safe_dm', AsyncMock())
        monkeypatch.setattr(verification, 'audit', AsyncMock())
        await verification.decide(i, rid, decision, 'Причина', card)
        await verification.decide(i, rid, decision, 'Причина', card)
        roles.assert_awaited_once()
        assert (await db.read(repo.user_get, 10))['status'] == user_status
        card.edit.assert_awaited_once()
        changes = card.edit.call_args.kwargs
        assert 'attachments' not in changes  # Discord retains existing files.
        assert changes['embed'].image.url == 'attachment://screenshot.png'
        assert changes['view'] is None
        db.close()
    asyncio.run(go())
