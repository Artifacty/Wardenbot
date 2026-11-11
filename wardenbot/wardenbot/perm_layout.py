"""Turns channel access levels and modes into Discord permission overwrites (no network)."""

from __future__ import annotations

import discord

from .layout import CategoryDef, ChannelDef, viewer_roles

STAFF_WRITERS = ("staff", "command")


def overwrite_for(mode: str, role_key: str | None = None, public: bool = False) -> discord.PermissionOverwrite:
    """Overwrite for a role that is allowed to see a channel in the given mode."""
    if mode == "voice":
        # connect / speak stay at the server default; only visibility is controlled here
        return discord.PermissionOverwrite(view_channel=True)
    staff = role_key in STAFF_WRITERS
    common = dict(view_channel=True, read_message_history=True, use_application_commands=True)
    if mode == "ro":
        extra = dict(
            add_reactions=not public, send_messages=staff, send_messages_in_threads=staff,
            create_public_threads=False, create_private_threads=False,
        )
    elif mode == "chat":
        extra = dict(
            send_messages=True, send_messages_in_threads=True, create_public_threads=True,
            add_reactions=True, attach_files=True, embed_links=True,
        )
    elif mode == "forum_bot":
        extra = dict(
            send_messages=staff, send_messages_in_threads=True, add_reactions=True,
            attach_files=True, embed_links=True,
        )
    elif mode == "forum_open":
        extra = dict(
            send_messages=True, send_messages_in_threads=True, add_reactions=True,
            attach_files=True, embed_links=True,
        )
    else:
        raise KeyError(f"unknown channel mode: {mode}")
    return discord.PermissionOverwrite(**common, **extra)


def bot_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, send_messages=True, send_messages_in_threads=True, create_public_threads=True,
        manage_threads=True, manage_messages=True, embed_links=True, attach_files=True,
        read_message_history=True, add_reactions=True,
    )


def channel_overwrites(ch: ChannelDef) -> dict[str, discord.PermissionOverwrite]:
    """Map 'everyone' / role key / 'bot' to an overwrite for one channel."""
    result: dict[str, discord.PermissionOverwrite] = {}
    if ch.access == "public":
        result["everyone"] = overwrite_for(ch.mode, None, public=True)
    else:
        result["everyone"] = discord.PermissionOverwrite(view_channel=False)
        for key in viewer_roles(ch.access):
            result[key] = overwrite_for(ch.mode, key)
        if ch.allies:
            result["ally"] = overwrite_for(ch.mode, "ally")
    result["bot"] = bot_overwrite()
    return result


def category_overwrites(cat: CategoryDef) -> dict[str, discord.PermissionOverwrite]:
    """Categories only control visibility; channels carry the full permission set."""
    result: dict[str, discord.PermissionOverwrite] = {}
    if cat.access == "public":
        result["everyone"] = discord.PermissionOverwrite(view_channel=True)
    else:
        result["everyone"] = discord.PermissionOverwrite(view_channel=False)
        for key in viewer_roles(cat.access):
            result[key] = discord.PermissionOverwrite(view_channel=True)
        if any(c.allies for c in cat.channels):
            result["ally"] = discord.PermissionOverwrite(view_channel=True)
    result["bot"] = discord.PermissionOverwrite(view_channel=True, manage_channels=True)
    return result
