"""Time zone and local date/time parsing. Everything is stored in UTC."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["ParseError", "resolve_tz", "tz_from_name", "parse_local_datetime", "parse_duration_hours", "to_iso", "from_iso"]


class ParseError(ValueError):
    """User input could not be parsed. `code` is an i18n key suffix."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_ALIASES = {
    "мск": "Europe/Moscow",
    "msk": "Europe/Moscow",
    "москва": "Europe/Moscow",
    "киев": "Europe/Kyiv",
    "kyiv": "Europe/Kyiv",
    "kiev": "Europe/Kyiv",
    "минск": "Europe/Minsk",
    "утс": "UTC",
    "utc": "UTC",
    "gmt": "UTC",
    "cet": "Europe/Berlin",
    "est": "America/New_York",
    "pst": "America/Los_Angeles",
}


def _fixed(sign: str, hours: int, minutes: int) -> tuple[tzinfo, str]:
    if hours > 14 or minutes > 59:
        raise ParseError("bad_tz")
    delta = timedelta(hours=hours, minutes=minutes)
    if sign == "-":
        delta = -delta
    label = f"UTC{sign}{hours:02d}:{minutes:02d}"
    return timezone(delta, label), label


def resolve_tz(text: str) -> tuple[tzinfo, str]:
    """Parse a time zone ('Europe/Moscow', 'МСК', 'UTC+3', '+03:00').

    Returns (tzinfo, normalized_name). The normalized name can be stored and passed
    back to `tz_from_name`.
    """
    raw = (text or "").strip()
    if not raw:
        raise ParseError("bad_tz")
    alias = _ALIASES.get(raw.lower())
    if alias:
        raw = alias
    if raw.upper() == "UTC":
        return UTC, "UTC"
    m = _OFFSET_RE.match(raw)
    if m:
        return _fixed(m.group(1), int(m.group(2)), int(m.group(3) or 0))
    try:
        zone = ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ParseError("bad_tz", raw) from exc
    return zone, zone.key


def tz_from_name(name: str, fallback: str = "UTC") -> tzinfo:
    """Inverse of resolve_tz for stored names; never raises."""
    try:
        return resolve_tz(name)[0]
    except ParseError:
        return resolve_tz(fallback)[0]


_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?$")
_TODAY = {"сегодня", "today"}
_TOMORROW = {"завтра", "tomorrow"}


def _build(tz: tzinfo, y: int, mo: int, d: int, t: time) -> datetime:
    try:
        return datetime(y, mo, d, t.hour, t.minute, tzinfo=tz)
    except ValueError as exc:
        raise ParseError("bad_datetime") from exc


def parse_local_datetime(text: str, tz: tzinfo, now: datetime | None = None) -> datetime:
    """Parse a local date and time typed by a user and return it as aware UTC.

    Accepted: '2026-11-08 20:00', '08.11.2026 20:00', '08.11 20:00', 'завтра 21:30',
    'today 19:00', '20:00' (today, or tomorrow if that time has passed).
    """
    now_utc = now or datetime.now(UTC)
    local_now = now_utc.astimezone(tz)
    cleaned = " ".join((text or "").replace("T", " ").split())
    if not cleaned:
        raise ParseError("bad_datetime")

    parts = cleaned.split(" ")
    tm = _TIME_RE.match(parts[-1])
    if not tm:
        raise ParseError("bad_datetime")
    hour, minute = int(tm.group(1)), int(tm.group(2))
    if hour > 23 or minute > 59:
        raise ParseError("bad_datetime")
    t = time(hour, minute)
    date_part = " ".join(parts[:-1]).strip().lower()

    if not date_part:
        candidate = _build(tz, local_now.year, local_now.month, local_now.day, t)
        if candidate <= local_now:
            nxt = local_now + timedelta(days=1)
            candidate = _build(tz, nxt.year, nxt.month, nxt.day, t)
    elif date_part in _TODAY:
        candidate = _build(tz, local_now.year, local_now.month, local_now.day, t)
    elif date_part in _TOMORROW:
        nxt = local_now + timedelta(days=1)
        candidate = _build(tz, nxt.year, nxt.month, nxt.day, t)
    else:
        iso = _ISO_DATE_RE.match(date_part)
        dmy = _DMY_RE.match(date_part)
        if iso:
            candidate = _build(tz, int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), t)
        elif dmy:
            day, month, year_s = int(dmy.group(1)), int(dmy.group(2)), dmy.group(3)
            if year_s:
                year = int(year_s) + (2000 if len(year_s) == 2 else 0)
                candidate = _build(tz, year, month, day, t)
            else:
                candidate = _build(tz, local_now.year, month, day, t)
                if candidate <= local_now:
                    candidate = _build(tz, local_now.year + 1, month, day, t)
        else:
            raise ParseError("bad_datetime")

    result = candidate.astimezone(UTC)
    if result <= now_utc:
        raise ParseError("past")
    if result - now_utc > timedelta(days=120):
        raise ParseError("too_far")
    return result


def parse_duration_hours(text: str) -> int:
    """Parse '2', '1.5', '2 ч', '90m' into minutes (15 .. 24h)."""
    raw = (text or "").strip().lower().replace(",", ".")
    if not raw:
        raise ParseError("bad_duration")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|ч|час|часа|часов|m|min|м|мин)?", raw)
    if not m:
        raise ParseError("bad_duration")
    value = float(m.group(1))
    unit = m.group(2) or "h"
    minutes = round(value if unit in {"m", "min", "м", "мин"} else value * 60)
    if not 15 <= minutes <= 24 * 60:
        raise ParseError("bad_duration")
    return minutes


def to_iso(dt: datetime) -> str:
    """Serialize an aware datetime as UTC ISO text."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    """Parse text produced by to_iso."""
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
