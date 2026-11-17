import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from wardenbot import repo, access, notifications, operation_service as ops
from wardenbot.db import Database
from wardenbot.slots import SlotSpec, SupplySpec
from wardenbot.timeutil import to_iso
from wardenbot.cogs.operation_edit import FIELDS

NOW=datetime(2026,11,17,12,tzinfo=UTC)
ADMIN=ops.Actor(1,99,True)
PLAYER=ops.Actor(1,10,False,frozenset({'recruit'}))

async def case(path=':memory:'):
    db=Database(str(path))
    for uid in (10,20,99):
        await db.write(repo.user_update,uid,NOW,status='admitted',rank='recruit')
    oid=await db.write(lambda c:repo.op_create(c,guild_id=1,organizer_id=99,title='Operation',region='R',
        type='offense',language='ru',start_at=NOW+timedelta(hours=2),duration_min=60,requirements='Old',
        briefing_public='Public',briefing_closed='NEVER PUBLIC',slots=[SlotSpec('Inf',2)],
        supply=[SupplySpec('BMats',100)],now=NOW))
    await db.write(lambda c:repo.op_update(c,oid,thread_id=123))
    return db,oid,(await db.read(repo.op_slots,oid))[0]['id'],(await db.read(repo.op_supply,oid))[0]['id']

async def fields(db,oid):
    row=await db.read(repo.op_get,oid)
    return {k:row[k] for k in FIELDS}


def test_edit_preserves_members_and_pledges_and_rejects_conflicts():
    async def go():
        db,oid,slot,item=await case()
        await db.write(ops.signup,PLAYER,oid,slot,NOW)
        await db.write(ops.pledge,PLAYER,oid,item,50,NOW)
        data=await fields(db,oid)
        data['title']='Edited'
        for slots,supply in [([SlotSpec('Other',1)],[SupplySpec('BMats',100)]),
                             ([SlotSpec('Inf',2)],[SupplySpec('BMats',40)])]:
            with pytest.raises(ops.OperationError):
                await db.write(ops.edit,ADMIN,oid,1,data,slots,supply,NOW)
            assert (await db.read(repo.op_get,oid))['title']=='Operation'
        await db.write(ops.edit,ADMIN,oid,1,data,[SlotSpec('Inf',3)],[SupplySpec('BMats',120)],NOW)
        assert (await db.read(repo.op_slots,oid))[0]['id']==slot
        assert (await db.read(repo.op_supply,oid))[0]['id']==item
        assert (await db.read(repo.op_signups,oid))[0]['user_id']==10
        assert (await db.read(ops.supply_totals,oid))[0]['promised']==50
        with pytest.raises(ops.OperationError,match='уже изменена'):
            await db.write(ops.edit,ADMIN,oid,1,data,[SlotSpec('Inf',3)],[],NOW)
        db.close()
    asyncio.run(go())


def test_concurrent_capacity_and_closed_recruiting():
    async def go():
        db,oid,slot,_=await case()
        await db.write(lambda c:c.execute('UPDATE op_slots SET capacity=1 WHERE id=?',(slot,)))
        results=await asyncio.gather(db.write(ops.signup,PLAYER,oid,slot,NOW),
            db.write(ops.signup,ops.Actor(1,20),oid,slot,NOW))
        assert sorted(r.status for r in results)==['full','ok']
        await db.write(ops.set_recruiting,ADMIN,oid,False,NOW)
        assert (await db.write(ops.signup,PLAYER,oid,slot,NOW)).status=='closed'
        await db.write(ops.set_recruiting,ADMIN,oid,True,NOW)
        assert len(await db.read(repo.op_signups,oid))==1
        db.close()
    asyncio.run(go())


def test_delivery_counts_once_and_finish_is_explicit():
    async def go():
        db,oid,slot,item=await case()
        await db.write(ops.signup,PLAYER,oid,slot,NOW)
        await db.write(repo.confirm_attendance,oid,10,True)
        await db.write(ops.pledge,PLAYER,oid,item,70,NOW)
        await db.write(ops.deliver,ADMIN,oid,item,10,30,NOW)
        totals=(await db.read(ops.supply_totals,oid))[0]
        assert totals['promised']==40 and totals['prepared']==30
        with pytest.raises(ops.OperationError):
            await db.write(ops.deliver,PLAYER,oid,item,10,70,NOW)
        with pytest.raises(ops.OperationError):
            await db.write(ops.pledge,PLAYER,oid,item,0,NOW)
        await db.write(notifications.schedule,NOW+timedelta(days=2))
        assert (await db.read(repo.op_get,oid))['status']=='running'
        assert await db.read(repo.attended_count,10)==0
        assert await db.write(ops.finish,ADMIN,oid,NOW+timedelta(days=2))==[10]
        assert await db.read(repo.attended_count,10)==1
        with pytest.raises(ops.OperationError):
            await db.write(ops.finish,ADMIN,oid,NOW+timedelta(days=2))
        assert await db.read(repo.attended_count,10)==1
        db.close()
    asyncio.run(go())


def test_reschedule_invalidates_old_reminders_and_survives_restart(tmp_path):
    async def go():
        path=tmp_path/'db'
        db,oid,_,_=await case(path)
        await db.write(notifications.schedule,NOW+timedelta(minutes=70))
        assert [j['kind'] for j in await db.read(notifications.pending)]==['remind60']
        data=await fields(db,oid)
        data['start_at']=to_iso(NOW+timedelta(days=1))
        await db.write(ops.edit,ADMIN,oid,1,data,[SlotSpec('Inf',2)],[SupplySpec('BMats',100)],NOW)
        db.close(); db=Database(str(path))
        assert [j['kind'] for j in await db.read(notifications.pending)]==['updated']
        await db.write(notifications.schedule,NOW+timedelta(hours=23,minutes=45))
        jobs=await db.read(notifications.pending)
        assert [j['kind'] for j in jobs]==['updated','remind15']
        for job in jobs:
            assert 'NEVER PUBLIC' not in job['content']
            await db.write(notifications.ack,job['id'],700+job['id'])
        await db.write(notifications.schedule,NOW+timedelta(hours=23,minutes=46))
        assert not await db.read(notifications.pending)
        db.close()
    asyncio.run(go())


def test_revocation_and_new_war_block_stale_actions(tmp_path):
    async def go():
        db,oid,slot,item=await case(tmp_path/'db')
        await db.write(ops.signup,PLAYER,oid,slot,NOW)
        await db.write(ops.pledge,PLAYER,oid,item,50,NOW)
        await db.write(access.revoke,1,10,99,'Review',NOW)
        with pytest.raises(ops.OperationError):
            await db.write(ops.signup,PLAYER,oid,slot,NOW)
        with pytest.raises(ops.OperationError):
            await db.write(ops.pledge,PLAYER,oid,item,10,NOW)
        assert await db.read(repo.op_signups,oid)==[]
        await db.write(repo.user_update,10,NOW,status='admitted')
        await db.write(access.new_war,1,123,[10,20],99,NOW)
        assert (await db.read(repo.user_get,10))['status']=='new'
        assert (await db.read(repo.user_get,99))['status']=='admitted'
        with pytest.raises(ValueError):
            await db.write(access.new_war,1,123,[10,20],99,NOW)
        with pytest.raises(ops.OperationError):
            await db.write(ops.set_recruiting,ops.Actor(2,99,True),oid,False,NOW)
        db.close()
    asyncio.run(go())


async def stream(items):
    for x in items: yield x


def test_notification_ack_loss_recovers_without_duplicate(tmp_path,monkeypatch):
    async def go():
        path=tmp_path/'db'
        db,oid,slot,_=await case(path)
        await db.write(ops.signup,PLAYER,oid,slot,NOW)
        await db.write(notifications.schedule,NOW+timedelta(minutes=61))
        job=(await db.read(notifications.pending))[0]
        messages=[]
        bot=SimpleNamespace(db=db,user=SimpleNamespace(id=999))
        async def send(**kw):
            msg=SimpleNamespace(id=55,author=bot.user,embeds=[kw['embed']])
            messages.append(msg)
            return msg
        thread=SimpleNamespace(history=lambda **kw:stream(messages),send=AsyncMock(side_effect=send))
        real=notifications.ack
        def crash(*args): raise RuntimeError('crash')
        monkeypatch.setattr(notifications,'ack',crash)
        with pytest.raises(RuntimeError):
            await notifications.send(bot,job,thread)
        db.close(); bot.db=Database(str(path))
        monkeypatch.setattr(notifications,'ack',real)
        await notifications.send(bot,job,thread)
        thread.send.assert_awaited_once()
        assert not await bot.db.read(notifications.pending)
        bot.db.close()
    asyncio.run(go())


def test_cancellation_has_reason_and_never_counts_delivery():
    async def go():
        db,oid,_,item=await case()
        await db.write(ops.pledge,PLAYER,oid,item,50,NOW)
        await db.write(ops.deliver,ADMIN,oid,item,10,50,NOW)
        with pytest.raises(ops.OperationError):
            await db.write(ops.cancel,ADMIN,oid,' ',NOW)
        await db.write(ops.cancel,ADMIN,oid,'Перенос планов',NOW)
        assert await db.read(repo.attended_count,10)==0
        assert (await db.read(repo.op_get,oid))['cancel_reason']=='Перенос планов'
        assert [j['kind'] for j in await db.read(notifications.pending)]==['cancelled']
        db.close()
    asyncio.run(go())


def test_competing_edits_have_one_winner():
    async def go():
        db,oid,_,_=await case()
        data=await fields(db,oid)
        results=await asyncio.gather(*(db.write(ops.edit,ADMIN,oid,1,dict(data,title=title),[SlotSpec('Inf',2)],[SupplySpec('BMats',100)],NOW)
                                       for title in ('First','Second')),return_exceptions=True)
        assert sum(isinstance(r,ops.OperationError) for r in results)==1
        assert (await db.read(repo.op_get,oid))['revision']==2
        db.close()
    asyncio.run(go())


def test_upgrade_keeps_previously_delivered_reminders(tmp_path,monkeypatch):
    import wardenbot.db as storage
    path=str(tmp_path/'upgrade.db')
    migrations=storage.MIGRATIONS
    monkeypatch.setattr(storage,'MIGRATIONS',migrations[:6])
    conn=storage.connect(path)
    oid=repo.op_create(conn,guild_id=1,organizer_id=99,title='Old',region='',type='offense',language='ru',start_at=NOW+timedelta(minutes=5),duration_min=60,requirements='',briefing_public='',briefing_closed='',slots=[SlotSpec('Inf',2)],supply=[],now=NOW)
    conn.execute('UPDATE operations SET reminder60_sent=1,reminder10_sent=1,thread_id=123 WHERE id=?',(oid,))
    conn.close()
    monkeypatch.setattr(storage,'MIGRATIONS',migrations)
    conn=storage.connect(path)
    notifications.schedule(conn,NOW)
    assert notifications.pending(conn)==[]
    conn.close()


def test_dirty_cards_survive_restart_and_old_ack_cannot_drop_update(tmp_path):
    async def go():
        path=tmp_path/'db'
        db,oid,slot,_=await case(path)
        old=(await db.read(repo.op_get,oid))['card_revision']
        await db.write(repo.card_ack,oid,old)
        assert await db.read(repo.dirty_cards)==[]
        await db.write(ops.signup,PLAYER,oid,slot,NOW)
        db.close(); db=Database(str(path))
        await db.write(repo.card_ack,oid,old)
        assert [r['id'] for r in await db.read(repo.dirty_cards)]==[oid]
        new=(await db.read(repo.op_get,oid))['card_revision']
        await db.write(repo.card_ack,oid,new)
        assert await db.read(repo.dirty_cards)==[]
        db.close()
    asyncio.run(go())


def test_manual_start_supersedes_unsent_reminders():
    async def go():
        db,oid,_,_=await case()
        await db.write(notifications.schedule,NOW+timedelta(minutes=61))
        await db.write(ops.start,ADMIN,oid,NOW+timedelta(minutes=62))
        assert [j['kind'] for j in await db.read(notifications.pending)]==['started']
        assert not await db.write(ops.start,ADMIN,oid,NOW+timedelta(minutes=63))
        db.close()
    asyncio.run(go())
