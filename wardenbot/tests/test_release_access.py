import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_discord_layer import fake_member,fake_interaction,make_bot
from wardenbot import repo
from wardenbot.cogs import operations
from wardenbot.cogs.moderation import allowed
from wardenbot.services import utcnow
from wardenbot.slots import SlotSpec


def test_briefing_45_minute_boundary_and_leave(monkeypatch):
    async def go():
        bot=make_bot()
        bot.cfg(1)['role.recruit']='500'
        now=utcnow()
        await bot.db.write(repo.user_update,10,now,status='admitted')
        oid=await bot.db.write(lambda c:repo.op_create(c,guild_id=1,organizer_id=99,title='T',region='',type='offense',language='ru',start_at=now+timedelta(minutes=46),duration_min=60,requirements='',briefing_public='',briefing_closed='secret',slots=[SlotSpec('Inf',1)],supply=[],now=now))
        slot=(await bot.db.read(repo.op_slots,oid))[0]['id']
        await bot.db.write(repo.signup,oid,10,slot,now)
        cog=operations.OperationsCog(bot)
        monkeypatch.setattr(operations,'utcnow',lambda:now)
        early=fake_interaction(fake_member(10,[500]),bot=bot)
        await cog.on_brief(early,oid)
        assert 'embed' not in early.response.send_message.call_args.kwargs
        monkeypatch.setattr(operations,'utcnow',lambda:now+timedelta(minutes=1))
        ready=fake_interaction(fake_member(10,[500]),bot=bot)
        await cog.on_brief(ready,oid)
        assert ready.response.send_message.call_args.kwargs['embed'].description=='secret'
        await bot.db.write(repo.leave,oid,10)
        left=fake_interaction(fake_member(10,[500]),bot=bot)
        await cog.on_brief(left,oid)
        assert 'embed' not in left.response.send_message.call_args.kwargs
        wrong=fake_interaction(fake_member(10,[500]),guild_id=2,bot=bot)
        await cog.on_brief(wrong,oid)
        assert 'embed' not in wrong.response.send_message.call_args.kwargs
        bot.db.close()
    asyncio.run(go())


def test_moderation_hierarchy_including_owner_and_bot():
    bot=SimpleNamespace(user=SimpleNamespace(id=999),cfg=lambda gid:{})
    actor=fake_member(10)
    actor.guild_permissions.administrator=True
    actor.top_role=10
    actor.guild=SimpleNamespace(id=1,owner_id=1,me=SimpleNamespace(top_role=12))
    assert allowed(bot,actor,SimpleNamespace(id=20,top_role=5))
    for target in [SimpleNamespace(id=1,top_role=5),SimpleNamespace(id=999,top_role=5),
                   SimpleNamespace(id=10,top_role=5),SimpleNamespace(id=20,top_role=11),
                   SimpleNamespace(id=20,top_role=13)]:
        assert not allowed(bot,actor,target)
