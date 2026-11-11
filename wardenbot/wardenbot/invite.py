"""Which Discord permissions the bot needs, and the invite link.

The bot never asks for Administrator: a leaked token must not give away the server. The list is
computed from what /setup actually does (roles it creates, overwrites it writes), so it cannot
drift from the code.

    python -m wardenbot.invite <APPLICATION_ID>
"""

from __future__ import annotations

import sys

import discord

from .layout import CATEGORIES, RoleDef
from .perm_layout import category_overwrites, channel_overwrites

# Needed for the management work itself.
BASE = (
    "manage_roles", "manage_channels", "manage_guild", "manage_messages", "manage_threads",
    "moderate_members", "kick_members", "ban_members", "view_audit_log",
)


def role_permissions(rd: RoleDef) -> discord.Permissions:
    """Permissions given to the roles the bot creates. Never Administrator."""
    if rd.moderator or rd.admin:
        return discord.Permissions(
            kick_members=True, moderate_members=True, manage_messages=True, manage_threads=True,
            view_audit_log=True, ban_members=rd.admin,
        )
    if rd.key == "counterintel":
        return discord.Permissions(view_audit_log=True, moderate_members=True)
    return discord.Permissions.none()


def required_permissions() -> discord.Permissions:
    from .layout import ROLES

    total = discord.Permissions.none()
    for name in BASE:
        setattr(total, name, True)
    for cat in CATEGORIES:
        overwrites = [category_overwrites(cat), *(channel_overwrites(ch) for ch in cat.channels)]
        for mapping in overwrites:
            for ow in mapping.values():
                allow, _deny = ow.pair()
                total |= allow
    for rd in ROLES:
        total |= role_permissions(rd)
    total.administrator = False
    return total


def required_permission_names() -> list[str]:
    return [name for name, value in required_permissions() if value]


def invite_url(application_id: int | str) -> str:
    perms = required_permissions().value
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&permissions={perms}&scope=bot%20applications.commands"
    )


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: python -m wardenbot.invite <APPLICATION_ID>", file=sys.stderr)
        return 2
    print(invite_url(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
