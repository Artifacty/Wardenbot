import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from wardenbot.cogs.operations import DateChoices, DatePicker, Draft, ExactTimeModal, ScheduleEditor
from wardenbot.timeutil import tz_from_name
from test_release_operations import case, ADMIN, NOW
from wardenbot import repo, operation_service as ops


def interaction(uid=10):
    return SimpleNamespace(user=SimpleNamespace(id=uid), message=SimpleNamespace(),
        response=SimpleNamespace(is_done=lambda: False, edit_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


def test_time_picker_saves_local_date_and_rejects_past(monkeypatch):
    async def go():
        fixed = datetime(2026, 11, 25, 12, tzinfo=UTC)
        monkeypatch.setattr('wardenbot.cogs.operations.utcnow', lambda: fixed)
        draft = Draft(1, 10, 'defense', 'ru', 'Europe/Moscow', fixed)
        cog = SimpleNamespace(show_preview=AsyncMock())
        picker = DatePicker(cog, draft, 'ru', fixed_day=1, hour=20, minute=30)
        assert len(picker.children) == 4
        assert picker.day.isoformat() == '2026-11-26'
        await picker.confirm(interaction())
        assert draft.start_at == datetime(2026, 11, 26, 17, 30, tzinfo=UTC)
        cog.show_preview.assert_awaited_once()
        past = DatePicker(cog, draft, 'ru', fixed_day=0, hour=14)
        reply = AsyncMock()
        monkeypatch.setattr('wardenbot.cogs.operations.reply', reply)
        await past.confirm(interaction())
        assert reply.await_args.args[1].startswith('Это время уже прошло')
        assert cog.show_preview.await_count == 1
    asyncio.run(go())


def test_quick_time_and_exact_minutes(monkeypatch):
    async def go():
        fixed = datetime(2026, 11, 25, 12, tzinfo=UTC)
        monkeypatch.setattr('wardenbot.cogs.operations.utcnow', lambda: fixed)
        draft = Draft(1, 10, 'defense', 'ru', 'Europe/Moscow', fixed)
        cog = SimpleNamespace(show_preview=AsyncMock())
        choices = DateChoices(cog, draft, 'ru')
        assert len(choices.children) == 7
        await choices.children[0].callback(interaction())
        assert draft.start_at == fixed + timedelta(minutes=30)
        picker = DatePicker(cog, draft, 'ru', fixed_day=1)
        form = ExactTimeModal(picker)
        form.clock._value = '20:10'
        await form.on_submit(interaction())
        assert draft.start_at.astimezone(tz_from_name(draft.tz_name)).strftime('%H:%M') == '20:10'
        assert len(DatePicker(cog, draft, 'ru').children) == 5
        assert len(DatePicker(cog, draft, 'ru', page=1).children[0].options) == 14
    asyncio.run(go())


def test_published_operation_uses_picker_and_saves_new_start(monkeypatch):
    async def go():
        monkeypatch.setattr('wardenbot.cogs.operations.utcnow', lambda: NOW)
        db, oid, _, _ = await case()
        op = await db.read(repo.op_get, oid)
        async def mutate(interaction, op_id, fn, *args):
            return await db.write(fn, ADMIN, op_id, *args)
        cog = SimpleNamespace(bot=SimpleNamespace(db=db), mutate=mutate, request_refresh=MagicMock())
        editor = ScheduleEditor(cog, oid, op['revision'])
        draft = Draft(1, 99, 'offense', 'ru', 'Europe/Moscow', NOW)
        picker = DatePicker(editor, draft, 'ru', fixed_day=1, hour=20, minute=30)
        await picker.confirm(interaction(99))
        new = await db.read(repo.op_get, oid)
        assert new['start_at'] != op['start_at']
        assert new['revision'] == op['revision'] + 1
        assert new['timezone'] == op['timezone']
        db.close()
    asyncio.run(go())
