"""One durable war-map card in the brief channel, with private region previews."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import repo, war_map as data
from ..perms import can_admin, can_staff
from ..services import BLUE, get_text_channel, interaction_errors, reply
from ..war_map_render import region_name, render, unknown_regions

log = logging.getLogger(__name__)


class RegionSelect(discord.ui.Select):
    def __init__(self, cog, names, page):
        super().__init__(custom_id=f'wb:warmap:regions:{page}',
                         placeholder=f'Выбрать регион · {page + 1}',
                         options=[discord.SelectOption(label=region_name(name)[:100], value=name) for name in names])
        self.cog = cog

    @interaction_errors
    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.show_region(interaction, self.values[0])


class MapView(discord.ui.View):
    def __init__(self, cog, snapshot):
        super().__init__(timeout=None)
        names = sorted(snapshot['regions'], key=region_name)
        # Discord allows at most 25 options per select, and 5 component rows.
        for page in range((len(names) + 24) // 25):
            self.add_item(RegionSelect(cog, names[page*25:(page+1)*25], page))


def map_embed(shard, snapshot, error, marker, *, region=None):
    embed = discord.Embed(title=region_name(region) if region else 'Карта войны', color=BLUE)
    if snapshot is None:
        embed.description = f'{shard.title()} · Данные карты пока недоступны.\nБот повторит загрузку автоматически.'
    else:
        war = snapshot['war']
        checked = datetime.fromtimestamp(snapshot['checked_at'], ZoneInfo('Europe/Moscow'))
        phase = ('Сопротивление' if war.get('resistanceStartTime') else
                 'Война завершена' if war.get('conquestEndTime') else
                 'Идёт война' if war.get('conquestStartTime') else 'Ожидание начала войны')
        embed.description = (f"**{shard.title()} · Война №{war['warNumber']}** · {phase}\n"
                             f'Проверено {checked:%d.%m.%Y %H:%M} МСК · обновление каждые 5 минут.\n'
                             'Заливка территорий по ближайшей базе; границы приблизительные.')
        if error or time.time() - snapshot['checked_at'] > data.INTERVAL * 2:
            embed.description += '\n⚠ Обновление задерживается. Показаны последние полученные данные.'
        if not snapshot['regions']:
            embed.description += '\nAPI пока не сообщил об активных регионах.'
        elif not region and (missing := unknown_regions(snapshot)):
            embed.description += '\nНовые регионы доступны отдельно без фона: ' + ', '.join(map(region_name, missing))[:500]
        if snapshot['regions']:
            embed.set_image(url='attachment://war-map.png')
    embed.set_footer(text=f'Foxhole War API · {marker}')
    return embed


class WarMapCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.shard = bot.config.war_map_shard
        self.snapshot = None
        self.error = None
        self.picture = None
        self.session = None
        self.api = None
        self.lock = asyncio.Lock()
        self.render_lock = asyncio.Semaphore(1)
        self.region_cooldowns = {}

    async def cog_load(self):
        self.snapshot = await self.bot.db.read(data.snapshot_get, self.shard)
        if self.snapshot and self.snapshot['regions']:
            self.bot.add_view(MapView(self, self.snapshot))
        if self.bot.config.war_map_enabled:
            self.session = aiohttp.ClientSession(headers={'User-Agent': 'Wardenbot/war-map'})
            self.api = data.WarAPI(self.bot.db, self.session, self.shard)
            self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()
        task = self.tick.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    @tasks.loop(seconds=data.INTERVAL)
    async def tick(self):
        guilds = [g for g in self.bot.guilds if isinstance(get_text_channel(self.bot, g, 'brief'), discord.TextChannel)]
        if not guilds:
            return
        async with self.lock:
            try:
                async with asyncio.timeout(180):
                    self.snapshot = await self.api.refresh()
                self.picture = None
                self.error = None
            except (data.WarAPIError, asyncio.TimeoutError) as exc:
                self.error = str(exc) or 'Истекло время ожидания War API'
                log.warning('War map refresh failed: %s', self.error)
            except Exception:
                self.error = 'Ошибка обновления карты'
                log.exception('War map refresh failed')
            for guild in guilds:
                try:
                    await self._publish(guild)
                except Exception:
                    log.exception('Could not publish war map in guild %s; retry next cycle', guild.id)

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    async def publish(self, guild):
        if not self.bot.config.war_map_enabled:
            return None
        async with self.lock:
            return await self._publish(guild)

    async def _publish(self, guild):
        channel = get_text_channel(self.bot, guild, 'brief')
        if not isinstance(channel, discord.TextChannel):
            return None
        marker = f'wb:war-map:{guild.id}'
        message = None

        def ours(candidate):
            return candidate.author.id == self.bot.user.id and any(
                e.footer.text and e.footer.text.endswith(marker) for e in candidate.embeds)

        saved = self.bot.cfg(guild.id).get('msg.war_map', '')
        if saved.isdigit():
            try:
                candidate = await channel.fetch_message(int(saved))
                if ours(candidate):
                    message = candidate
            except discord.NotFound:
                pass
        if message is None:
            # Recover a send that succeeded before saving its ID, including after a restart.
            async for candidate in channel.history(limit=None):
                if ours(candidate):
                    message = candidate
                    break
        has_map = self.snapshot and self.snapshot['regions']
        if has_map and self.picture is None:
            async with self.render_lock:
                self.picture = await asyncio.to_thread(render, self.snapshot)
        embed = map_embed(self.shard, self.snapshot, self.error, marker)
        view = MapView(self, self.snapshot) if has_map else None
        file = discord.File(BytesIO(self.picture), filename='war-map.png') if has_map else None
        try:
            if message:
                message = await message.edit(embed=embed, view=view, attachments=[file] if file else [],
                                             allowed_mentions=discord.AllowedMentions.none())
            else:
                kwargs = {'file': file} if file else {}
                message = await channel.send(embed=embed, view=view,
                                             allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        finally:
            if file:
                file.close()
        await self.bot.set_cfg(guild.id, 'msg.war_map', message.id)
        if not message.pinned:
            try:
                await message.pin(reason='Постоянная карта войны')
            except discord.HTTPException:
                log.warning('Could not pin war map in guild %s', guild.id)
        return message

    async def show_region(self, interaction, name):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await reply(interaction, 'Откройте карту на сервере.')
            return
        channel = get_text_channel(self.bot, guild, 'brief')
        user = await self.bot.db.read(repo.user_get, interaction.user.id)
        if not can_staff(self.bot, interaction.user) and (
            not user or user['status'] != 'admitted' or channel is None
            or not channel.permissions_for(interaction.user).view_channel
        ):
            await reply(interaction, 'Для просмотра карты нужен допуск и доступ к каналу сводки.')
            return
        snapshot = self.snapshot
        if not self.bot.config.war_map_enabled or snapshot is None or name not in snapshot['regions']:
            await reply(interaction, 'Данные этого региона пока недоступны. Откройте актуальную карточку карты.')
            return
        now = time.monotonic()
        if now < self.region_cooldowns.get(interaction.user.id, 0):
            await reply(interaction, 'Подождите несколько секунд перед следующим просмотром.')
            return
        self.region_cooldowns = {uid: until for uid, until in self.region_cooldowns.items() if until > now}
        self.region_cooldowns[interaction.user.id] = now + 5
        async with self.render_lock:
            picture = await asyncio.to_thread(render, snapshot, name)
        embed = map_embed(self.shard, snapshot, self.error, 'Увеличенная карта региона', region=name)
        file = discord.File(BytesIO(picture), filename='war-map.png')
        try:
            await reply(interaction, embed=embed, file=file)
        finally:
            file.close()

    warmap = app_commands.Group(name='warmap', description='Карта войны Foxhole', guild_only=True)

    @warmap.command(name='publish', description='Опубликовать или восстановить карту в сводке')
    async def publish_command(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_admin(self.bot, interaction.user):
            await reply(interaction, 'Нужны права Admin.')
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.publish(interaction.guild)
        await reply(interaction, f'Карта: {message.jump_url}' if message else
                    'Настройте канал сводки через /setup run и включите WAR_MAP_ENABLED.')

    @warmap.command(name='status', description='Проверить источник данных и последнее обновление карты')
    async def status(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_admin(self.bot, interaction.user):
            await reply(interaction, 'Нужны права Admin.')
            return
        checked = f"<t:{int(self.snapshot['checked_at'])}:F>" if self.snapshot else 'ещё не загружена'
        await reply(interaction, f'Карта включена: {self.bot.config.war_map_enabled}\nСервер: {self.shard.title()}\n'
                    f'Последняя успешная проверка: {checked}\nОшибка: {self.error or "нет"}')


async def setup(bot):
    await bot.add_cog(WarMapCog(bot))
