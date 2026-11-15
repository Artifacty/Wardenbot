"""Native slash-command localization. Discord selects these by client locale, not role."""
import re
import discord
from discord import app_commands

NAMES = {
    'menu': 'меню', 'language': 'язык', 'help': 'помощь', 'operation': 'операция',
    'create': 'создать', 'noshow': 'неявка', 'attendance': 'участие', 'member': 'участник',
    'admit': 'допустить', 'rank': 'роль', 'info': 'данные', 'vouch': 'поручиться',
    'timezone': 'часовой-пояс', 'set': 'установить', 'show': 'показать', 'priority': 'цель',
    'clear': 'убрать', 'clan': 'клан', 'register': 'создать', 'assign': 'назначить',
    'remove': 'снять', 'player': 'игрок', 'type': 'тип', 'attended': 'присутствовал',
    'zone': 'пояс', 'slot': 'номер', 'name': 'название', 'duty': 'полномочия',
}
DESCRIPTIONS = {
    'Open your personal menu': 'Открыть персональное меню',
    'Choose your language role': 'Выбрать языковую роль',
    'Confirm actual participation or correct a completed operation': 'Подтвердить участие или исправить итог операции',
    'Clans and their representatives': 'Кланы и их представители',
    'Register a clan': 'Создать запись клана',
    'Assign a clan representative': 'Назначить представителя клана',
    'Remove a clan representative': 'Снять полномочия представителя клана',
}


class CommandTranslator(app_commands.Translator):
    async def translate(self, string, locale, context):
        if locale not in (discord.Locale.russian, discord.Locale.american_english, discord.Locale.british_english):
            return None
        data = context.data
        command = data.command if isinstance(data, app_commands.Parameter) else data
        while isinstance(command, (app_commands.Command, app_commands.Group)) and command.parent:
            command = command.parent
        # The entry flow stays English even for a Russian Discord client.
        if getattr(command, 'name', None) == 'verify':
            return None
        raw = str(string)
        russian = locale == discord.Locale.russian
        if context.location.name.endswith('_name') and context.location.name != 'choice_name':
            return NAMES.get(raw) if russian else None
        if russian and raw in DESCRIPTIONS:
            return DESCRIPTIONS[raw]
        if ' · ' in raw:
            parts = raw.split(' · ')
            matches = [p for p in parts if bool(re.search('[А-Яа-яЁё]', p)) == russian]
            return matches[0] if matches else None
        return None
