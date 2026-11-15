"""Private, role-localized navigation without duplicate public command sets."""
import discord
from discord import app_commands
from discord.ext import commands
from .. import repo
from ..i18n import user_language, t
from ..opsmeta import OP_TYPES, LANGS
from ..services import reply, channel_mention, report_error


class PersonalView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        user = await interaction.client.db.read(repo.user_get, interaction.user.id)
        if interaction.user.id != self.owner_id or not user or user['status'] != 'admitted':
            await reply(interaction, t('err_no_perm', await user_language(interaction)))
            return False
        return True

    async def on_error(self, interaction, error, item):
        await report_error(interaction.client, interaction, error, 'personal menu')


class OperationOptions(PersonalView):
    def __init__(self, owner_id, lang):
        super().__init__(owner_id)
        self.kind = discord.ui.Select(placeholder='Тип операции' if lang == 'ru' else 'Operation type',
            options=[discord.SelectOption(label=v.get(lang), value=k) for k,v in OP_TYPES.items()])
        self.communication = discord.ui.Select(placeholder='Язык общения' if lang == 'ru' else 'Communication language',
            options=[discord.SelectOption(label=v.get(lang), value=k, default=k == lang) for k,v in LANGS.items()])
        self.selected_type = None
        self.selected_language = lang
        self.kind.callback = self.pick_type
        self.communication.callback = self.pick_language
        self.add_item(self.kind)
        self.add_item(self.communication)
        button = discord.ui.Button(label='Продолжить' if lang == 'ru' else 'Continue', style=discord.ButtonStyle.primary)
        button.callback = self.open_form
        self.add_item(button)

    async def pick_type(self, interaction):
        self.selected_type = self.kind.values[0]
        await interaction.response.defer()

    async def pick_language(self, interaction):
        self.selected_language = self.communication.values[0]
        await interaction.response.defer()

    async def open_form(self, interaction):
        lang = await user_language(interaction)
        if self.selected_type is None:
            await reply(interaction, 'Выберите тип операции.' if lang == 'ru' else 'Choose an operation type.')
            return
        cog = interaction.client.get_cog('OperationsCog')
        await cog.create.callback(cog, interaction,
            app_commands.Choice(name=self.selected_type, value=self.selected_type),
            app_commands.Choice(name=self.selected_language, value=self.selected_language))


class MenuView(PersonalView):
    def __init__(self, owner_id, lang):
        super().__init__(owner_id)
        for ru, en, handler in [('Создать операцию','Create operation',self.create),
                                ('Доска операций','Operations board',self.board),
                                ('Язык','Language',self.language),('Помощь','Help',self.help)]:
            button = discord.ui.Button(label=ru if lang == 'ru' else en)
            button.callback = handler
            self.add_item(button)

    async def create(self, interaction):
        lang = await user_language(interaction)
        await reply(interaction, 'Параметры операции' if lang == 'ru' else 'Operation options',
                    view=OperationOptions(interaction.user.id, lang))

    async def board(self, interaction):
        await reply(interaction, channel_mention(interaction.client, interaction.guild.id, 'ops_board'))

    async def language(self, interaction):
        from .selfroles import LanguageView
        await reply(interaction, 'Russian / English', view=LanguageView())

    async def help(self, interaction):
        await reply(interaction, t('help_text', await user_language(interaction)))


class MenuCog(commands.Cog):
    @app_commands.command(name='menu', description='Open your personal menu')
    @app_commands.guild_only()
    async def menu(self, interaction):
        user = await interaction.client.db.read(repo.user_get, interaction.user.id)
        if not user or user['status'] != 'admitted':
            from .verification import build_verify_view
            await reply(interaction, 'Complete verification first.', view=build_verify_view())
            return
        lang = await user_language(interaction)
        await reply(interaction, 'Ваше меню' if lang == 'ru' else 'Your menu', view=MenuView(interaction.user.id, lang))


async def setup(bot):
    await bot.add_cog(MenuCog())
