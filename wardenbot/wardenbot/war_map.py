"""Public Foxhole War API, cached in SQLite independently of the Discord UI."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from email.utils import parsedate_to_datetime

import aiohttp

BASE_URLS = {
    "able": "https://war-service-live.foxholeservices.com/api/worldconquest",
    "baker": "https://war-service-live-2.foxholeservices.com/api/worldconquest",
    "charlie": "https://war-service-live-3.foxholeservices.com/api/worldconquest",
}
INTERVAL = 300


class WarAPIError(RuntimeError):
    pass


def cache_get(conn, shard, scope, path):
    row = conn.execute("SELECT * FROM war_map_cache WHERE shard=? AND scope=? AND path=?",
                       (shard, scope, path)).fetchone()
    return dict(row) if row else None


def cache_put(conn, shard, scope, path, payload, etag, expires_at):
    conn.execute("INSERT OR REPLACE INTO war_map_cache VALUES (?,?,?,?,?,?)",
                 (shard, scope, path, json.dumps(payload), etag, expires_at))


def snapshot_get(conn, shard):
    row = conn.execute("SELECT payload FROM war_map_snapshots WHERE shard=?", (shard,)).fetchone()
    return json.loads(row[0]) if row else None


def snapshot_put(conn, shard, snapshot):
    conn.execute("INSERT OR REPLACE INTO war_map_snapshots VALUES (?,?)", (shard, json.dumps(snapshot)))
    # Static data is scoped to a war; discard caches from earlier wars after a successful refresh.
    conn.execute("DELETE FROM war_map_cache WHERE shard=? AND scope NOT IN ('',?)",
                 (shard, snapshot['war']['warId']))


def validate_map(data):
    if not isinstance(data, dict) or not isinstance(data.get('mapItems'), list):
        raise WarAPIError('Некорректные данные карты')
    for key in ('mapItems', 'mapTextItems'):
        items = data.get(key, [])
        if not isinstance(items, list) or len(items) > 10000:
            raise WarAPIError('Некорректный список объектов')
        for item in items:
            if not isinstance(item, dict):
                raise WarAPIError('Некорректный объект карты')
            if key == 'mapItems' and (not isinstance(item.get('iconType'), int)
                                     or not isinstance(item.get('flags', 0), int)
                                     or not isinstance(item.get('teamId', 'NONE'), str)):
                raise WarAPIError('Некорректный тип объекта карты')
            for axis in ('x', 'y'):
                value = item.get(axis)
                if not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise WarAPIError('Некорректные координаты объекта')
    return data


def validate_war(data):
    if not isinstance(data, dict) or not isinstance(data.get('warId'), str) or not data['warId'] \
            or not isinstance(data.get('warNumber'), int):
        raise WarAPIError('Некорректные данные войны')
    return data


def validate_names(data):
    if not isinstance(data, list) or len(data) > 100 or any(
        not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}', name) for name in data
    ):
        raise WarAPIError('Некорректный список регионов')
    return data


class WarAPI:
    def __init__(self, db, session, shard):
        self.db, self.session, self.shard = db, session, shard
        self.semaphore = asyncio.Semaphore(3)
        self.blocked_until = 0.0

    async def fetch(self, path, scope='', *, static=False, validate=lambda data: data):
        cached = await self.db.read(cache_get, self.shard, scope, path)
        if cached and (static or cached['expires_at'] > time.time()):
            return validate(json.loads(cached['payload']))
        async with self.semaphore:
            if time.time() < self.blocked_until:
                raise WarAPIError('API временно ограничил запросы')
            headers = {'If-None-Match': cached['etag']} if cached and cached['etag'] else {}
            try:
                async with self.session.get(BASE_URLS[self.shard] + path, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status == 429:
                        retry = response.headers.get('Retry-After', '300')
                        try:
                            delay = float(retry)
                        except ValueError:
                            try:
                                delay = parsedate_to_datetime(retry).timestamp() - time.time()
                            except (ValueError, TypeError, OverflowError):
                                delay = INTERVAL
                        self.blocked_until = time.time() + max(INTERVAL, delay)
                        raise WarAPIError('API временно ограничил запросы (HTTP 429)')
                    if response.status == 304 and cached:
                        payload = json.loads(cached['payload'])
                    elif response.status == 200:
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > 4 * 1024 * 1024:
                                raise WarAPIError('Ответ API слишком большой')
                        payload = json.loads(body)
                    else:
                        raise WarAPIError(f'War API: HTTP {response.status}')
                    payload = validate(payload)
                    match = re.search(r'max-age=(\d+)', response.headers.get('Cache-Control', ''))
                    age = int(match[1]) if match else 0
                    try:
                        age = max(0, age - int(response.headers.get('Age', '0')))
                    except ValueError:
                        pass
                    etag = response.headers.get('ETag') or (cached['etag'] if cached else None)
                    await self.db.write(cache_put, self.shard, scope, path, payload, etag, time.time() + age)
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise WarAPIError('Не удалось получить данные War API') from exc

    async def refresh(self):
        war = await self.fetch('/war', validate=validate_war)
        names = await self.fetch('/maps', scope=war['warId'], validate=validate_names)
        names = sorted(set(name for name in names if not name.startswith('HomeRegion')))

        async def region(name):
            static = await self.fetch(f'/maps/{name}/static', war['warId'], static=True, validate=validate_map)
            dynamic = await self.fetch(f'/maps/{name}/dynamic/public', war['warId'], validate=validate_map)
            return name, {'static': static, 'dynamic': dynamic}

        # TaskGroup cancels remaining requests on failure. Never mark a partial refresh as current.
        try:
            async with asyncio.TaskGroup() as group:
                jobs = [group.create_task(region(name)) for name in names]
        except ExceptionGroup as exc:
            raise WarAPIError('Не все регионы ответили; сохранена предыдущая карта') from exc
        # Revalidate the war identity at the end of a long update, respecting API cache lifetimes.
        after = await self.fetch('/war', validate=validate_war)
        if after['warId'] != war['warId']:
            raise WarAPIError('Началась новая война; карта обновится при следующей проверке')
        snapshot = {'war': after, 'regions': dict(job.result() for job in jobs),
                    'checked_at': time.time(), 'shard': self.shard}
        await self.db.write(snapshot_put, self.shard, snapshot)
        return snapshot
