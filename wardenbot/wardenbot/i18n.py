"""Interface strings. The active community UI uses Russian."""

from __future__ import annotations

import re
from typing import Any

import discord

from .timeutil import ParseError

S: dict[str, dict[str, str]] = {}


def _add(key: str, ru: str, en: str) -> None:
    S[key] = {"ru": ru, "en": en}


def lang_of(interaction: discord.Interaction | None) -> str:
    """'ru' when the user's Discord client is Russian, otherwise 'en'."""
    if interaction is None:
        return "en"
    return "ru" if str(interaction.locale).lower().startswith("ru") else "en"


def t(key: str, lang: str, **kw: Any) -> str:
    entry = S[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**kw) if kw else text


def parse_error_text(exc: ParseError, lang: str) -> str:
    key = f"err_{exc.code}"
    if key not in S:
        return t("err_generic", lang)
    return t(key, lang, detail=exc.detail)


# ------------------------------------------------------------------ errors --
_add("err_generic", "Что-то пошло не так. Попробуйте ещё раз; если повторится, сообщите штабу.",
     "Something went wrong. Try again; if it keeps happening, tell the staff.")
_add("err_no_perm", "У вас нет прав на это действие.", "You do not have permission to do that.")
_add("err_guild_only", "Команда работает только на сервере.", "This command only works on a server.")
_add("err_need_setup", "Сервер ещё не настроен: администратор должен выполнить /setup run.",
     "The server is not set up yet: an administrator must run /setup run.")
_add("err_not_admitted", "Сначала пройдите допуск в канале {verify}.", "Pass verification first in {verify}.")
_add("err_op_missing", "Операция не найдена.", "Operation not found.")
_add("err_bad_datetime", "Укажите, когда начать: завтра 20:30, сегодня 21:00, через 30 минут или 25.11 19:00. Часовой пояс — в заголовке формы.",
     "Could not read the date. Examples: 2026-11-08 20:00, 08.11 20:00, tomorrow 21:30.")
_add("err_past", "Это время уже прошло.", "That time is in the past.")
_add("err_too_far", "Операцию можно запланировать не дальше чем на 120 дней вперёд.",
     "Operations can be scheduled at most 120 days ahead.")
_add("err_bad_duration", "Длительность: от 15 минут до 24 часов, например 2 или 1.5 или 90m.",
     "Duration: from 15 minutes to 24 hours, for example 2 or 1.5 or 90m.")
_add("err_bad_tz", "Не знаю такой пояс. Примеры: Europe/Moscow, МСК, UTC+3.",
     "Unknown time zone. Examples: Europe/Moscow, UTC+3, America/New_York.")
_add("err_bad_steam", "Нужна ссылка на профиль Steam вида https://steamcommunity.com/profiles/7656… или /id/имя.",
     "A Steam profile link is required, like https://steamcommunity.com/profiles/7656… or /id/name.")
_add("err_no_slots", "Укажите хотя бы один слот, например: Пехота 20.", "Add at least one slot, for example: Infantry 20.")
_add("err_bad_slot_line", "Не понял строку слота: «{detail}». Формат: Название 20.",
     "Could not read the slot line: “{detail}”. Format: Name 20.")
_add("err_bad_slot_capacity", "Число мест в слоте от 1 до 200: «{detail}».", "Slot size must be 1 to 200: “{detail}”.")
_add("err_dup_slot", "Слот «{detail}» указан дважды.", "Slot “{detail}” appears twice.")
_add("err_too_many_slots", "Слотов может быть не больше {detail}.", "At most {detail} slots are allowed.")
_add("err_bad_supply_line", "Не понял строку снабжения: «{detail}». Формат: BMats 5000.",
     "Could not read the supply line: “{detail}”. Format: BMats 5000.")
_add("err_bad_supply_amount", "Количество снабжения должно быть от 1 до 10 000 000: «{detail}».",
     "Supply amount must be 1 to 10,000,000: “{detail}”.")
_add("err_dup_supply", "Позиция «{detail}» указана дважды.", "Item “{detail}” appears twice.")
_add("err_too_many_supply", "Позиций снабжения не больше {detail}.", "At most {detail} supply items.")
_add("err_unknown_supply", "В этой операции нет позиции «{detail}».", "This operation has no supply item “{detail}”.")

# ------------------------------------------------------------------- setup --
_add("setup_started", "Настраиваю сервер. Это займёт 1–3 минуты, ничего не удаляется.",
     "Setting the server up. This takes 1-3 minutes and nothing is deleted.")
_add("setup_missing_perms", "Боту не хватает прав: {perms}. Пригласите его заново со ссылкой из инструкции.",
     "The bot lacks permissions: {perms}. Re-invite it with the link from the guide.")
_add("setup_need_community",
     "Форумы доступны только на серверах с режимом «Сообщество». Включите его: Настройки сервера → Включить сообщество (выберите любые временные каналы правил и обновлений), затем повторите /setup run.",
     "Forum channels need the Community mode. Turn it on: Server Settings → Enable Community (pick any temporary rules and updates channels), then run /setup run again.")
_add("setup_done", "Готово.", "Done.")
_add("setup_panels_done", "Сообщения в каналах правил, старта и допуска обновлены.",
     "The rules, start and verification messages are updated.")

# ------------------------------------------------------------ verification --
_add("ver_already", "Вы уже прошли допуск.", "You are already verified.")
_add("ver_pending", "Ваша заявка уже в очереди, штаб ответит в личные сообщения. Не забудьте выполнить /verify screenshot, если ещё не отправили скриншот.",
     "Your request is already in the queue; the staff will reply by direct message. If you have not sent your screenshot yet, run /verify screenshot.")
_add("ver_cooldown", "Повторная заявка возможна через {hours} ч.", "You can apply again in {hours} h.")
_add("ver_modal_title", "Допуск", "Verification")
_add("ver_f_steam", "Ссылка на профиль Steam", "Steam profile link")
_add("ver_f_name", "Ник в игре", "In-game name")
_add("ver_f_regiment", "Полк (если есть)", "Regiment (if any)")
_add("ver_f_voucher", "Кто может поручиться (ник Discord)", "Who can vouch for you (Discord name)")
_add("ver_started",
     "Заявка создана. Введите код **{code}** в игровой чат. Сделайте один скриншот, "
     "на котором одновременно видны F1 и чат с кодом. Отправьте его через `/verify screenshot`. "
     "Код привязан к вашей заявке; скриншот проверит модератор.",
     "Your request is created. Type **{code}** in the in-game chat. Take ONE screenshot showing "
     "both F1 and the chat containing your code. Run `/verify screenshot` and attach that image. "
     "This code is bound to you and this request; a moderator will check the screenshot.")

_add("ver_no_open", "Нет заявки, ожидающей скриншот. Нажмите кнопку допуска в канале {verify}.",
     "No request is waiting for a screenshot. Press the verification button in {verify}.")
_add("ver_bad_image", "Нужен файл-картинка (png, jpg, webp) размером до 10 МБ.",
     "An image file is required (png, jpg, webp), up to 10 MB.")
_add("ver_sent", "Скриншот принят, заявка передана штабу. Ответ придёт в личные сообщения.",
     "Screenshot received and passed to the staff. You will get a reply by direct message.")
_add("ver_no_review", "Канал очереди допуска не настроен. Сообщите администратору.",
     "The screening channel is not configured. Tell an administrator.")
_add("ver_status_none", "У вас нет заявки.", "You have no request.")
_add("ver_status", "Статус заявки: {status}.", "Request status: {status}.")
_add("ver_decided", "Заявка уже обработана.", "This request has already been handled.")
_add("ver_approved_dm", "Допуск одобрен. Добро пожаловать! Роль доступа будет синхронизирована автоматически.",
     "Verification approved. Welcome! Your access role will be synchronized automatically.")
_add("ver_rejected_dm", "Допуск отклонён. Причина: {reason}", "Verification rejected. Reason: {reason}")
_add("ver_info_dm", "По вашей заявке нужны уточнения: {reason}. Выполните `/verify screenshot` с новым скриншотом или напишите штабу.",
     "The staff needs more information: {reason}. Run `/verify screenshot` with a new screenshot or contact the staff.")
_add("ver_reason_title", "Причина", "Reason")
_add("ver_reason_label", "Что написать игроку", "What to tell the player")
_add("ver_done_ok", "Готово.", "Done.")

# ----------------------------------------------------------------- members --
_add("mem_admitted", "{user} допущен(а) со званием {rank}.", "{user} admitted with rank {rank}.")
_add("mem_rank_set", "Звание {user}: {rank}.", "Rank of {user}: {rank}.")
_add("mem_vouched", "Вы поручились за {user}.", "You vouched for {user}.")
_add("mem_vouch_self", "За себя ручаться нельзя.", "You cannot vouch for yourself.")
_add("mem_vouch_not_recruit", "Поручаться можно только за допущенных игроков.", "You can only vouch for admitted players.")
_add("mem_vouch_dup", "Вы уже ручались за этого игрока.", "You have already vouched for this player.")
_add("mem_promoted", "Ваше звание повышено: {rank}.", "You have been promoted: {rank}.")
_add("mem_info",
     "{user}\nЗвание: {rank}\nСтатус: {status}\nДопущен: {admitted}\nПосещено операций: {ops}\n"
     "Поручительств: {vouches}\nSteam: {steam}\nНик: {name}\nПолк: {regiment}",
     "{user}\nRank: {rank}\nStatus: {status}\nAdmitted: {admitted}\nOps attended: {ops}\n"
     "Vouches: {vouches}\nSteam: {steam}\nName: {name}\nRegiment: {regiment}")
_add("mem_unknown", "Об этом игроке нет записей.", "No record of this player.")
_add("tz_set", "Часовой пояс сохранён: {tz}. Время в операциях вы вводите по нему.",
     "Time zone saved: {tz}. Operation times you type use it.")
_add("tz_show", "Ваш часовой пояс: {tz}.", "Your time zone: {tz}.")
_add("help_text",
     "**Команды**\n`/operation create` создать операцию\n`/timezone set` задать часовой пояс\n`/verify screenshot` отправить скриншот для допуска\n"
     "`/member vouch` поручиться за игрока (сержант и выше)\n`/priority set` цели в штабе (штаб)\n\nМалую группу создавайте кнопкой в «ищу-группу». Вопрос или жалобу — в «вопросы-и-жалобы». Предложения — в форуме ШТАБА. Правила: входной канал rules.",
     "**Commands**\n`/operation create` create an operation\n`/timezone set` set your time zone\n`/verify screenshot` send your verification screenshot\n"
     "`/member vouch` vouch for a player (sergeants and above)\n`/priority set` HQ objectives (staff)\n\nRules and tips: the rules channel.")

# -------------------------------------------------------------- operations --
_add("op_no_forum", "Канал доски операций не настроен. Администратор должен выполнить /setup run.",
     "The operations board channel is not configured. An administrator must run /setup run.")
_add("op_modal_title", "Операция", "Operation")
_add("op_f_title", "Название", "Title")
_add("op_f_region", "Регион", "Region")
_add("op_f_start", "Старт (ваш часовой пояс)", "Start (your time zone)")
_add("op_f_duration", "Длительность, часов", "Duration, hours")
_add("op_f_requirements", "Требования (микрофон, опыт, техника)", "Requirements (mic, experience, vehicles)")
_add("op_modal2_title", "Детали операции", "Operation details")
_add("op_f_public", "Описание для всех (без точных деталей)", "Public briefing (no exact details)")
_add("op_f_closed", "Закрытый брифинг (откроется записавшимся)", "Closed briefing (opens to sign-ups)")
_add("op_f_slots", "Места: Пехота: 10 (по строке)", "Slots: Name number, comma separated")
_add("op_f_supply", "Нужно ресурсов (необязательно)", "Supply: BMats 5000, Fuel 2000")
_add("op_draft_basic",
     "Основное сохранено. Время указано по поясу **{tz}** (сменить: `/timezone set`). Теперь добавьте задачу и места участников. Закрытый план и ресурсы можно пропустить.",
     "Basics saved. Times use the **{tz}** zone (change it with `/timezone set`). Now add the briefing and slots.")
_add("op_draft_ready", "Предпросмотр ниже. Если всё верно, нажмите «Опубликовать».",
     "Preview below. If everything is right, press Publish.")
_add("op_draft_gone", "Черновик устарел. Начните заново: /operation create.", "The draft expired. Start again with /operation create.")
_add("op_btn_details", "План и места", "Details")
_add("op_btn_publish", "Опубликовать", "Publish")
_add("op_btn_cancel", "Отмена", "Cancel")
_add("op_cancelled_draft", "Черновик удалён.", "Draft discarded.")
_add("op_published", "Операция опубликована: {link}", "Operation published: {link}")
_add("op_signup_title", "Выберите слот:", "Choose a slot:")
_add("op_signup_closed", "Запись на эту операцию закрыта.", "Sign-up for this operation is closed.")
_add("op_signup_blocked", "Запись закрыта до {until} из-за повторных неявок.",
     "Sign-up is blocked until {until} because of repeated no-shows.")
_add("op_signup_all_full", "Все слоты заняты.", "All slots are full.")
_add("op_signup_ok", "Вы записаны: {slot}.", "You are signed up: {slot}.")
_add("op_signup_ok_late", "Вы записаны: {slot}. Операция уже идёт, подключайтесь к сбору.",
     "You are signed up: {slot}. The operation is already running, join the muster.")
_add("op_signup_moved", "Слот изменён: {slot}.", "Slot changed: {slot}.")
_add("op_signup_already", "Вы уже записаны в этот слот.", "You are already in this slot.")
_add("op_signup_full", "Этот слот уже заполнен, выберите другой.", "That slot is full; choose another.")
_add("op_left", "Запись отменена.", "Sign-up cancelled.")
_add("op_not_signed", "Вы не записаны на эту операцию.", "You are not signed up for this operation.")
_add("op_brief_none", "У этой операции нет закрытого брифинга.", "This operation has no closed briefing.")
_add("op_brief_need_signup", "Закрытый брифинг доступен только записавшимся.", "The closed briefing is only for signed-up members.")
_add("op_brief_later", "Брифинг откроется за {minutes} минут до старта: <t:{ts}:R>.",
     "The briefing opens {minutes} minutes before the start: <t:{ts}:R>.")
_add("op_brief_title", "Закрытый брифинг", "Closed briefing")
_add("op_manage_title", "Управление операцией #{id}", "Manage operation #{id}")
_add("op_btn_start", "Начать сейчас", "Start now")
_add("op_btn_finish", "Завершить", "Finish")
_add("op_btn_cancel_op", "Отменить операцию", "Cancel operation")
_add("op_btn_urgent_on", "Пометить срочной", "Mark urgent")
_add("op_btn_urgent_off", "Снять «срочно»", "Clear urgent")
_add("op_btn_supply", "Снабжение", "Supply")
_add("op_btn_edit", "Описание", "Edit briefing")
_add("op_started", "Операция началась.", "The operation has started.")
_add("op_finished", "Операция завершена. Явка учтена: {n}. Архивирование поставлено в очередь.", "Operation finished. Attendance recorded: {n}.")
_add("op_cancelled", "Операция отменена.", "Operation cancelled.")
_add("op_state_bad", "Это действие сейчас недоступно.", "That action is not available right now.")
_add("op_supply_title", "Запасы без учёта поставок", "Supply")
_add("op_supply_label", "Итоговые начальные запасы: BMats 1200", "BMats 1200 (set) or BMats +200 (add)")
_add("op_supply_none", "У этой операции нет списка снабжения.", "This operation has no supply list.")
_add("op_supply_ok", "Начальные запасы обновлены. Подтверждённые поставки считаются отдельно.", "Supply updated.")
_add("op_urgent_ok", "Метка обновлена.", "Flag updated.")
_add("op_edit_ok", "Описание обновлено.", "Briefing updated.")
_add("op_noshow_ok", "Неявка отмечена: {user}.", "No-show recorded: {user}.")
_add("op_noshow_missing", "Этот игрок не был записан на операцию.", "That player was not signed up for the operation.")
_add("op_remind_60", "Старт через час. Закрытый брифинг доступен по кнопке под карточкой.",
     "Starting in an hour. The closed briefing is available with the button under the card.")
_add("op_remind_10", "Старт через 10 минут. Собираемся.", "Starting in 10 minutes. Muster up.")
_add("op_auto_closed", "Операция закрыта автоматически: организатор не завершил её вовремя.",
     "The operation was closed automatically: the organizer did not finish it in time.")
_add("op_summary", "Итоги: пришли {n}.", "Summary: {n} attended.")
_add("op_btn_signup", "Записаться", "Sign up")
_add("op_btn_leave", "Отменить запись", "Leave")
_add("op_btn_brief", "Брифинг", "Briefing")
_add("op_btn_manage", "Управление", "Manage")

# -------------------------------------------------------------- priorities --
_add("pri_modal_title", "Цель", "Objective")
_add("pri_f_title_ru", "Заголовок (русский)", "Title (Russian)")
_add("pri_f_title_en", "Title (English)", "Title (English)")
_add("pri_f_body_ru", "Текст (русский)", "Text (Russian)")
_add("pri_f_body_en", "Text (English)", "Text (English)")
_add("pri_saved", "Цель {slot} опубликована.", "Objective {slot} published.")
_add("pri_cleared", "Цель {slot} удалена.", "Objective {slot} removed.")
_add("pri_no_channel", "Канал целей не настроен.", "The objectives channel is not configured.")
_add("pri_none", "Такой цели нет.", "There is no such objective.")

# --------------------------------------------------------------- self roles --
_add("role_added", "Роль добавлена: {role}.", "Role added: {role}.")
_add("role_removed", "Роль снята: {role}.", "Role removed: {role}.")
_add("role_missing", "Роль не настроена. Администратор должен выполнить /setup run.",
     "The role is not configured. An administrator must run /setup run.")


async def user_language(interaction) -> str:
    """The current community uses Russian, regardless of legacy language roles."""
    return 'ru'

_add("err_too_many_places", "Всего не больше {detail} мест на операцию: лимит доступа приватных каналов Discord.",
     "At most {detail} places per operation: Discord private-channel permission limit.")
_add("op_signup_operation_full", "Достигнут лимит операции: 95 участников для приватных каналов Discord.",
     "Operation limit reached: 95 participants for private Discord channels.")
