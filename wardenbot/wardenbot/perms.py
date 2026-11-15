"""Role-based permission checks. Role ids are kept in the guild config (set by /setup)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

import discord

from .layout import RANK_ROLES

if TYPE_CHECKING:  # pragma: no cover
    from .bot import WardenBot


def member_role_keys(bot: "WardenBot", member: discord.Member) -> set[str]:
    """Role keys (command, staff, recruit, ...) that the member holds."""
    cfg = bot.cfg(member.guild.id)
    held = {r.id for r in member.roles}
    return {k[len("role."):] for k, v in cfg.items() if k.startswith("role.") and v.isdigit() and int(v) in held}


def has_any(bot: "WardenBot", member: discord.Member, keys: Iterable[str]) -> bool:
    return bool(member_role_keys(bot, member) & set(keys))


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def can_staff(bot: "WardenBot", member: discord.Member) -> bool:
    return is_admin(member) or has_any(bot, member, ("staff", "command"))


def can_screen(bot: "WardenBot", member: discord.Member) -> bool:
    return is_admin(member) or has_any(bot, member, ("staff", "command"))


def can_create_ops(bot: "WardenBot", member: discord.Member) -> bool:
    return can_staff(bot, member) or has_any(bot, member, ("ops_officer", "regiment_co"))


def can_vouch(bot: "WardenBot", member: discord.Member) -> bool:
    return can_staff(bot, member) or has_any(bot, member, ("sergeant", "regiment_co"))


def is_admitted(bot: "WardenBot", member: discord.Member) -> bool:
    return can_staff(bot, member) or has_any(bot, member, RANK_ROLES)


def can_manage_op(bot: "WardenBot", member: discord.Member, organizer_id: int) -> bool:
    return member.id == organizer_id or can_staff(bot, member)


def can_admin(bot, member: discord.Member) -> bool:
    """Native Discord administrators and the configured bot Admin role."""
    return is_admin(member) or has_any(bot, member, ('command',))
