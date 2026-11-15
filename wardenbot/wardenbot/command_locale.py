"""Keep command names ASCII; translate only descriptions for Russian clients."""
from discord import app_commands
import discord

class CommandTranslator(app_commands.Translator):
    async def translate(self, string, locale, context):
        if context.location.name.endswith('_name'):
            return None
        if locale == discord.Locale.russian and ' · ' in str(string):
            parts = str(string).split(' · ')
            return next((p for p in parts if any('А' <= c <= 'я' or c in 'Ёё' for c in p)), None)
        return None
