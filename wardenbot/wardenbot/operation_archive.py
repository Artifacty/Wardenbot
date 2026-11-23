"""Copy a terminal operation to the archive before removing its forum post.

One bot process per database. A stable footer reconciles a successful Discord send
whose database receipt was lost. Failures leave the source intact and are retried.
"""
import asyncio
import json
import discord
from . import archives, repo
from .services import get_text_channel


class OperationArchiver:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def run(self, op, thread):
        if op['status'] not in ('done', 'cancelled'):
            return
        async with self.lock:
            guild = self.bot.get_guild(op['guild_id'])
            if guild is None:
                return
            job = await self.bot.db.read(archives.get, op['id'])
            if job and job['source_deleted']:
                return
            if job:
                channel = guild.get_channel(job['channel_id'])
                if channel is None:
                    channel = await guild.fetch_channel(job['channel_id'])
            else:
                channel = get_text_channel(self.bot, guild, 'ops_archive')
            if not isinstance(channel, discord.TextChannel):
                return  # /setup run must configure an archive first
            marker = f"wardenbot:archive:{op['guild_id']}:{op['id']}"
            message = None
            if job and job['message_id']:
                try:
                    message = await channel.fetch_message(job['message_id'])
                except discord.NotFound:
                    pass
            if message is None:
                # Includes a previous send that succeeded just before process failure.
                async for candidate in channel.history(limit=None):
                    if candidate.author.id == self.bot.user.id and any(
                        e.footer.text == marker for e in candidate.embeds
                    ):
                        message = candidate
                        break
            if job is None:
                from .cogs.operations import load_view, card_embed
                from .render import build_card
                view = await self.bot.db.read(load_view, op['id'])
                signups = await self.bot.db.read(repo.op_signups, op['id'])
                payload = {
                    'operation_id': op['id'], 'status': op['status'],
                    'ended_at': op['ended_at'],
                    'card': card_embed(build_card(view)).to_dict(),
                    'operation': {k: op[k] for k in ('id','title','region','type','language','organizer_id',
                        'start_at','duration_min','requirements','briefing_public','status','ended_at')},
                    'cancel_reason': op['cancel_reason'],
                    'deliveries': [dict(r) for r in await self.bot.db.read(lambda c:c.execute('SELECT * FROM supply_pledges WHERE op_id=?',(op['id'],)).fetchall())],
                    'slots': [dict(r) for r in await self.bot.db.read(repo.op_slots, op['id'])],
                    'supply': [dict(r) for r in await self.bot.db.read(repo.op_supply, op['id'])],
                    'signups': [dict(row) for row in signups],
                }
                await self.bot.db.write(archives.save, op['id'], channel.id, payload)
                job = await self.bot.db.read(archives.get, op['id'])
            if message is None:
                payload = json.loads(job['payload'])
                embed = discord.Embed.from_dict(payload['card'])
                embed.set_footer(text=marker)
                message = await channel.send(
                    content=f"Итоги операции #{op['id']}." +
                            ('\nПричина отмены: '+payload['cancel_reason'] if payload.get('cancel_reason') else ''),
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await self.bot.db.write(archives.receipt, op['id'], message.id)
            if thread is not None:
                try:
                    await thread.delete(reason=f"Operation archived: {message.id}")
                except discord.NotFound:
                    pass
            await self.bot.db.write(archives.complete, op['id'])
