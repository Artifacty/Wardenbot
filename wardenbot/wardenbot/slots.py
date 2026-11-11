"""Parsing of operation slots and supply lists typed by organizers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .timeutil import ParseError

MAX_SLOTS = 12
MAX_CAPACITY = 200
MAX_NAME = 40
MAX_SUPPLY_ITEMS = 12
MAX_SUPPLY_AMOUNT = 10_000_000


@dataclass(frozen=True)
class SlotSpec:
    name: str
    capacity: int


def _split(text: str) -> list[str]:
    parts = re.split(r"[\n;,]+", text or "")
    return [p.strip() for p in parts if p.strip()]


_NAME_THEN_NUM = re.compile(r"^(?P<name>.+?)\s*(?:[:=\-–—]|x|×)?\s*(?P<num>\d+)\s*$", re.IGNORECASE)
_NUM_THEN_NAME = re.compile(r"^(?P<num>\d+)\s*(?:[xх×:]|\s)\s*(?P<name>.+)$", re.IGNORECASE)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip(" :=-–—·"))


def parse_slots(text: str) -> list[SlotSpec]:
    """Parse lines like 'Пехота 20, ПТ 6' or one slot per line ('Medics: 3')."""
    result: list[SlotSpec] = []
    seen: set[str] = set()
    for part in _split(text):
        m = _NAME_THEN_NUM.match(part) or _NUM_THEN_NAME.match(part)
        if not m:
            raise ParseError("bad_slot_line", part)
        name = _clean_name(m.group("name"))
        capacity = int(m.group("num"))
        if not name or len(name) > MAX_NAME:
            raise ParseError("bad_slot_line", part)
        if not 1 <= capacity <= MAX_CAPACITY:
            raise ParseError("bad_slot_capacity", part)
        key = name.casefold()
        if key in seen:
            raise ParseError("dup_slot", name)
        seen.add(key)
        result.append(SlotSpec(name, capacity))
    if not result:
        raise ParseError("no_slots")
    if len(result) > MAX_SLOTS:
        raise ParseError("too_many_slots", str(MAX_SLOTS))
    return result


@dataclass(frozen=True)
class SupplySpec:
    name: str
    needed: int


_AMOUNT = r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<k>[kк])?"
_SUP_NAME_NUM = re.compile(rf"^(?P<name>.+?)\s*[:=]?\s*{_AMOUNT}\s*$", re.IGNORECASE)
_SUP_NUM_NAME = re.compile(rf"^{_AMOUNT}\s*[xх×]?\s*(?P<name>[^\d].*)$", re.IGNORECASE)


def _amount(num: str, k: str | None) -> int:
    value = float(num.replace(",", "."))
    if k:
        value *= 1000
    return int(round(value))


def parse_supply(text: str) -> list[SupplySpec]:
    """Parse needs like 'BMats 5000', 'Топливо: 2к', '4 x Танки'. Empty text is allowed."""
    result: list[SupplySpec] = []
    seen: set[str] = set()
    for part in _split(text):
        m = _SUP_NAME_NUM.match(part) or _SUP_NUM_NAME.match(part)
        if not m:
            raise ParseError("bad_supply_line", part)
        name = _clean_name(m.group("name"))
        needed = _amount(m.group("num"), m.group("k"))
        if not name or len(name) > MAX_NAME:
            raise ParseError("bad_supply_line", part)
        if not 1 <= needed <= MAX_SUPPLY_AMOUNT:
            raise ParseError("bad_supply_amount", part)
        key = name.casefold()
        if key in seen:
            raise ParseError("dup_supply", name)
        seen.add(key)
        result.append(SupplySpec(name, needed))
    if len(result) > MAX_SUPPLY_ITEMS:
        raise ParseError("too_many_supply", str(MAX_SUPPLY_ITEMS))
    return result


_PROGRESS = re.compile(rf"^(?P<name>.+?)\s*[:=]?\s*(?P<plus>\+)?{_AMOUNT}\s*$", re.IGNORECASE)


def parse_supply_progress(text: str, names: list[str]) -> dict[str, tuple[int, bool]]:
    """Parse progress updates ('BMats 1200' sets, 'BMats +200' adds).

    Returns {exact_name: (amount, is_increment)}. Unknown items raise ParseError.
    """
    lookup = {n.casefold(): n for n in names}
    updates: dict[str, tuple[int, bool]] = {}
    for part in _split(text):
        m = _PROGRESS.match(part)
        if not m:
            raise ParseError("bad_supply_line", part)
        raw_name = _clean_name(m.group("name"))
        exact = lookup.get(raw_name.casefold())
        if exact is None:
            raise ParseError("unknown_supply", raw_name)
        amount = _amount(m.group("num"), m.group("k"))
        if amount > MAX_SUPPLY_AMOUNT:
            raise ParseError("bad_supply_amount", part)
        updates[exact] = (amount, bool(m.group("plus")))
    if not updates:
        raise ParseError("bad_supply_line", text.strip())
    return updates


def progress_bar(done: int, total: int, width: int = 8) -> str:
    """Return a bar like '▰▰▰▱▱▱▱▱'."""
    if total <= 0:
        return "▱" * width
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    if done > 0 and filled == 0:
        filled = 1
    return "▰" * filled + "▱" * (width - filled)
