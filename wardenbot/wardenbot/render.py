"""Builds the bilingual operation card as plain data (converted to an embed elsewhere)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .opsmeta import (
    CANCELLED,
    DONE,
    LANGS,
    OP_TYPES,
    OPEN,
    RUNNING,
    STATUS_COLOR,
    STATUS_LABEL,
    STATUS_SYMBOL,
    URGENT_SYMBOL,
)
from .slots import progress_bar

TITLE_MAX = 256
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
DESCRIPTION_MAX = 1800
EMBED_TOTAL_MAX = 5800  # Discord allows 6000 in total; keep a margin.


@dataclass
class SlotView:
    id: int
    name: str
    capacity: int
    members: list[int] = field(default_factory=list)


@dataclass
class SupplyView:
    name: str
    needed: int
    collected: int = 0


@dataclass
class OpView:
    id: int
    title: str
    region: str
    type: str
    language: str
    start_at: datetime
    duration_min: int
    organizer_id: int
    requirements: str
    briefing_public: str
    status: str
    urgent: bool = False
    slots: list[SlotView] = field(default_factory=list)
    supply: list[SupplyView] = field(default_factory=list)


@dataclass
class CardField:
    name: str
    value: str
    inline: bool = False


@dataclass
class Card:
    title: str
    description: str
    color: int
    fields: list[CardField]
    footer: str


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def thread_title(region: str, title: str) -> str:
    """Forum post title, at most 100 characters."""
    region = region.strip()
    base = f"[{region}] {title.strip()}" if region else title.strip()
    return clip(base, 100)


def format_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"~{hours} ч"
    if hours == 0:
        return f"~{rest} мин"
    return f"~{hours} ч {rest} мин · {hours} h {rest} min"


def _unix(dt: datetime) -> int:
    return int(dt.timestamp())


def _slots_fields(slots: list[SlotView]) -> list[CardField]:
    lines: list[str] = []
    for s in slots:
        count = len(s.members)
        full = " · полно" if count >= s.capacity else ""
        head = f"**{s.name}** {count}/{s.capacity}{full}"
        if s.members:
            names = " ".join(f"<@{uid}>" for uid in s.members)
            if len(names) > 360:
                shown: list[str] = []
                used = 0
                for uid in s.members:
                    token = f"<@{uid}>"
                    if used + len(token) + 1 > 330:
                        break
                    shown.append(token)
                    used += len(token) + 1
                names = " ".join(shown) + f" +{len(s.members) - len(shown)}"
            lines.append(f"{head}\n{names}")
        else:
            lines.append(head)
    fields: list[CardField] = []
    chunk: list[str] = []
    size = 0
    for line in lines:
        if chunk and size + len(line) + 2 > FIELD_VALUE_MAX - 20:
            fields.append(CardField("", "\n\n".join(chunk)))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 2
    if chunk:
        fields.append(CardField("", "\n\n".join(chunk)))
    for i, f in enumerate(fields):
        f.name = "Слоты" if i == 0 else f"Слоты({i + 1})"
    return fields


def _supply_field(items: list[SupplyView]) -> CardField | None:
    if not items:
        return None
    lines = []
    for it in items:
        pct = int(min(100, it.collected * 100 // it.needed)) if it.needed else 0
        lines.append(f"{it.name}: {it.collected}/{it.needed} {progress_bar(it.collected, it.needed)} {pct}%")
    return CardField("Снабжение", clip("\n".join(lines), FIELD_VALUE_MAX))


def build_card(op: OpView) -> Card:
    """Build the public operation card. The closed briefing is never included."""
    symbol = STATUS_SYMBOL.get(op.status, "○")
    urgent = f" {URGENT_SYMBOL}" if op.urgent and op.status in {OPEN, RUNNING} else ""
    title = clip(f"{symbol} {op.title}{urgent}", TITLE_MAX)

    end = op.start_at + timedelta(minutes=op.duration_min)
    type_label = OP_TYPES.get(op.type)
    lang_label = LANGS.get(op.language)
    fields = [
        CardField("Старт", f"<t:{_unix(op.start_at)}:F>\n<t:{_unix(op.start_at)}:R>", True),
        CardField("Длительность", f"{format_duration(op.duration_min)}\n→ <t:{_unix(end)}:t>", True),
        CardField("Регион", clip(op.region or "—", 100), True),
        CardField("Тип", type_label.ru if type_label else op.type, True),
        CardField("Язык", lang_label.ru if lang_label else op.language, True),
        CardField("Организатор", f"<@{op.organizer_id}>", True),
    ]
    if op.requirements.strip():
        fields.append(CardField("Требования", clip(op.requirements, FIELD_VALUE_MAX)))
    fields.extend(_slots_fields(op.slots))
    supply = _supply_field(op.supply)
    if supply:
        fields.append(supply)

    description = clip(op.briefing_public, DESCRIPTION_MAX) if op.briefing_public.strip() else ""
    if op.status == CANCELLED:
        description = "**Операция отменена**\n" + description
    elif op.status == DONE:
        description = "**Операция завершена**\n" + description

    # Keep the whole embed under Discord's limit by trimming the long fields first.
    def total() -> int:
        return len(title) + len(description) + sum(len(f.name) + len(f.value) for f in fields)

    while total() > EMBED_TOTAL_MAX:
        longest = max(fields, key=lambda f: len(f.value))
        if len(longest.value) <= 120:
            break
        longest.value = clip(longest.value, max(120, len(longest.value) - 200))
    if total() > EMBED_TOTAL_MAX:
        description = clip(description, max(0, DESCRIPTION_MAX - (total() - EMBED_TOTAL_MAX)))

    label = STATUS_LABEL.get(op.status)
    footer = f"ID {op.id} · {label.ru if label else op.status}"
    color = STATUS_COLOR.get(op.status, STATUS_COLOR[OPEN])
    return Card(title=title, description=description, color=color, fields=fields[:25], footer=footer)
