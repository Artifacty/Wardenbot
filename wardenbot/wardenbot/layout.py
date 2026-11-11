"""Declarative description of the server: roles, categories and channels.

This mirrors the project plan. It is plain data so it can be tested without Discord.
"""

from __future__ import annotations

from dataclasses import dataclass

from .opsmeta import ALL_FORUM_TAGS


# ---------------------------------------------------------------- roles ---
@dataclass(frozen=True)
class RoleDef:
    key: str
    symbol: str
    ru: str
    en: str
    color: int
    hoist: bool = False
    mentionable: bool = False
    admin: bool = False
    moderator: bool = False  # mute / kick / delete messages

    @property
    def name(self) -> str:
        return f"{self.symbol} {self.ru} · {self.en}"


# Listed from the highest to the lowest. Discord puts new roles at the bottom, so
# the setup command creates them in this order.
ROLES: tuple[RoleDef, ...] = (
    RoleDef("command", "★", "Командование", "Command", 0xC9A227, hoist=True, admin=True),  # admin = may also ban; never Discord Administrator
    RoleDef("staff", "◆", "Штаб", "Staff", 0x3C8DD6, hoist=True, moderator=True),
    RoleDef("counterintel", "⌖", "Контрразведка", "Counterintel", 0x6C5CE7, hoist=True),
    RoleDef("regiment_co", "❖", "Командир полка", "Regiment CO", 0x2E86AB, hoist=True),
    RoleDef("ops_officer", "◇", "Офицер операций", "Ops Officer", 0x4DA3D9, hoist=True),
    RoleDef("sergeant", "›››", "Сержант", "Sergeant", 0x7FB3E0),
    RoleDef("private", "››", "Рядовой", "Private", 0x9CC4E8),
    RoleDef("recruit", "›", "Рекрут", "Recruit", 0xBAD6EE),
    RoleDef("ally", "○", "Союзник", "Ally", 0x95A5A6),
    RoleDef("lang_ru", "", "Язык: русский", "Language: Russian", 0x5D6D7E),
    RoleDef("lang_en", "", "Язык: английский", "Language: English", 0x5D6D7E),
    RoleDef("ping_ops", "", "Пинг: операции", "Ping: operations", 0x34495E, mentionable=True),
    RoleDef("ping_logi", "", "Пинг: логистика", "Ping: logistics", 0x34495E, mentionable=True),
)
ROLE_BY_KEY = {r.key: r for r in ROLES}


def role_display_name(role: RoleDef) -> str:
    return role.name if role.symbol else f"{role.ru} · {role.en}"


RANK_ROLES = ("recruit", "private", "sergeant")
STAFF_ROLES = ("staff", "counterintel", "command")
# Access levels: who sees a channel. Ranks are cumulative.
_RANK_CHAIN = {"recruit": RANK_ROLES, "private": RANK_ROLES[1:], "sergeant": RANK_ROLES[2:]}


def viewer_roles(access: str) -> tuple[str, ...]:
    """Role keys that can view a channel at the given access level."""
    if access == "public":
        return ()  # everyone
    if access in _RANK_CHAIN:
        return _RANK_CHAIN[access] + STAFF_ROLES
    if access == "staff":
        return ("staff", "command")
    if access == "screening":
        return STAFF_ROLES
    if access == "command":
        return ("command",)
    if access == "co":
        return ("regiment_co", "staff", "command")
    raise KeyError(f"unknown access level: {access}")


ACCESS_LEVELS = ("public", "recruit", "private", "sergeant", "staff", "screening", "command", "co")


# ------------------------------------------------------------- channels ---
@dataclass(frozen=True)
class ChannelDef:
    key: str
    kind: str  # text | forum | voice
    symbol: str
    ru: str
    en: str
    emoji: str
    topic_ru: str
    topic_en: str
    access: str
    mode: str  # ro | chat | forum_bot | forum_open | voice
    launch: bool = True
    allies: bool = False  # allies may see the channel too
    forum_tags: tuple[str, ...] = ()
    guidelines: str = ""

    def name(self, style: str = "full") -> str:
        if style == "ru":
            body = f"{self.symbol}┃{self.ru}"
        elif style == "en":
            body = f"{self.symbol}┃{self.en}"
        else:
            body = f"{self.symbol}┃{self.ru}┃{self.en}"
        return f"{body}{self.emoji}"

    @property
    def topic(self) -> str:
        return f"{self.topic_ru}\n{self.topic_en}"[:1024]


@dataclass(frozen=True)
class CategoryDef:
    key: str
    number: str
    ru: str
    en: str
    emoji: str
    access: str
    channels: tuple[ChannelDef, ...]

    @property
    def name(self) -> str:
        return f"{self.number} // {self.ru} · {self.en} {self.emoji}"


def _ch(key, kind, symbol, ru, en, emoji, topic_ru, topic_en, access, mode, **kw) -> ChannelDef:
    return ChannelDef(key, kind, symbol, ru, en, emoji, topic_ru, topic_en, access, mode, **kw)


CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef("entry", "00", "ВХОД", "ENTRY", "🚪", "public", (
        _ch("rules", "text", "⬢", "правила", "rules", "📜",
            "Правила сервера на двух языках.", "Server rules in two languages.", "public", "ro"),
        _ch("start", "text", "⬢", "старт", "start", "🚀",
            "Что это за сервер, выбор языка и пингов.", "What this server is, language and ping roles.", "public", "ro"),
        _ch("verify", "text", "⬢", "допуск", "verify", "🛂",
            "Кнопка допуска. Писать в канал нельзя, бот отвечает только вам.",
            "Verification button. You cannot post here; the bot replies only to you.", "public", "ro"),
        _ch("news", "text", "⬢", "новости", "news", "📰",
            "Новости сервера и бота.", "Server and bot news.", "public", "ro"),
    )),
    CategoryDef("hq", "01", "ШТАБ", "HQ", "🏛️", "private", (
        _ch("objectives", "text", "⬢", "цели", "objectives", "🎯",
            "Текущие направления и цели. Один пост на фронт; бот редактирует их.",
            "Current fronts and goals. One post per front; the bot edits them.", "private", "ro"),
        _ch("war_status", "text", "⬢", "статус-войны", "war-status", "🗺️",
            "Номер войны, день, захваты регионов.", "War number, day, region captures.", "private", "ro", launch=False),
        _ch("brief", "text", "⬢", "сводка", "brief", "📋",
            "Ежедневная сводка бота: операции и срочные нужды.", "Daily bot brief: operations and urgent needs.", "private", "ro"),
    )),
    CategoryDef("ops", "02", "ОПЕРАЦИИ", "OPS", "⚔️", "recruit", (
        _ch("ops_board", "forum", "▤", "доска-операций", "ops-board", "⚔️",
            "Один пост равен одной операции. Создаются командой /operation create.",
            "One post is one operation. Create them with /operation create.", "recruit", "forum_bot",
            forum_tags=tuple(ALL_FORUM_TAGS),
            guidelines="Записывайтесь кнопкой под карточкой. Sign up with the button under the card."),
        _ch("supply", "forum", "▤", "снабжение", "supply", "📦",
            "Запросы на снабжение по операциям.", "Supply requests for operations.", "recruit", "forum_open", launch=False),
        _ch("lfg", "text", "⬡", "ищу-группу", "lfg", "🔎",
            "Быстрый поиск группы прямо сейчас.", "Quick group search, right now.", "recruit", "chat"),
        _ch("after_action", "forum", "▤", "отчёты", "after-action", "📊",
            "Итоги операций и посещаемость.", "Operation results and attendance.", "private", "forum_open", launch=False),
    )),
    CategoryDef("logistics", "03", "ЛОГИСТИКА", "LOGI", "🚚", "recruit", (
        _ch("sell", "forum", "▤", "продажа", "sell", "🏷️",
            "Продажа внутриигровых материалов и техники. Срок жизни 7–14 дней.",
            "Selling in-game materials and vehicles. Listings live 7-14 days.", "recruit", "forum_open",
            guidelines="Укажите: что, сколько, где передать. State what, how much and where to hand it over."),
        _ch("buy", "forum", "▤", "покупка", "buy", "🛒",
            "Покупка внутриигровых материалов и техники.", "Buying in-game materials and vehicles.", "recruit", "forum_open",
            guidelines="Укажите: что, сколько, где передать. State what, how much and where to hand it over."),
        _ch("services", "forum", "▤", "услуги", "services", "🔧",
            "Перевозка, производство, фронтовая логистика.", "Hauling, production, front-line logistics.", "recruit", "forum_open", launch=False),
    )),
    CategoryDef("units", "04", "ПОЛКИ", "UNITS", "🪖", "recruit", (
        _ch("recruitment", "forum", "▤", "набор", "recruitment", "📣",
            "Карточки набора от полков.", "Recruitment cards from regiments.", "recruit", "forum_open",
            guidelines="Объявления о наборе ведут командиры и офицеры полка. Recruitment posts by regiment officers only."),
        _ch("seeking_unit", "forum", "▤", "ищу-полк", "seeking-unit", "🙋",
            "Карточки игроков, которые ищут полк.", "Cards of players looking for a regiment.", "recruit", "forum_open"),
        _ch("commanders", "text", "▨", "командиры", "commanders", "🎖️",
            "Дипломатия и координация между полками.", "Diplomacy and coordination between regiments.", "co", "chat"),
    )),
    CategoryDef("comms", "05", "СВЯЗЬ", "COMMS", "📡", "recruit", (
        _ch("general_ru", "text", "⬡", "общий", "general-ru", "🇷🇺",
            "Русскоязычный общий чат.", "Russian-language general chat.", "recruit", "chat", allies=True),
        _ch("general_en", "text", "⬡", "general", "general-en", "🇬🇧",
            "English general chat.", "Английский общий чат.", "recruit", "chat", allies=True),
        _ch("questions", "forum", "▤", "вопросы", "questions", "❓",
            "Вопросы новичков и ответы наставников.", "Newcomer questions and mentor answers.", "recruit", "forum_open", launch=False),
        _ch("guides", "forum", "▤", "гайды", "guides", "📚",
            "Гайды по игре и по серверу.", "Game and server guides.", "recruit", "forum_open", launch=False),
        _ch("feedback", "forum", "▤", "предложения", "feedback", "💡",
            "Предложения по серверу и боту.", "Suggestions for the server and the bot.", "recruit", "forum_open", launch=False),
    )),
    CategoryDef("voice", "06", "ГОЛОС", "VOICE", "🎙️", "recruit", (
        _ch("muster_ru", "voice", "◉", "сбор-ru", "muster-ru", "📢",
            "Сбор перед операцией, русский язык.", "Pre-operation muster, Russian.", "recruit", "voice"),
        _ch("muster_en", "voice", "◉", "сбор-en", "muster-en", "📢",
            "Сбор перед операцией, английский язык.", "Pre-operation muster, English.", "recruit", "voice"),
        _ch("mixed", "voice", "◉", "смешанный", "mixed", "🌐",
            "Смешанный состав, оба языка.", "Mixed group, both languages.", "recruit", "voice", allies=True),
        _ch("rookies", "voice", "◉", "новичкам", "rookies", "🌱",
            "Голосовое общение новичков с наставниками.", "Voice chat for rookies and mentors.", "recruit", "voice", launch=False),
        _ch("create", "voice", "◉", "создать", "create", "➕",
            "Вход создаёт временный канал операции.", "Joining creates a temporary operation channel.", "private", "voice", launch=False),
    )),
    CategoryDef("command", "08", "КОМАНДОВАНИЕ", "COMMAND", "⭐", "staff", (
        _ch("mod", "text", "▨", "модерация", "mod", "🛡️",
            "Рабочий чат модераторов.", "Moderators' working chat.", "staff", "chat"),
        _ch("screening", "text", "▨", "допуск-очередь", "screening", "🔍",
            "Очередь верификации и работа контрразведки.", "Verification queue and counterintelligence.", "screening", "chat"),
        _ch("audit_log", "text", "▨", "журнал", "audit-log", "🧾",
            "Журнал бота: входы, выдача ролей, решения.", "Bot log: joins, role grants, decisions.", "staff", "ro"),
        _ch("bot_admin", "text", "▨", "бот", "bot-admin", "🤖",
            "Ошибки и метрики бота.", "Bot errors and metrics.", "command", "ro"),
        _ch("command_voice", "voice", "◉", "штаб-голос", "command-voice", "🎙️",
            "Голосовой канал штаба.", "Staff voice channel.", "staff", "voice", launch=False),
    )),
)

ALL_CHANNELS: tuple[ChannelDef, ...] = tuple(c for cat in CATEGORIES for c in cat.channels)
CHANNEL_BY_KEY = {c.key: c for c in ALL_CHANNELS}


def launch_channels() -> tuple[ChannelDef, ...]:
    return tuple(c for c in ALL_CHANNELS if c.launch)
