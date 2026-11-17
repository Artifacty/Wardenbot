"""Private resource pledge forms; delivery is confirmed by operation management."""
import discord
from .. import operation_service as ops
from ..services import reply, utcnow, report_error


class PledgeModal(discord.ui.Modal):
    def __init__(self,cog,op_id,supply_id,name):
        super().__init__(title='Обещать ресурсы')
        self.cog,self.op_id,self.supply_id=cog,op_id,supply_id
        self.amount=discord.ui.TextInput(label=f'{name}: итоговое количество (0 — отмена)'[:45],max_length=8)
        self.add_item(self.amount)

    async def on_submit(self,interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            value=int(self.amount.value)
            await self.cog.mutate(interaction,self.op_id,ops.pledge,self.supply_id,value,utcnow())
        except (ValueError,ops.OperationError) as exc:
            await reply(interaction,str(exc) if isinstance(exc,ops.OperationError) else 'Введите целое количество.'); return
        self.cog.request_refresh(self.op_id)
        await reply(interaction,'Обещание сохранено. Доставку отдельно подтверждает организатор.')

    async def on_error(self,interaction,error):
        await report_error(self.cog.bot,interaction,error,'pledge')


class SupplyPicker(discord.ui.View):
    def __init__(self,cog,op_id,items):
        super().__init__(timeout=300)
        self.cog,self.op_id=cog,op_id
        self.items={str(x['id']):x['name'] for x in items}
        self.select=discord.ui.Select(placeholder='Что вы готовы привезти?',options=[discord.SelectOption(label=x['name'],value=str(x['id'])) for x in items])
        self.select.callback=self.choose
        self.add_item(self.select)

    async def choose(self,interaction):
        key=self.select.values[0]
        await interaction.response.send_modal(PledgeModal(self.cog,self.op_id,int(key),self.items[key]))

    async def on_error(self,interaction,error,item):
        await report_error(self.cog.bot,interaction,error,'supply picker')
