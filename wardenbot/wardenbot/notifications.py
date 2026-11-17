"""Durable Discord delivery. Reconcile bot-authored markers after ambiguous HTTP failures."""
import json
from datetime import timedelta
import discord
from . import repo
from .operation_service import notify
from .timeutil import from_iso


def schedule(conn, now):
    for op in repo.ops_active(conn):
        if not op['thread_id'] or op['status'] != 'open':
            continue
        seconds = (from_iso(op['start_at'])-now).total_seconds()
        if seconds <= 0:
            repo.op_update(conn,op['id'],status='running')
            conn.execute("UPDATE notifications SET state='superseded' WHERE op_id=? AND state='pending' AND kind LIKE 'remind%'",(op['id'],))
            notify(conn,op,'started',f"Операция #{op['id']} началась.",now)
        elif seconds <= 15*60:
            conn.execute("UPDATE notifications SET state='superseded' WHERE op_id=? AND state='pending' AND kind='remind60'",(op['id'],))
            notify(conn,op,'remind15',f"Операция #{op['id']} начнётся через 15 минут или раньше: <t:{int(from_iso(op['start_at']).timestamp())}:F>.",now)
        elif seconds <= 60*60:
            notify(conn,op,'remind60',f"Операция #{op['id']} начнётся в течение часа: <t:{int(from_iso(op['start_at']).timestamp())}:F>.",now)


def pending(conn):
    return conn.execute("SELECT * FROM notifications WHERE state='pending' ORDER BY id").fetchall()


def ack(conn, job_id, message_id):
    conn.execute("UPDATE notifications SET state='sent',message_id=?,last_error=NULL WHERE id=? AND state='pending'",(message_id,job_id))


def failed(conn, job_id, error):
    conn.execute('UPDATE notifications SET attempts=attempts+1,last_error=? WHERE id=?',(error,job_id))


async def send(bot, job, thread):
    current = await bot.db.read(lambda c:c.execute('SELECT * FROM notifications WHERE id=?',(job['id'],)).fetchone())
    if current['state'] != 'pending':
        return
    marker = f"wb:notice:{job['id']}:"
    existing = {}
    async for msg in thread.history(limit=None, after=from_iso(job['created_at'])-timedelta(seconds=2)):
        if msg.author.id == bot.user.id:
            for embed in msg.embeds:
                if embed.footer.text and embed.footer.text.startswith(marker):
                    existing[embed.footer.text] = msg.id
    users = json.loads(job['recipients'])
    groups = [users[n:n+60] for n in range(0,len(users),60)] or [[]]
    last = None
    for part, group in enumerate(groups):
        key = marker+str(part)
        if key in existing:
            last = existing[key]
            continue
        embed = discord.Embed(description=job['content'],color=0x2F6FB5)
        embed.set_footer(text=key)
        msg = await thread.send(content=' '.join(f'<@{u}>' for u in group) or None,embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=u) for u in group]))
        last = msg.id
    await bot.db.write(ack,job['id'],last)
