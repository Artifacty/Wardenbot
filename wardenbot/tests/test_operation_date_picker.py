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
        assert 'МСК' in picker.prompt() and 'Europe/Moscow' not in picker.prompt()
        assert len(picker.children) == 5
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
        assert len(choices.children) == 6
        assert 'Позже' not in [button.label for button in choices.children]
        await choices.children[0].callback(interaction())
        assert draft.start_at == fixed + timedelta(minutes=30)
        picker = DatePicker(cog, draft, 'ru', fixed_day=1)
        form = ExactTimeModal(picker)
        form.clock._value = '20:10'
        await form.on_submit(interaction())
        assert draft.start_at.astimezone(tz_from_name(draft.tz_name)).strftime('%H:%M') == '20:10'
        assert len(DatePicker(cog, draft, 'ru').children) == 6
        assert len(DatePicker(cog, draft, 'ru', page=1).children[0].options) == 14
    asyncio.run(go())


def test_picker_defaults_follow_visible_selection(monkeypatch):
    async def go():
        fixed = datetime(2026, 11, 25, 12, tzinfo=UTC)
        monkeypatch.setattr('wardenbot.cogs.operations.utcnow', lambda: fixed)
        draft = Draft(1, 10, 'defense', 'ru', 'Europe/Moscow', fixed)
        cog = SimpleNamespace(show_preview=AsyncMock())
        picker = DatePicker(cog, draft, 'ru')
        i = interaction()
        picker.day_select._values = ['2026-12-08']
        await picker.day_select.callback(i)
        picker.hour_select._values = ['1']
        await picker.hour_select.callback(i)
        picker.minute_select._values = ['30']
        await picker.minute_select.callback(i)
        assert '08.12.2026, 01:30' in i.response.edit_message.call_args.kwargs['content']
        for selector, expected in ((picker.day_select, '2026-12-08'),
                                   (picker.hour_select, '1'), (picker.minute_select, '30')):
            assert [option.value for option in selector.options if option.default] == [expected]
        await picker.confirm(i)
        assert draft.start_at.astimezone(tz_from_name(draft.tz_name)).strftime('%d.%m.%Y %H:%M') == '08.12.2026 01:30'
    asyncio.run(go())


def test_later_button_pages_inside_date_picker(monkeypatch):
    async def go():
        fixed = datetime(2026, 11, 25, 12, tzinfo=UTC)
        monkeypatch.setattr('wardenbot.cogs.operations.utcnow', lambda: fixed)
        draft = Draft(1, 10, 'defense', 'ru', 'Europe/Moscow', fixed)
        cog = SimpleNamespace(show_preview=AsyncMock())
        first = DatePicker(cog, draft, 'ru', hour=21, minute=30)
        later = next(button for button in first.children if isinstance(button, discord.ui.Button)
                     and button.label == 'Позже')
        i = interaction()
        await later.callback(i)
        second = i.response.edit_message.call_args.kwargs['view']
        assert second.page == 1
        assert second.day.isoformat() == '2026-12-09'
        assert (second.hour, second.minute) == (21, 30)
        assert '09.12.2026, 21:30' in i.response.edit_message.call_args.kwargs['content']
        assert [option.value for option in second.day_select.options if option.default] == ['2026-12-09']
        earlier = next(button for button in second.children if isinstance(button, discord.ui.Button)
                       and button.label == 'Раньше')
        await earlier.callback(i)
        back = i.response.edit_message.call_args.kwargs['view']
        assert back.page == 0 and back.day.isoformat() == '2026-11-25'
        assert (back.hour, back.minute) == (21, 30)
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
