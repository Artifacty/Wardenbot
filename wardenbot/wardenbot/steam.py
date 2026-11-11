"""Steam profile link parsing, used for duplicate-account detection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .timeutil import ParseError

_ID64 = re.compile(r"^7656119\d{10}$")
_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?steamcommunity\.com/(?P<kind>profiles|id)/(?P<value>[A-Za-z0-9_\-]{2,64})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_VANITY = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")


@dataclass(frozen=True)
class SteamRef:
    key: str  # canonical key for duplicate detection: 'id64:...' or 'vanity:...'
    url: str  # canonical URL


def parse_steam(text: str) -> SteamRef:
    """Accept a profile URL or a bare 17-digit SteamID64. Raises ParseError otherwise."""
    raw = (text or "").strip()
    if not raw:
        raise ParseError("bad_steam")
    if _ID64.match(raw):
        return SteamRef(f"id64:{raw}", f"https://steamcommunity.com/profiles/{raw}")
    m = _URL.match(raw)
    if not m:
        raise ParseError("bad_steam")
    kind, value = m.group("kind").lower(), m.group("value")
    if kind == "profiles":
        if not _ID64.match(value):
            raise ParseError("bad_steam")
        return SteamRef(f"id64:{value}", f"https://steamcommunity.com/profiles/{value}")
    if not _VANITY.match(value):
        raise ParseError("bad_steam")
    return SteamRef(f"vanity:{value.lower()}", f"https://steamcommunity.com/id/{value}")
