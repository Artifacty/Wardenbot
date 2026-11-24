"""Readable voice names retain durable, independent channel identities."""
import asyncio

import discord
import pytest

from test_community import discord_fixture
from wardenbot.cogs.community import CommunityCog
from wardenbot.db import Database


@pytest.mark.parametrize('source,role', [('operation', 'подготовка'), ('operation', 'Войс 1'),
                                        ('operation', 'Войс 2'), ('group', 'Сбор')])
def test_same_titles_keep_separate_rooms_after_restart(tmp_path, source, role):
    async def go():
        path = tmp_path / 'names.db'
        bot, guild, _, channels, _ = await discord_fixture(path)
        cog = CommunityCog(bot)
        key = 'operation_rooms' if source == 'operation' else 'small_groups'
        category = await cog.category(guild, key, 'Комнаты')
        first = await cog.room(guild, source, 1, role, 'Оборона', [10], category)
        second = await cog.room(guild, source, 2, role, 'Оборона', [20], category)
        assert first.id != second.id
        assert first.name == second.name == f'Оборона {role}'
        bot.db.close()
        bot.db = Database(str(path))
        cog = CommunityCog(bot)
        assert (await cog.room(guild, source, 1, role, 'Оборона', [10], category)).id == first.id
        assert (await cog.room(guild, source, 2, role, 'Оборона', [20], category)).id == second.id
        await cog.delete_rooms(guild, source, 1)
        assert first.id not in channels and second.id in channels
        assert guild.create_voice_channel.await_count == 2
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('source,role', [('operation', 'Войс 1'), ('group', 'Сбор')])
def test_legacy_voice_is_renamed_in_place(source, role):
    async def go():
        bot, guild, _, _, _ = await discord_fixture()
        cog = CommunityCog(bot)
        category = await cog.category(guild, 'rooms', 'Комнаты')
        old = await guild.create_voice_channel(f'Старое название {role} · 42', category=category)
        result = await cog.room(guild, source, 42, role, 'Новое название', [10], category)
        assert result.id == old.id and result.name == f'Новое название {role}'
        guild.create_voice_channel.assert_awaited_once()
        old.delete.assert_not_awaited()
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('failure', ['receipt', 'rename'])
def test_creation_failure_recovers_before_exposing_room(tmp_path, failure):
    async def go():
        path = tmp_path / 'retry.db'
        bot, guild, players, channels, _ = await discord_fixture(path)
        cog = CommunityCog(bot)
        category = await cog.category(guild, 'small_groups', 'Небольшие группы')
        create = guild.create_voice_channel.side_effect
        async def interrupted(*args, **kwargs):
            channel = await create(*args, **kwargs)
            if failure == 'receipt':
                raise RuntimeError('Lost creation response')
            channel.edit.side_effect = RuntimeError('Rename failed')
            return channel
        guild.create_voice_channel.side_effect = interrupted
        with pytest.raises(RuntimeError):
            await cog.room(guild, 'group', 42, 'Сбор', 'Дуо', [10], category)
        voice = next(ch for ch in channels.values() if isinstance(ch, discord.VoiceChannel))
        assert players[10] not in voice.overwrites
        assert voice.overwrites[guild.default_role].view_channel is False
        async def edit(**changes):
            for key, value in changes.items():
                setattr(voice, key, value)
            return voice
        voice.edit.side_effect = edit
        bot.db.close()
        bot.db = Database(str(path))
        restored = await CommunityCog(bot).room(guild, 'group', 42, 'Сбор', 'Дуо', [10], category)
        assert restored.id == voice.id and restored.name == 'Дуо Сбор'
        assert players[10] in restored.overwrites
        guild.create_voice_channel.assert_awaited_once()
        bot.db.close()
    asyncio.run(go())
