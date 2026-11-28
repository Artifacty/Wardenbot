import asyncio
import json
import time
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from PIL import Image

from wardenbot import war_map as data, repo
from wardenbot.bot import WardenBot
from wardenbot.config import Config
from wardenbot.db import Database
from wardenbot.cogs.war_map import WarMapCog, MapView, map_embed
from wardenbot.war_map_render import render, region_info, region_name, REGIONS, point, sector_polygons


def snapshot():
    return {'war': {'warId': 'war-1', 'warNumber': 1, 'conquestStartTime': 1}, 'shard': 'able',
            'checked_at': time.time(), 'regions': {'DeadLandsHex': {
                'static': {'mapItems': [], 'mapTextItems': [
                    {'x': .5, 'y': .5, 'text': 'Abandoned Ward', 'mapMarkerType': 'Major'}]},
                'dynamic': {'mapItems': [{'x': .5, 'y': .5, 'teamId': 'WARDENS', 'iconType': 56, 'flags': 1}]}}}}


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload, self.status, self.headers = payload, status, headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        body = json.dumps(self.payload).encode()
        # Real HTTP bodies may arrive in arbitrarily small chunks.
        for n in range(0, len(body), 17):
            yield body[n:n+17]


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, **kwargs):
        path = url.split('/worldconquest')[-1]
        self.calls.append((path, kwargs))
        result = self.responses[path]
        return result.pop(0) if isinstance(result, list) else result


def routes():
    s = snapshot()
    return {'/war': Response(s['war']), '/maps': Response(['DeadLandsHex', 'HomeRegionW']),
            '/maps/DeadLandsHex/static': Response(s['regions']['DeadLandsHex']['static']),
            '/maps/DeadLandsHex/dynamic/public': Response(s['regions']['DeadLandsHex']['dynamic'])}


def test_etag_and_cache_survive_restart(tmp_path):
    async def go():
        path = str(tmp_path / 'cache.db')
        db = Database(path)
        session = Session({'/war': Response(snapshot()['war'], headers={'ETag': 'v1', 'Cache-Control': 'max-age=0'})})
        await data.WarAPI(db, session, 'able').fetch('/war', validate=data.validate_war)
        db.close()
        db = Database(path)
        session = Session({'/war': Response(status=304, headers={'Cache-Control': 'max-age=3600'})})
        api = data.WarAPI(db, session, 'able')
        assert (await api.fetch('/war'))['warId'] == 'war-1'
        assert session.calls[0][1]['headers'] == {'If-None-Match': 'v1'}
        await api.fetch('/war')
        assert len(session.calls) == 1
        db.close()
    asyncio.run(go())


def test_startup_restores_region_buttons_and_closes_session(tmp_path):
    async def go():
        path = str(tmp_path / 'restart.db')
        db = Database(path)
        stored = snapshot()
        await db.write(data.snapshot_put, 'able', stored)
        db.close()
        bot = WardenBot(Config(token='test'), Database(path))
        async with bot:
            cog = WarMapCog(bot)
            await bot.add_cog(cog)
            assert cog.snapshot == stored
            assert any(isinstance(view, MapView) and view.is_persistent() for view in bot.persistent_views)
            session = cog.session
            task = cog.tick.get_task()
            await bot.remove_cog('WarMapCog')
            assert session.closed and task.done()
        bot.db.close()
    asyncio.run(go())


def test_outage_preserves_snapshot_and_new_war_refreshes_static(tmp_path):
    async def go():
        db = Database(str(tmp_path / 'state.db'))
        session = Session(routes())
        api = data.WarAPI(db, session, 'able')
        first = await api.refresh()
        assert list(first['regions']) == ['DeadLandsHex']
        session.responses['/war'] = Response({'warId': 'war-2', 'warNumber': 2})
        session.responses['/maps/DeadLandsHex/dynamic/public'] = Response(status=503)
        with pytest.raises(data.WarAPIError):
            await api.refresh()
        assert await db.read(data.snapshot_get, 'able') == first
        assert len([p for p, _ in session.calls if p.endswith('/static')]) == 2
        session.responses['/maps/DeadLandsHex/dynamic/public'] = routes()['/maps/DeadLandsHex/dynamic/public']
        second = await api.refresh()
        assert second['war']['warId'] == 'war-2'
        assert db.conn.execute("SELECT COUNT(*) FROM war_map_cache WHERE scope='war-1'").fetchone()[0] == 0
        db.close()
    asyncio.run(go())


def test_rate_limit_and_invalid_payload_are_not_cached():
    async def go():
        db = Database(':memory:')
        session = Session({'/war': Response(status=429, headers={'Retry-After': '900'})})
        api = data.WarAPI(db, session, 'able')
        for _ in range(2):
            with pytest.raises(data.WarAPIError):
                await api.fetch('/war')
        assert len(session.calls) == 1 and api.blocked_until >= time.time() + 890
        api.blocked_until = 0
        session.responses['/war'] = Response({'bad': 'payload'})
        with pytest.raises(data.WarAPIError):
            await api.fetch('/war', validate=data.validate_war)
        assert await db.read(data.cache_get, 'able', '', '/war') is None
        db.close()
    asyncio.run(go())


def test_renderer_and_region_menu_cover_all_regions():
    async def go():
        s = snapshot()
        for name in REGIONS:
            s['regions'][name] = deepcopy(s['regions']['DeadLandsHex'])
        view = MapView(None, s)
        assert view.is_persistent()
        assert len(view.children) == 3
        assert all(len(item.options) <= 25 for item in view.children)
        assert {option.value for item in view.children for option in item.options} == set(REGIONS)
        assert region_info('MarbanHollow') == region_info('MarbanHollowHex')
        assert region_name('MooringCountyHex') == 'The Moors'
        assert point(region_info('DeadLandsHex'), .5, .5)[0] == pytest.approx(2048)
        for name in (None, 'DeadLandsHex'):
            png = render(s, name)
            assert len(png) < 8 * 1024 * 1024
            with Image.open(BytesIO(png)) as image:
                assert image.format == 'PNG' and image.width >= 1280
        old = deepcopy(s)
        old['checked_at'] -= 1000
        assert 'задерживается' in map_embed('able', old, None, 'test').description
    asyncio.run(go())


def test_sector_fill_divides_one_hex_by_base_and_clips_to_border():
    items = [
        {'x': .25, 'y': .5, 'teamId': 'WARDENS', 'iconType': 56},
        {'x': .75, 'y': .5, 'teamId': 'COLONIALS', 'iconType': 45},
    ]
    sectors = sector_polygons(items, 100, 100)
    assert len(sectors) == 2
    assert all(0 <= x <= 100 and 0 <= y <= 100
               for polygon, _ in sectors for x, y in polygon)
    assert max(x for x, _ in sectors[0][0]) == pytest.approx(50)
    assert min(x for x, _ in sectors[1][0]) == pytest.approx(50)
    assert sector_polygons([], 100, 100) == []
    s = snapshot()
    s['regions']['DeadLandsHex']['dynamic']['mapItems'] = items
    with Image.open(BytesIO(render(s, 'DeadLandsHex'))) as image:
        left = image.getpixel((320, 82+554))
        right = image.getpixel((960, 82+554))
        assert left[2] > right[2]  # blue wash on the Warden side
        assert right[1] > left[1]  # green wash on the Colonial side


def test_publish_recovers_lost_id_after_restart_and_deleted_message(tmp_path, monkeypatch):
    async def go():
        path = str(tmp_path / 'bot.db')
        bot = WardenBot(Config(token='test'), Database(path))
        bot._connection.user = SimpleNamespace(id=7)
        guild = SimpleNamespace(id=1)
        channel = MagicMock(spec=discord.TextChannel)
        messages = []

        async def history(**kwargs):
            for message in reversed(messages):
                yield message

        async def send(**kwargs):
            message = SimpleNamespace(id=100+len(messages), author=bot.user, embeds=[kwargs['embed']],
                                      pinned=True, jump_url='https://discord.com/channels/1/2/100')
            async def edit(**changes):
                message.embeds = [changes['embed']]
                return message
            message.edit = AsyncMock(side_effect=edit)
            messages.append(message)
            return message

        async def fetch(mid):
            for m in messages:
                if m.id == mid:
                    return m
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not found'), 'Unknown message')

        channel.send = AsyncMock(side_effect=send)
        channel.fetch_message = AsyncMock(side_effect=fetch)
        channel.history = history
        monkeypatch.setattr('wardenbot.cogs.war_map.get_text_channel', lambda *args: channel)
        cog = WarMapCog(bot)
        cog.snapshot = snapshot()
        await bot.db.write(data.snapshot_put, 'able', cog.snapshot)
        cog.picture = b'fake png'
        bot.set_cfg = AsyncMock(side_effect=RuntimeError('lost DB acknowledgement'))
        with pytest.raises(RuntimeError):
            await cog.publish(guild)
        assert channel.send.await_count == 1
        bot.db.close()
        bot = WardenBot(Config(token='test'), Database(path))
        bot._connection.user = SimpleNamespace(id=7)
        await bot._load_cfg()
        cog = WarMapCog(bot)
        cog.snapshot = await bot.db.read(data.snapshot_get, 'able')
        cog.picture = b'fake png'
        await cog.publish(guild)
        assert channel.send.await_count == 1
        assert bot.cfg(1)['msg.war_map'] == '100'
        messages.clear()
        await cog.publish(guild)
        assert channel.send.await_count == 2
        bot.db.close()
    asyncio.run(go())


@pytest.mark.parametrize('admitted', [False, True])
def test_region_checks_access_and_replies_privately(monkeypatch, admitted):
    async def go():
        bot = WardenBot(Config(token='test'), Database(':memory:'))
        cog = WarMapCog(bot)
        cog.snapshot = snapshot()
        guild = SimpleNamespace(id=1)
        member = MagicMock(spec=discord.Member)
        member.id, member.guild, member.roles = 12, guild, []
        member.guild_permissions = discord.Permissions.none()
        channel = MagicMock(spec=discord.TextChannel)
        channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        monkeypatch.setattr('wardenbot.cogs.war_map.get_text_channel', lambda *args: channel)
        renderer = MagicMock(return_value=b'png')
        monkeypatch.setattr('wardenbot.cogs.war_map.render', renderer)
        from datetime import datetime, UTC
        if admitted:
            await bot.db.write(repo.user_update, 12, datetime.now(UTC), status='admitted')
        i = SimpleNamespace(guild=guild, user=member, response=SimpleNamespace(is_done=lambda: True),
                            followup=SimpleNamespace(send=AsyncMock()))
        await cog.show_region(i, 'DeadLandsHex')
        assert i.followup.send.call_args.kwargs['ephemeral'] is True
        assert renderer.call_count == int(admitted)
        bot.db.close()
    asyncio.run(go())
