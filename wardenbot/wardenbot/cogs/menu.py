"""Personal menu for the Russian community."""
import re
import discord
from discord import app_commands
from discord.ext import commands
from .. import repo
from ..perms import can_staff
from ..i18n import user_language, t
from ..opsmeta import OP_TYPES
from ..services import interaction_errors, reply, channel_mention, report_error


class PersonalView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        staff = isinstance(interaction.user, discord.Member) and can_staff(interaction.client, interaction.user)
        user = None if staff else await interaction.client.db.read(repo.user_get, interaction.user.id)
        if interaction.user.id != self.owner_id or (not staff and (not user or user['status'] != 'admitted')):
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
        self.selected_type = None
        self.kind.callback = self.pick_type
        self.add_item(self.kind)
        button = discord.ui.Button(label='Продолжить' if lang == 'ru' else 'Continue', style=discord.ButtonStyle.primary)
        button.callback = self.open_form
        self.add_item(button)

    async def pick_type(self, interaction):
        self.selected_type = self.kind.values[0]
        await interaction.response.defer()

    async def open_form(self, interaction):
        lang = await user_language(interaction)
        if self.selected_type is None:
            await reply(interaction, 'Выберите тип операции.' if lang == 'ru' else 'Choose an operation type.')
            return
        cog = interaction.client.get_cog('OperationsCog')
        await cog.create.callback(cog, interaction,
            app_commands.Choice(name=self.selected_type, value=self.selected_type))


class MenuButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wb:menu:(?P<owner>\d+):(?P<action>create|board|help|verify)"):
    LABELS = {'create': 'Создать операцию', 'board': 'Доска операций', 'help': 'Помощь', 'verify': 'Пройти проверку'}

    def __init__(self, owner, action):
        super().__init__(discord.ui.Button(label=self.LABELS[action], custom_id=f'wb:menu:{owner}:{action}'))
        self.owner, self.action = owner, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['owner']), match['action'])

    @interaction_errors
    async def callback(self, interaction):
        if interaction.user.id != self.owner:
            await reply(interaction, 'Откройте собственное меню командой /menu.')
            return
        if self.action == 'verify':
            from .verification import begin_verification
            await begin_verification(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        staff = isinstance(interaction.user, discord.Member) and can_staff(interaction.client, interaction.user)
        user = None if staff else await interaction.client.db.read(repo.user_get, interaction.user.id)
        if self.action != 'help' and not staff and (not user or user['status'] != 'admitted'):
            await reply(interaction, 'Допуск отсутствует или отозван. Откройте /menu для проверки.')
            return
        if self.action == 'create':
            await reply(interaction, 'Параметры операции', view=OperationOptions(self.owner, 'ru'))
        elif self.action == 'board':
            await reply(interaction, channel_mention(interaction.client, interaction.guild.id, 'ops_board'))
        else:
            await reply(interaction, t('help_text', 'ru'))

    async def on_error(self, interaction, error):
        await report_error(interaction.client, interaction, error, 'menu button')


class MenuView(discord.ui.View):
    def __init__(self, owner_id, lang='ru', admitted=True):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        for action in (('create', 'board', 'help') if admitted else ('verify', 'help')):
            self.add_item(MenuButton(owner_id, action))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.owner_id


class MenuCog(commands.Cog):
    async def cog_load(self):
        self.bot.add_dynamic_items(MenuButton)

    @app_commands.command(name='menu', description='Открыть персональное меню')
    @app_commands.guild_only()
    async def menu(self, interaction: discord.Interaction):
        # Discord requires an acknowledgement within 3 seconds, including on a busy SQLite DB.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await reply(interaction, 'Меню доступно на сервере.')
            return
        staff = can_staff(interaction.client, interaction.user)
        user = None if staff else await interaction.client.db.read(repo.user_get, interaction.user.id)
        admitted = staff or bool(user and user['status'] == 'admitted')
        await reply(interaction, 'Ваше меню' if admitted else 'Для доступа к операциям пройдите проверку.',
                    view=MenuView(interaction.user.id, admitted=admitted))


async def setup(bot):
    cog = MenuCog()
    cog.bot = bot
    await bot.add_cog(cog)
