from datetime import UTC, datetime, timedelta
import pytest
from wardenbot import clans, repo, role_jobs
from wardenbot.db import connect
from wardenbot.slots import SlotSpec

NOW = datetime.now(UTC)

@pytest.fixture
def registry(tmp_path):
    path = str(tmp_path / 'clans.db')
    conn = connect(path)
    for uid in range(1, 6):
        repo.user_update(conn, uid, NOW, status='admitted')
    cid = clans.register(conn, 1, 'Wardens')
    clans.assign(conn, 1, 99, True, cid, 1, 'leader')
    clans.assign(conn, 1, 1, False, cid, 2, 'delegate')
    clans.assign(conn, 1, 1, False, cid, 3, 'delegate')
    op_id = repo.op_create(conn, guild_id=1, organizer_id=2, clan_id=cid,
        title='Test', region='', type='offense', language='ru', start_at=NOW+timedelta(hours=2),
        duration_min=60, requirements='', briefing_public='', briefing_closed='secret',
        slots=[SlotSpec('Inf', 1)], supply=[], now=NOW)
    yield conn, cid, op_id, path
    conn.close()


def test_clan_authority_and_revocation(registry):
    c, cid, op_id, _ = registry
    op = repo.op_get(c, op_id)
    assert clans.may_manage(c, 1, 1, op, False, {'regiment_co'})
    assert clans.may_manage(c, 1, 2, op, False, {'clan_delegate'})
    assert not clans.may_manage(c, 1, 3, op, False, {'clan_delegate'})
    assert not clans.may_manage(c, 1, 2, op, False, set())
    assert not clans.may_manage(c, 2, 1, op, True, {'regiment_co'})
    assert clans.may_manage(c, 1, 99, op, True, set())
    clans.remove(c, 1, 1, False, 2)
    # Stale Discord role does not preserve permission.
    assert not clans.may_manage(c, 1, 2, op, False, {'clan_delegate'})
    assert role_jobs.get(c, 1, 2, 'clan')['target'] == 'none'


def test_cannot_take_other_clan_or_demote_leader(registry):
    c, cid, _, _ = registry
    other = clans.register(c, 1, 'Other')
    clans.assign(c, 1, 99, True, other, 4, 'leader')
    for target in (1, 4):
        with pytest.raises(clans.ClanError, match='permission'):
            clans.assign(c, 1, 1, False, cid, target, 'delegate')
    with pytest.raises(clans.ClanError, match='leader_exists'):
        clans.assign(c, 1, 99, True, cid, 5, 'leader')
    repo.user_update(c, 1, NOW, status='rejected')
    with pytest.raises(clans.ClanError, match='permission'):
        clans.assign(c, 1, 1, False, cid, 5, 'delegate')
    assert clans.representative(c, 1, 1) is None


def test_registry_and_pending_roles_survive_restart(registry):
    c, cid, op_id, path = registry
    assert clans.register(c, 1, ' WARDENS ') == cid
    assert clans.register(c, 2, 'Wardens') != cid
    c.commit()
    restarted = connect(path)
    try:
        assert repo.op_get(restarted, op_id)['clan_id'] == cid
        assert clans.membership(restarted, 1, 2)['duty'] == 'delegate'
        assert role_jobs.get(restarted, 1, 2, 'clan')['applied_revision'] == 0
    finally:
        restarted.close()


def test_open_modal_rechecks_removed_assignment():
    import asyncio
    from test_discord_layer import make_bot, fake_member, fake_interaction
    from wardenbot.cogs.operations import OperationsCog, BriefingModal

    async def go():
        bot = make_bot()
        bot.cfg(1)['role.clan_delegate'] = '501'
        await bot.db.write(repo.user_update, 2, NOW, status='admitted')
        cid = await bot.db.write(clans.register, 1, 'Wardens')
        await bot.db.write(clans.assign, 1, 99, True, cid, 2, 'delegate')
        op_id = await bot.db.write(lambda c: repo.op_create(c, guild_id=1, organizer_id=2,
            clan_id=cid, title='T', region='', type='offense', language='en',
            start_at=NOW+timedelta(hours=2), duration_min=60, requirements='',
            briefing_public='', briefing_closed='original', slots=[SlotSpec('Inf',1)], supply=[], now=NOW))
        cog = OperationsCog(bot)
        interaction = fake_interaction(fake_member(2,[501]), bot=bot)
        op = await bot.db.read(repo.op_get, op_id)
        assert await cog.allowed_manage(interaction, op)
        modal = BriefingModal(cog, op_id, 'en', op)
        await bot.db.write(clans.remove, 1, 99, True, 2)
        await modal.on_submit(interaction)
        assert (await bot.db.read(repo.op_get, op_id))['briefing_closed'] == 'original'
        assert interaction.response.send_message.called
        assert await cog.creator_clan(interaction) is None
    asyncio.run(go())
