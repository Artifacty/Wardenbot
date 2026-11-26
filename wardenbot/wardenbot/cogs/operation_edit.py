"""Operation forms delegate atomic checks and persistence to the application service."""
import discord
from .. import repo, operation_service as ops
from ..slots import parse_slots, parse_supply
from ..opsmeta import OP_TYPES
from ..timeutil import ParseError, parse_local_datetime, resolve_tz, from_iso, to_iso, parse_duration_hours, display_tz
from ..services import reply, utcnow, report_error, evaluate_member

FIELDS = ('title','region','type','start_at','timezone','duration_min','requirements','briefing_public','briefing_closed')


class Editor(discord.ui.Modal):
    def __init__(self,cog,op,slots,supply,section):
        super().__init__(title={'basics':'Основное','schedule':'Время операции','details':'План, места и ресурсы'}[section],timeout=900)
        self.cog,self.op,self.slots,self.supply,self.section = cog,op,slots,supply,section
        self.inputs = {}
        if section=='basics':
            specs=[('title','Название',op['title'],80),('region','Регион',op['region'],40),
                   ('type','Тип: offense/defense/logistics/naval/partisan/training',op['type'],30),
                   ('requirements','Требования',op['requirements'],400)]
        elif section=='schedule':
            specs=[('start','Начало: дата и время / завтра 20:30',from_iso(op['start_at']).astimezone(resolve_tz(op['timezone'])[0]).strftime('%Y-%m-%d %H:%M'),40),
                   ('timezone','Часовой пояс (МСК, UTC+3)',display_tz(op['timezone']),50),
                   ('duration','Длительность, часов',str(op['duration_min']/60),12)]
        else:
            specs=[('briefing_public','Публичное описание',op['briefing_public'],1500),
                   ('briefing_closed','Закрытый брифинг и точка сбора',op['briefing_closed'],1500),
                   ('slots','Роли: название и число мест','\n'.join(f"{r['name']}: {r['capacity']}" for r in slots),600),
                   ('supply','Нужно ресурсов: название и количество','\n'.join(f"{r['name']}: {r['needed']}" for r in supply),600)]
        for key,label,value,size in specs:
            item=discord.ui.TextInput(label=label[:45],default=value,max_length=size,
                required=key in ('title','type','start','timezone','duration','slots'),
                placeholder='завтра 20:30 / через 30 минут / 25.11 19:00' if key=='start' else None,
                style=discord.TextStyle.paragraph if size>300 else discord.TextStyle.short)
            self.inputs[key]=item
            self.add_item(item)

    async def on_submit(self,interaction):
        await interaction.response.defer(ephemeral=True)
        fields={k:self.op[k] for k in FIELDS}
        slots=parse_slots('\n'.join(f"{r['name']}: {r['capacity']}" for r in self.slots))
        supply=parse_supply('\n'.join(f"{r['name']}: {r['needed']}" for r in self.supply))
        values={k:v.value.strip() for k,v in self.inputs.items()}
        try:
            if self.section=='schedule':
                zone,name=resolve_tz(values['timezone'])
                # Keeping the displayed start is allowed for a running operation.
                previous=from_iso(self.op['start_at']).astimezone(zone).strftime('%Y-%m-%d %H:%M')
                fields['start_at']=self.op['start_at'] if values['start']==previous else to_iso(parse_local_datetime(values['start'],zone,utcnow()))
                fields.update(timezone=name,duration_min=parse_duration_hours(values['duration']))
            elif self.section=='basics':
                fields.update(values)
            else:
                slots=parse_slots(values.pop('slots'))
                supply=parse_supply(values.pop('supply'))
                fields.update(values)
            await self.cog.mutate(interaction,self.op['id'],ops.edit,self.op['revision'],fields,slots,supply,utcnow())
        except (ops.OperationError,ParseError) as exc:
            await reply(interaction,str(exc) if isinstance(exc,ops.OperationError) else f'Проверьте заполнение формы: {exc.code}.')
            return
        self.cog.request_refresh(self.op['id'])
        await reply(interaction,'Изменения сохранены. Участники получат публичное уведомление. Для следующего изменения откройте управление заново.')

    async def on_error(self,interaction,error):
        await report_error(self.cog.bot,interaction,error,'operation editor')


class CancelModal(discord.ui.Modal):
    def __init__(self,cog,op_id):
        super().__init__(title='Подтвердить отмену операции')
        self.cog,self.op_id=cog,op_id
        self.reason=discord.ui.TextInput(label='Публичная причина (без закрытых сведений)',max_length=500,style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self,interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.mutate(interaction,self.op_id,ops.cancel,self.reason.value,utcnow())
        except ops.OperationError as exc:
            await reply(interaction,str(exc)); return
        self.cog.request_refresh(self.op_id)
        await reply(interaction,'Операция отменена. Уведомление и архивирование поставлены в очередь.')

    async def on_error(self,interaction,error):
        await report_error(self.cog.bot,interaction,error,'cancel operation')


class FinishView(discord.ui.View):
    def __init__(self,cog,op_id):
        super().__init__(timeout=300)
        self.cog,self.op_id=cog,op_id

    @discord.ui.button(label='Подтвердить завершение',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        await interaction.response.defer(ephemeral=True)
        try:
            attendees=await self.cog.mutate(interaction,self.op_id,ops.finish,utcnow())
        except ops.OperationError as exc:
            await reply(interaction,str(exc)); return
        await interaction.edit_original_response(content=f'Операция завершена. Зачтено участников: {len(attendees)}. Архивирование в очереди.',view=None)
        for uid in attendees:
            await evaluate_member(self.cog.bot,interaction.guild,uid)
        self.cog.request_refresh(self.op_id)

    async def on_error(self,interaction,error,item):
        await report_error(self.cog.bot,interaction,error,'finish operation')
