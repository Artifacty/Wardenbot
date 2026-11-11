# Warden Bot · v0.1

Дискорд-бот для двуязычного (RU/EN) сервера Варденов в Foxhole. Он строит сервер по плану (роли, каналы, права), проводит допуск, ведёт доску операций с записью по слотам, пишет напоминания, публикует цели штаба и повышает игроков по правилам.

**Статус:** ядро (v0.1). Логика, база данных и интерфейс проверены автотестами (54 теста) и сверены с исходниками discord.py 2.x. **Живого запуска против настоящего Discord ещё не было**: первый запуск на тестовом сервере (шаг 4) обязателен, и мелкие шероховатости там возможны. Перечень того, что уже есть и чего пока нет, в конце файла.

## Что умеет v0.1

| Модуль | Что делает |
|---|---|
| `/setup run` | Создаёт 13 ролей, 8 категорий и 22 канала запуска (ещё 10 каналов: `include_later`), форумные теги, права по плану, правило AutoMod против шестизначных кодов складов, публикует правила (RU и EN), стартовое сообщение с кнопками языка и пингов и кнопку допуска. Ничего не удаляет. Повторный запуск безопасен. |
| Допуск | Кнопка → форма (Steam, ник, полк, поручитель) → одноразовый код `WH-XXXX` → `/verify screenshot` → карточка в очереди штаба с кнопками «Допустить / Отклонить / Нужно уточнение». Флаги: тот же Steam у другого аккаунта, молодой аккаунт Discord, прошлый отказ. Скриншот удаляется после решения. |
| Операции | `/operation create` (две формы и предпросмотр) → пост на форуме с карточкой RU · EN и кнопками «Записаться / Отменить запись / Брифинг / Управление». Слоты с лимитами, снабжение с полосой прогресса, закрытый брифинг открывается записавшимся за 60 минут до старта, напоминания за 60 и 10 минут, автостарт, автозакрытие через 6 часов после конца, теги статуса, срочности, типа и языка. |
| Явка и звания | Рекрут после допуска; рядовой через 7 дней и с поручительством сержанта, командира полка или штаба; сержант после 10 посещённых операций. Три неявки подряд закрывают запись на 7 дней. Понижений нет. |
| Цели штаба | `/priority set 1..5` двуязычные посты в канале целей, редактируются на месте. |
| Прочее | `/member admit/rank/info/vouch`, `/timezone set/show`, `/help`, журнал действий в канале штаба, кнопки выбора языка и пингов. |

Ответы бота лично игроку идут на языке его клиента Discord (русский или английский), публичные карточки всегда двуязычные.

## Что понадобится

* Python 3.12 или новее (или Docker).
* Аккаунт Discord, свой тестовый сервер и позже боевой.
* Любой компьютер для проверки, затем сервер, где бот будет работать круглосуточно (см. `docs/HOSTING.md`).

## Шаг 1. Создать приложение и токен

1. Откройте <https://discord.com/developers/applications> → **New Application** → назовите (например, «Warden Bot»).
2. Слева **Bot**. Нажмите **Reset Token** и скопируйте токен. Его видно один раз. Токен это пароль бота: никому не показывайте, в чат и в репозиторий не вставляйте.
3. Там же, в блоке **Privileged Gateway Intents**, включите **Server Members Intent**. **Message Content Intent не нужен**, оставьте выключенным.
4. Слева **Installation** → в **Install Link** выберите **None**, а на странице **Bot** выключите **Public Bot**. Так никто чужой не сможет пригласить вашего бота.
5. Слева **General Information** → скопируйте **Application ID**.

## Шаг 2. Подготовить сервер и пригласить бота

1. Создайте пустой тестовый сервер (потом то же самое повторите на боевом).
2. **Включите режим «Сообщество»**: Настройки сервера → Включить сообщество. Без него нельзя создать форумы. Discord попросит каналы правил и обновлений, выберите любые временные: бот переназначит их сам.
3. Соберите ссылку-приглашение. Права рассчитаны из того, что бот реально делает, **администратора бот не просит**:

   ```
   https://discord.com/oauth2/authorize?client_id=ВАШ_APPLICATION_ID&permissions=1428345187574&scope=bot%20applications.commands
   ```

   Или командой `python -m wardenbot.invite ВАШ_APPLICATION_ID`, она напечатает ту же ссылку.
4. Откройте ссылку, выберите сервер, подтвердите.
5. **Важно:** Настройки сервера → Роли → перетащите роль бота **на самый верх** списка (под вас). Иначе бот не сможет выдавать и упорядочивать свои роли.

## Шаг 3. Запустить бота у себя

Windows (обычная командная строка `cmd`, активировать окружение не нужно):

1. Проверьте, есть ли Python: `py --list`. Нужна версия 3.12 или новее (3.13 тоже подходит). Если пишет «No suitable Python runtime found» или список пуст, установите Python с <https://www.python.org/downloads/windows/> и **поставьте галочку «Add python.exe to PATH»**. Либо: `winget install Python.Python.3.12`. После установки откройте командную строку заново.
2. Дальше по порядку:

```bat
cd /d E:\wardenbot
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
notepad .env
.venv\Scripts\python.exe -m wardenbot
```

`py -m venv` без номера версии берёт самый новый установленный Python. Команды `Activate.ps1` и `activate` не нужны: мы вызываем `python.exe` из `.venv` напрямую. (`Activate.ps1` работает только в PowerShell и при разрешённых скриптах, а в `cmd` он не запускается вовсе.)

Linux / macOS:

```bash
cd wardenbot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
python -m wardenbot
```

В `.env` впишите минимум:

```
DISCORD_TOKEN=токен_из_шага_1
DEV_GUILD_ID=ID_тестового_сервера
```

ID сервера: в Discord включите «Режим разработчика» (Настройки → Расширенные), правый клик по иконке сервера → «Копировать ID сервера».

Успешный запуск выглядит так:

```
INFO  wardenbot.bot: Synced 7 slash commands (dev guild)
INFO  wardenbot.bot: Logged in as Warden Bot#1234 (…) in 1 guild(s)
```

С `DEV_GUILD_ID` команды появляются мгновенно. Без него это глобальные команды: первый раз они могут появляться до часа.

## Шаг 4. Первая настройка и проверка (на тестовом сервере)

1. В любом канале выполните `/setup run`. Займёт 1–3 минуты. Бот ответит списком созданного. Если что-то не вышло, он скажет, что именно.
2. Проверьте `/setup status`: у всех ролей и каналов должна стоять галочка.
3. Назначьте себе роль **◆ Штаб · Staff** (или **★ Командование · Command**) вручную. Права администратора Discord у вас и так есть, но роли бота выдаёт и проверяет именно по ним.
4. Прогон по сценарию (удобно вторым аккаунтом или с другом):
   * Второй аккаунт нажимает кнопку допуска в канале допуска → заполняет форму → выполняет `/verify screenshot` → вы одобряете в канале очереди → аккаунт получает роль Рекрут и видит операции.
   * `/operation create` → тип, язык → заполнить обе формы → «Опубликовать». В форуме появится пост.
   * Второй аккаунт нажимает «Записаться», выбирает слот. Карточка обновляется через пару секунд.
   * В управлении операцией проверьте «Начать сейчас», снабжение (`BMats +200`), «Завершить».
   * `/priority set 1` публикует первую цель в канале целей.
5. Если нужны поздние каналы (снабжение, отчёты, гайды, голосовые): `/setup run include_later:True`.
6. Если вы меняли права вручную и хотите вернуть как в плане: `/setup run fix_permissions:True`. Это перезаписывает права существующих каналов.

На боевом сервере повторите шаги 2.2–4.1, затем уберите `DEV_GUILD_ID` или оставьте пустым.

## Шаг 5. Запуск на постоянной основе

Бот должен работать круглосуточно. Подбор сервера и цены: `docs/HOSTING.md`.

### Вариант А: Docker (проще всего)

```bash
git clone … wardenbot && cd wardenbot      # или скопируйте папку на сервер
cp .env.example .env && nano .env           # токен; DEV_GUILD_ID оставьте пустым
docker compose up -d --build
docker compose logs -f
```

База лежит в томе `wardenbot-data` и переживает пересборки.

### Вариант Б: systemd (Linux без Docker)

```bash
sudo useradd --system --create-home --home-dir /opt/wardenbot wardenbot
sudo cp -r wardenbot tools requirements.txt .env.example /opt/wardenbot/ && cd /opt/wardenbot
sudo -u wardenbot python3.12 -m venv .venv
sudo -u wardenbot .venv/bin/pip install -r requirements.txt
sudo -u wardenbot cp .env.example .env && sudo -u wardenbot nano .env && sudo chmod 600 .env
sudo -u wardenbot mkdir -p data backups
sudo cp deploy/wardenbot*.service deploy/wardenbot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wardenbot wardenbot-backup.timer
journalctl -u wardenbot -f
```

### Резервные копии

База хранит заявки, звания, операции и явку. Копия делается, пока бот работает:

```bash
python tools/backup.py data/wardenbot.db backups     # хранит 14 последних
```

В варианте systemd это уже делает `wardenbot-backup.timer` (каждый день в 04:15). Для Docker:
`docker compose exec wardenbot python tools/backup.py /data/wardenbot.db /data/backups`, затем скопируйте папку с сервера к себе. **Копию надо хранить не только на том же сервере.**

### Обновление

```bash
# Docker
git pull && docker compose up -d --build
# systemd
sudo systemctl stop wardenbot && (обновить файлы) && sudo -u wardenbot .venv/bin/pip install -r requirements.txt && sudo systemctl start wardenbot
```

Схема базы обновляется сама (`PRAGMA user_version`). Перед обновлением сделайте копию.

## Настройки (`.env`)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DISCORD_TOKEN` | нет | токен бота (обязательно) |
| `DEV_GUILD_ID` | пусто | сервер для мгновенных команд при тестировании |
| `DB_PATH` | `data/wardenbot.db` | путь к базе |
| `DEFAULT_TZ` | `Europe/Moscow` | пояс по умолчанию для времени операций |
| `CHANNEL_NAME_STYLE` | `full` | `full`: `⬢┃правила┃rules📜`, `ru`, `en` (меняет только новые каналы) |
| `PRIVATE_AFTER_DAYS` | 7 | дней до «Рядового» (плюс поручительство) |
| `SERGEANT_AFTER_OPS` | 10 | операций до «Сержанта» |
| `NOSHOW_LIMIT` / `NOSHOW_BLOCK_DAYS` | 3 / 7 | неявок подряд / дней блокировки записи |
| `YOUNG_ACCOUNT_DAYS` | 14 | аккаунт моложе этого срока помечается флагом |
| `REJECT_COOLDOWN_HOURS` | 24 | пауза после отказа в допуске |
| `BRIEFING_OPEN_MINUTES` | 60 | за сколько минут до старта открывается закрытый брифинг |
| `AUTO_CLOSE_HOURS` | 6 | через сколько часов после конца автоматически закрыть забытую операцию |

## Если что-то не работает

| Симптом | Причина и решение |
|---|---|
| `Configuration error: DISCORD_TOKEN is not set` | Нет файла `.env` рядом с местом запуска или токен пуст. |
| `LoginFailure: Improper token` | Токен скопирован с лишним пробелом или устарел: Reset Token и вставьте заново. |
| `PrivilegedIntentsRequired` | Не включён Server Members Intent (шаг 1.3). |
| Бот не подключается, таймауты | Discord заблокирован в России с октября 2024 года ([3DNews](https://3dnews.ru/1113647/)). Запускайте бота на сервере за пределами РФ; с домашнего компьютера в РФ нужен VPN. Актуальный статус проверьте сами. |
| Команд нет в списке | Нужен перезапуск после правок; без `DEV_GUILD_ID` новые глобальные команды появляются до часа. Проверьте, что при приглашении был scope `applications.commands`. |
| `/setup run`: «Форумы доступны только в режиме Сообщество» | Шаг 2.2. |
| `/setup run`: «Боту не хватает прав» | Приглашайте заново по ссылке из шага 2.3. |
| Роли не упорядочились | Роль бота надо поднять выше (шаг 2.5) и повторить `/setup run`. |
| Правило AutoMod не создано | Создайте вручную: Настройки сервера → Автомодерация → Создать правило → Ключевые слова → Регулярное выражение `\b\d{6}\b`, действие «Блокировать сообщение», исключения: каналы штаба, допуска и роли Штаб и Командование. |
| Кнопки под старыми карточками «не отвечают» | Бот был выключен. После запуска кнопки оживают сами, повторно ничего публиковать не нужно. |
| Бот отвечает «Что-то пошло не так» | Подробности в логах (`docker compose logs` или `journalctl -u wardenbot`) и в канале «бот» (только Командование). |

## Безопасность и данные

* Бот хранит ссылку на Steam, игровой ник, полк, Discord ID, звания и явку. Скриншоты допуска **не хранятся в базе**, а лишь временно лежат в закрытом канале очереди и удаляются из карточки после решения. Напишите об этом в правилах (в тексте правил это уже есть).
* Токен хранится только в `.env` (`chmod 600`) и не должен попадать в Git. Файл `.gitignore` это учитывает. Если токен утёк: Developer Portal → Reset Token.
* У бота нет прав администратора. Роль «Командование» тоже не администратор Discord: она получает только модераторские права и право банить.
* Защита от вторых аккаунтов в v0.1 держится на допуске: скриншот с кодом, один Steam на один Discord, флаги риска, ручное решение контрразведки. Бот не умеет проверять фракцию в игре автоматически (такого API нет).

## Что ещё не сделано (дорожная карта)

**v0.2:** снабжение и запросы по операциям отдельными карточками; доска объявлений о продаже и покупке со сроком жизни и закрытием; карточки наборов полков; ежедневная сводка в канал «сводка»; подтверждение явки кнопкой самим игроком; сброс фракции при новой войне.
**v0.3:** перевод сообщений между парными каналами (RU ↔ EN), временные голосовые каналы операций, статус войны, жалобы и апелляции, статистика по полкам.

## Структура проекта

```
wardenbot/
  __main__.py      точка входа
  bot.py           класс бота, синхронизация команд
  cogs/            setup_cog, verification, operations, priorities, members, selfroles, housekeeping
  layout.py        роли, категории и каналы по плану (данные)
  perm_layout.py   права каналов
  invite.py        расчёт прав бота и ссылка-приглашение
  repo.py db.py    база SQLite
  render.py        карточка операции
  opsplan.py       что делать по таймеру (чистая функция)
  ranks.py slots.py steam.py timeutil.py codeword.py   чистая логика
  i18n.py          строки RU/EN
  texts/           правила RU и EN
tests/             автотесты (pytest)
tools/backup.py    резервная копия базы
deploy/            unit-файлы systemd
docs/HOSTING.md    где разместить бота
```

Тесты: `pip install -r requirements-dev.txt && pytest -q`. Часть тестов сверяет интерфейс с установленным discord.py (лимиты Discord на длину подписей, права, сквозная запись на операцию).

---

## English summary

A bilingual (RU/EN) Discord bot for a Warden-side Foxhole community server: builds the server (roles, channels, permissions, forum tags, AutoMod), verifies new members (Steam link + one-time code + screenshot + staff review), runs an operations board (slots, supply progress, closed briefing, reminders, auto start/close), publishes HQ objectives and promotes members by rules.

Quick start: create an application in the Developer Portal, enable **Server Members Intent**, invite the bot with `python -m wardenbot.invite <APPLICATION_ID>` (no Administrator needed), enable **Community** on the server, move the bot role to the top, `cp .env.example .env`, set `DISCORD_TOKEN` (and `DEV_GUILD_ID` for testing), `pip install -r requirements.txt`, `python -m wardenbot`, then run `/setup run`. Docker and systemd files are included. Hosting options: `docs/HOSTING.md`.

Status: v0.1 core, covered by 54 automated tests and checked against the discord.py 2.x source, but not yet exercised against live Discord. Run it on a test server first.
