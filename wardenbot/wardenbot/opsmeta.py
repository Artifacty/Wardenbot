"""Fixed vocabulary for operations: types, statuses, languages, default slots."""

from __future__ import annotations

from dataclasses import dataclass

# Status keys stored in the database.
OPEN, RUNNING, DONE, CANCELLED = "open", "running", "done", "cancelled"
ACTIVE_STATUSES = (OPEN, RUNNING)


@dataclass(frozen=True)
class Label:
    ru: str
    en: str

    @property
    def both(self) -> str:
        return f"{self.ru} · {self.en}"

    def get(self, lang: str) -> str:
        return self.ru if lang == "ru" else self.en


STATUS_SYMBOL = {OPEN: "○", RUNNING: "◑", DONE: "●", CANCELLED: "⊘"}
URGENT_SYMBOL = "▲"
STATUS_LABEL = {
    OPEN: Label("Запись открыта", "Sign-up open"),
    RUNNING: Label("Идёт", "In progress"),
    DONE: Label("Завершена", "Finished"),
    CANCELLED: Label("Отменена", "Cancelled"),
}
STATUS_COLOR = {OPEN: 0x2F6FB5, RUNNING: 0xD9A441, DONE: 0x5C6B7A, CANCELLED: 0x8B3A3A}

# Forum tags. Names are limited to 20 characters by Discord.
STATUS_TAG = {
    OPEN: "○ Запись / Open",
    RUNNING: "◑ Идёт / Live",
    DONE: "● Завершена / Done",
    CANCELLED: "⊘ Отменена / Cancel",
}
URGENT_TAG = "▲ Срочно / Urgent"

OP_TYPES: dict[str, Label] = {
    "offense": Label("Наступление", "Offensive"),
    "defense": Label("Оборона", "Defense"),
    "logistics": Label("Логистика", "Logistics"),
    "naval": Label("Флот", "Naval"),
    "partisan": Label("Партизаны", "Partisans"),
    "training": Label("Обучение", "Training"),
}
TYPE_TAG = {
    "offense": "Атака / Attack",
    "defense": "Оборона / Defense",
    "logistics": "Логистика / Logi",
    "naval": "Флот / Naval",
    "partisan": "Партизаны / Partisan",
    "training": "Обучение / Training",
}

LANGS: dict[str, Label] = {
    "ru": Label("русский", "Russian"),
    "en": Label("английский", "English"),
    "both": Label("русский и английский", "Russian and English"),
}
LANG_TAG = {"ru": "RU", "en": "EN", "both": "RU+EN"}

ALL_FORUM_TAGS: list[str] = [
    *STATUS_TAG.values(),
    URGENT_TAG,
    *TYPE_TAG.values(),
    *LANG_TAG.values(),
]

DEFAULT_SLOTS: dict[str, str] = {
    "offense": "Пехота · Infantry 20, ПТ · AT 6, Танкисты · Tankers 4, Медики · Medics 3, Инженеры · Engineers 4, Логисты · Logi 6",
    "defense": "Пехота · Infantry 20, ПТ · AT 6, Инженеры · Engineers 6, Медики · Medics 3, Логисты · Logi 6",
    "logistics": "Водители · Drivers 10, Погрузка · Loaders 6, Охрана · Escort 4",
    "naval": "Экипаж · Crew 12, Десант · Marines 10",
    "partisan": "Пехота · Infantry 8, ПТ · AT 2, Разведка · Scouts 2",
    "training": "Новички · Rookies 20, Наставники · Mentors 5",
}

DEFAULT_SUPPLY = "BMats 5000, Fuel · Топливо 2000"
