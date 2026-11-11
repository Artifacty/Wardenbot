"""Helpers shared by the cogs: audit log, DMs, rank roles, error reporting."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord

from . import repo
from . import ranks
from .i18n import lang_of, t
from .layout import RANK_ROLES, ROLE_BY_KEY
from .timeutil import from_iso

if TYPE_CHECKING:  # pragma: no cover
    from .bot import WardenBot

log = logging.getLogger(__name__)
BLUE = 0x2F6FB5


def utcnow() -> datetime:
    return datetime.now(UTC)


def channel_mention(bot: "WardenBot", guild_id: int, key: str) -> str:
    cid = bot.cfg(guild_id).get(f"ch.{key}")
    return f"<#{cid}>" if cid and cid.isdigit() else f"#{key}"


def get_text_channel(bot: "WardenBot", guild: discord.Guild, key: str) -> discord.abc.GuildChannel | None:
    cid = bot.cfg(guild.id).get(f"ch.{key}")
    if cid and cid.isdigit():
        return guild.get_channel(int(cid))
    return None


def get_role(bot: "WardenBot", guild: discord.Guild, key: str) -> discord.Role | None:
    rid = bot.cfg(guild.id).get(f"role.{key}")
    if rid and rid.isdigit():
        return guild.get_role(int(rid))
    return None


async def audit(
    bot: "WardenBot", guild: discord.Guild, actor: discord.abc.User | int | None, action: str,
    target: str = "", details: str = "",
) -> None:
    """Write to the database and mirror to the audit-log channel (best effort)."""
    actor_id = actor if isinstance(actor, int) or actor is None else actor.id
    now = utcnow()
    await bot.db.write(repo.audit_add, guild.id, actor_id, action, target, details, now)
    channel = get_text_channel(bot, guild, "audit_log")
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(color=BLUE, timestamp=now)
    embed.title = action
    lines = []
    if actor_id:
        lines.append(f"<@{actor_id}>")
    if target:
        lines.append(target)
    if details:
        lines.append(details)
    embed.description = "\n".join(lines)[:4000] or None
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning("Could not write to the audit-log channel", exc_info=True)


async def safe_dm(user: discord.abc.User, content: str | None = None, embed: discord.Embed | None = None) -> bool:
    try:
        await user.send(content=content, embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def set_rank_roles(bot: "WardenBot", member: discord.Member, rank: str, reason: str) -> None:
    """Give the member exactly one rank role."""
    guild = member.guild
    target = get_role(bot, guild, rank)
    others = [r for key in RANK_ROLES if key != rank and (r := get_role(bot, guild, key)) is not None and r in member.roles]
    if others:
        await member.remove_roles(*others, reason=reason)
    if target is not None and target not in member.roles:
        await member.add_roles(target, reason=reason)


async def evaluate_member(bot: "WardenBot", guild: discord.Guild, user_id: int, now: datetime | None = None) -> str | None:
    """Promote a member if the rules allow it. Returns the new rank or None."""
    now = now or utcnow()
    rules = bot.rank_rules

    def work(conn):
        user = repo.user_get(conn, user_id)
        if user is None or user["status"] != "admitted" or not user["rank"]:
            return None
        admitted = from_iso(user["admitted_at"]) if user["admitted_at"] else None
        new_rank = ranks.auto_rank(
            user["rank"], admitted, now, repo.vouch_valid_count(conn, user_id) > 0,
            repo.attended_count(conn, user_id), rules,
        )
        if new_rank == user["rank"]:
            return None
        repo.user_update(conn, user_id, now, rank=new_rank)
        return new_rank

    new_rank = await bot.db.write(work)
    if new_rank is None:
        return None
    member = guild.get_member(user_id)
    if member is None:
        return new_rank
    try:
        await set_rank_roles(bot, member, new_rank, reason="Automatic promotion")
    except discord.HTTPException:
        log.warning("Could not update rank roles for %s", user_id, exc_info=True)
    label = ROLE_BY_KEY[new_rank]
    await audit(bot, guild, None, "Promotion", f"<@{user_id}>", label.name)
    await safe_dm(member, f"{t('mem_promoted', 'ru', rank=label.ru)}\n{t('mem_promoted', 'en', rank=label.en)}")
    return new_rank


async def reply(interaction: discord.Interaction, content: str | None = None, **kwargs) -> None:
    """Respond or follow up, ephemeral by default."""
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


async def report_error(bot: "WardenBot", interaction: discord.Interaction | None, error: BaseException, where: str) -> None:
    """Log an unexpected error, tell the user briefly and notify the bot-admin channel."""
    log.error("Unhandled error in %s", where, exc_info=error)
    if interaction is not None:
        try:
            await reply(interaction, t("err_generic", lang_of(interaction)))
        except discord.HTTPException:
            pass
        guild = interaction.guild
        if guild is not None:
            channel = get_text_channel(bot, guild, "bot_admin")
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(f"Error in `{where}`: `{type(error).__name__}: {str(error)[:300]}`")
                except discord.HTTPException:
                    pass
