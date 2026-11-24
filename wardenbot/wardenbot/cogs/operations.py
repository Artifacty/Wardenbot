"""Operations: create (two modals + preview), the persistent card buttons, reminders and the tick loop."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import repo, clans, archives, notifications, operation_service as ops
from .operation_edit import Editor, CancelModal, FinishView
from .operation_supply import SupplyPicker
from ..operation_archive import OperationArchiver
from ..bot import WardenBot
from ..i18n import user_language, parse_error_text, t
from ..opsmeta import (
    DEFAULT_SLOTS, DEFAULT_SUPPLY, DONE, LANG_TAG, LANGS, OP_TYPES, OPEN, RUNNING,
    STATUS_TAG, TYPE_TAG, URGENT_TAG, CLOSED_SIGNUP_TAG,
)
from ..opsplan import Action, OpTimes, plan_actions
from ..perms import member_role_keys, can_staff, is_admitted
from ..ranks import noshow_block_until
from ..render import Card, OpView, SlotView, SupplyView, build_card, thread_title
from ..services import interaction_errors, audit, channel_mention, evaluate_member, get_role, get_text_channel, report_error, reply, utcnow
from ..slots import SlotSpec, SupplySpec, parse_slots, parse_supply, parse_supply_progress
from ..timeutil import ParseError, from_iso, parse_duration_hours, parse_local_datetime, resolve_tz, tz_from_name

log = logging.getLogger(__name__)
DRAFT_TTL = timedelta(minutes=45)
NO_SUPPLY_TYPES = {"training", "partisan"}


# ------------------------------------------------------------------ embeds --
def card_embed(card: Card) -> discord.Embed:
    embed = discord.Embed(title=card.title, description=card.description or None, color=card.color)
    for f in card.fields:
        embed.add_field(name=f.name, value=f.value, inline=f.inline)
    embed.set_footer(text=card.footer)
    return embed


def load_view(conn, op_id: int) -> OpView | None:
    op = repo.op_get(conn, op_id)
    if op is None:
        return None
    signups = repo.op_signups(conn, op_id)
    slots = []
    for s in repo.op_slots(conn, op_id):
        members = [r["user_id"] for r in signups if r["slot_id"] == s["id"]]
        slots.append(SlotView(s["id"], s["name"], s["capacity"], members))
    supply = [SupplyView(r["name"], r["needed"], r["prepared"], r["promised"]) for r in ops.supply_totals(conn, op_id)]
    return OpView(
        id=op["id"], title=op["title"], region=op["region"], type=op["type"], language=op["language"],
        start_at=from_iso(op["start_at"]), duration_min=op["duration_min"], organizer_id=op["organizer_id"],
        requirements=op["requirements"], briefing_public=op["briefing_public"], status=op["status"],
        urgent=bool(op["urgent"]), slots=slots, supply=supply, signup_open=bool(op["signup_open"]),
    )


def wanted_tag_names(op: OpView) -> list[str]:
    names = [STATUS_TAG[op.status]]
    if op.status in (OPEN, RUNNING):
        recruiting = STATUS_TAG[OPEN] if op.signup_open else CLOSED_SIGNUP_TAG
        if op.status == OPEN:
            names = [recruiting]
        else:
            names.append(recruiting)
    if op.urgent and op.status in (OPEN, RUNNING):
        names.append(URGENT_TAG)
    names.append(TYPE_TAG[op.type])
    names.append(LANG_TAG[op.language])
    return names


# ------------------------------------------------------------------ drafts --
@dataclass
class Draft:
    guild_id: int
    organizer_id: int
    type: str
    language: str
    tz_name: str
    created: datetime
    clan_id: int | None = None
    raw1: dict[str, str] = field(default_factory=dict)
    raw2: dict[str, str] = field(default_factory=dict)
    start_at: datetime | None = None
    duration_min: int | None = None
    slots: list[SlotSpec] = field(default_factory=list)
    supply: list[SupplySpec] = field(default_factory=list)
    publication_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    publish_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    published_url: str | None = None

    def preview_view(self) -> OpView:
        return OpView(
            id=0, title=self.raw1.get("title", ""), region=self.raw1.get("region", ""), type=self.type,
            language=self.language, start_at=self.start_at, duration_min=self.duration_min or 60,  # type: ignore[arg-type]
            organizer_id=self.organizer_id, requirements=self.raw1.get("requirements", ""),
            briefing_public=self.raw2.get("public", ""), status=OPEN,
            slots=[SlotView(0, s.name, s.capacity) for s in self.slots],
            supply=[SupplyView(s.name, s.needed) for s in self.supply],
        )


async def respond_draft(
    interaction: discord.Interaction, content: str, embed: discord.Embed | None, view: discord.ui.View | None,
) -> None:
    if interaction.message is not None and not interaction.response.is_done():
        await interaction.response.edit_message(content=content, embed=embed, view=view)
        return
    kwargs: dict = {"view": view} if view is not None else {}
    if embed is not None:
        kwargs["embed"] = embed
    await reply(interaction, content, **kwargs)


class BasicsModal(discord.ui.Modal):
    def __init__(self, cog: "OperationsCog", draft: Draft, lang: str) -> None:
        super().__init__(title=f"Операция · {draft.tz_name}"[:45], timeout=900)
        self.cog, self.draft, self.lang = cog, draft, lang
        raw = draft.raw1
        self.f_title = discord.ui.TextInput(label=t("op_f_title", lang), default=raw.get("title"), max_length=80)
        self.f_region = discord.ui.TextInput(label=t("op_f_region", lang), default=raw.get("region"), required=False, max_length=40)
        self.f_start = discord.ui.TextInput(
            label="Когда начинаем? Например: завтра 20:30", default=raw.get("start"), max_length=40, placeholder="сегодня 20:30 / завтра 19:00 / через 30 минут",
        )
        self.f_duration = discord.ui.TextInput(label=t("op_f_duration", lang), default=raw.get("duration") or "2", placeholder="2 = два часа; 90m = полтора часа", max_length=12)
        self.f_req = discord.ui.TextInput(
            label=t("op_f_requirements", lang), default=raw.get("requirements"), required=False,
            style=discord.TextStyle.paragraph, max_length=400,
        )
        for item in (self.f_title, self.f_region, self.f_start, self.f_duration, self.f_req):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        d, lang = self.draft, self.lang
        d.raw1 = {
            "title": self.f_title.value.strip(), "region": self.f_region.value.strip(),
            "start": self.f_start.value.strip(), "duration": self.f_duration.value.strip(),
            "requirements": self.f_req.value.strip(),
        }
        try:
            d.start_at = parse_local_datetime(d.raw1["start"], tz_from_name(d.tz_name), utcnow())
            d.duration_min = parse_duration_hours(d.raw1["duration"])
        except ParseError as exc:
            d.start_at = None
            await respond_draft(interaction, parse_error_text(exc, lang), None, DraftView(self.cog, d, lang, "basics"))
            return
        if d.slots:
            await self.cog.show_preview(interaction, d, lang)
        else:
            await respond_draft(
                interaction, t("op_draft_basic", lang, tz=d.tz_name), None, DraftView(self.cog, d, lang, "details"),
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, "operation basics modal")  # type: ignore[arg-type]


class DetailsModal(discord.ui.Modal):
    def __init__(self, cog: "OperationsCog", draft: Draft, lang: str) -> None:
        super().__init__(title=t("op_modal2_title", lang), timeout=900)
        self.cog, self.draft, self.lang = cog, draft, lang
        raw = draft.raw2
        default_slots = raw.get("slots") or DEFAULT_SLOTS.get(draft.type, "")
        default_supply = raw.get("supply") if "supply" in raw else ("" if draft.type in NO_SUPPLY_TYPES else DEFAULT_SUPPLY)
        para = discord.TextStyle.paragraph
        self.f_public = discord.ui.TextInput(label=t("op_f_public", lang), default=raw.get("public"), required=False, style=para, max_length=1500)
        self.f_closed = discord.ui.TextInput(label=t("op_f_closed", lang), default=raw.get("closed"), required=False, style=para, max_length=1500)
        self.f_slots = discord.ui.TextInput(label=t("op_f_slots", lang), default=default_slots[:600], style=para, max_length=600, placeholder="Пехота: 10\nТанкист: 6")
        self.f_supply = discord.ui.TextInput(label=t("op_f_supply", lang), default=default_supply[:300], required=False, style=para, max_length=300, placeholder="Бинты: 100\nДизель: 50")
        for item in (self.f_public, self.f_closed, self.f_slots, self.f_supply):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        d, lang = self.draft, self.lang
        d.raw2 = {
            "public": self.f_public.value.strip(), "closed": self.f_closed.value.strip(),
            "slots": self.f_slots.value.strip(), "supply": self.f_supply.value.strip(),
        }
        try:
            d.slots = parse_slots(d.raw2["slots"])
            d.supply = parse_supply(d.raw2["supply"]) if d.raw2["supply"] else []
        except ParseError as exc:
            d.slots = []
            await respond_draft(interaction, parse_error_text(exc, lang), None, DraftView(self.cog, d, lang, "details"))
            return
        await self.cog.show_preview(interaction, d, lang)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, "operation details modal")  # type: ignore[arg-type]


class DraftView(discord.ui.View):
    """Ephemeral helper under the draft. Stage: basics | details | ready."""

    def __init__(self, cog: "OperationsCog", draft: Draft, lang: str, stage: str) -> None:
        super().__init__(timeout=1800)
        self.cog, self.draft, self.lang, self.stage = cog, draft, lang, stage
        if stage == "basics":
            self._add(("Основное" if lang == "ru" else "Basics"), discord.ButtonStyle.primary, self.open_basics)
        elif stage == "details":
            self._add(t("op_btn_details", lang), discord.ButtonStyle.primary, self.open_details)
        else:
            self._add(t("op_btn_publish", lang), discord.ButtonStyle.success, self.publish)
            self._add(t("op_btn_details", lang), discord.ButtonStyle.secondary, self.open_details)
            self._add(("Основное" if lang == "ru" else "Basics"), discord.ButtonStyle.secondary, self.open_basics)
            self._add(t("op_btn_cancel", lang), discord.ButtonStyle.danger, self.discard)

    def _add(self, label: str, style: discord.ButtonStyle, handler) -> None:
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.draft.organizer_id:
                await reply(interaction, t("err_no_perm", self.lang))
                return
            await handler(interaction)

        button.callback = callback  # type: ignore[assignment]
        self.add_item(button)

    async def open_basics(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BasicsModal(self.cog, self.draft, self.lang))

    async def open_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DetailsModal(self.cog, self.draft, self.lang))

    async def discard(self, interaction: discord.Interaction) -> None:
        self.cog.drafts.pop(self.draft.organizer_id, None)
        await interaction.response.edit_message(content=t("op_cancelled_draft", self.lang), embed=None, view=None)

    async def publish(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            link = await self.cog.publish_draft(interaction, self.draft)
        except ParseError as exc:
            await interaction.edit_original_response(content=parse_error_text(exc, self.lang), embed=None, view=self)
            return
        if link is None:
            await interaction.edit_original_response(content=t("op_no_forum", self.lang), embed=None, view=self)
            return
        self.cog.drafts.pop(self.draft.organizer_id, None)
        await interaction.edit_original_response(content=t("op_published", self.lang, link=link), embed=None, view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction.client, interaction, error, "operation draft")  # type: ignore[arg-type]


# --------------------------------------------------- persistent card buttons --
def op_view(op_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("signup", "leave", "brief", "manage", "supply"):
        view.add_item(OpButton(op_id, action))
    return view


class OpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wb:op:(?P<id>\d+):(?P<action>signup|leave|brief|manage|supply)"):
    STYLE = {
        "supply": discord.ButtonStyle.secondary, "signup": discord.ButtonStyle.success, "leave": discord.ButtonStyle.secondary,
        "brief": discord.ButtonStyle.primary, "manage": discord.ButtonStyle.secondary,
    }
    LABEL = {"supply": "op_btn_supply", "signup": "op_btn_signup", "leave": "op_btn_leave", "brief": "op_btn_brief", "manage": "op_btn_manage"}

    def __init__(self, op_id: int, action: str) -> None:
        super().__init__(discord.ui.Button(label=t(self.LABEL[action], "ru"), style=self.STYLE[action], custom_id=f"wb:op:{op_id}:{action}"))
        self.op_id, self.action = op_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]), match["action"])

    @interaction_errors
    async def callback(self, interaction: discord.Interaction) -> None:
        cog: OperationsCog = interaction.client.get_cog("OperationsCog")  # type: ignore[assignment]
        handler = {
            "supply": cog.on_supply, "signup": cog.on_signup, "leave": cog.on_leave, "brief": cog.on_brief, "manage": cog.on_manage,
        }[self.action]
        await interaction.response.defer(ephemeral=True)
        await handler(interaction, self.op_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, f"op button {self.action}")  # type: ignore[arg-type]


class SlotSelectView(discord.ui.View):
    def __init__(self, cog: "OperationsCog", op_id: int, lang: str, slots: list[SlotView], current: int | None) -> None:
        super().__init__(timeout=180)
        self.cog, self.op_id, self.lang = cog, op_id, lang
        options = []
        for s in slots[:25]:
            taken = len(s.members)
            full = taken >= s.capacity
            options.append(discord.SelectOption(
                label=s.name[:100], value=str(s.id), description=(f"{taken}/{s.capacity}" + ((" · занято" if lang == "ru" else " · full") if full else "")),
                default=(s.id == current),
            ))
        select = discord.ui.Select(placeholder=t("op_signup_title", lang), options=options, min_values=1, max_values=1)
        select.callback = self.chosen  # type: ignore[assignment]
        self.select = select
        self.add_item(select)

    async def chosen(self, interaction: discord.Interaction) -> None:
        op = await self.cog.bot.db.read(repo.op_get, self.op_id)
        if not await self.cog.admitted_here(interaction, op):
            await reply(interaction, t('err_no_perm', self.lang))
            return
        slot_id = int(self.select.values[0])
        await interaction.response.defer()
        try:
            result = await self.cog.mutate(interaction,self.op_id,ops.signup,slot_id,utcnow(),
                                         self.cog.bot.config.noshow_limit,self.cog.bot.config.noshow_block_days)
        except ops.OperationError as exc:
            await reply(interaction,str(exc)); return
        lang = self.lang
        key = {
            "ok": "op_signup_ok", "moved": "op_signup_moved", "already": "op_signup_already",
            "full": "op_signup_full", "operation_full": "op_signup_operation_full", "closed": "op_signup_closed", "no_slot": "err_generic",
        }.get(result.status, "err_generic")
        if result.status == "ok":
            op = await self.cog.bot.db.read(repo.op_get, self.op_id)
            if op is not None and op["status"] == RUNNING:
                key = "op_signup_ok_late"
        text = t(key, lang, slot=result.slot_name)
        await interaction.edit_original_response(content=text, view=None if result.status != "full" else self)
        if result.status in ("ok", "moved"):
            self.cog.request_refresh(self.op_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction.client, interaction, error, "slot select")  # type: ignore[arg-type]


class ManageView(discord.ui.View):
    def __init__(self, cog: "OperationsCog", op_id: int, lang: str, status: str, urgent: bool) -> None:
        super().__init__(timeout=300)
        self.cog, self.op_id, self.lang = cog, op_id, lang
        if status == OPEN:
            self._add(t("op_btn_start", lang), discord.ButtonStyle.primary, self.start)
        if status in (OPEN, RUNNING):
            self._add(t("op_btn_finish", lang), discord.ButtonStyle.success, self.finish)
            self._add(t("op_btn_urgent_off" if urgent else "op_btn_urgent_on", lang), discord.ButtonStyle.secondary, self.toggle_urgent)
            self._add("Начальные запасы", discord.ButtonStyle.secondary, self.supply)
            self._add('Изменить основное', discord.ButtonStyle.secondary, self.edit_basics)
            self._add('Изменить время', discord.ButtonStyle.secondary, self.edit_schedule)
            self._add('План, места, ресурсы', discord.ButtonStyle.secondary, self.edit_briefing)
            self._add('Открыть/закрыть набор', discord.ButtonStyle.secondary, self.recruiting)
            self._add('Состав и явка', discord.ButtonStyle.secondary, self.roster)
            self._add(t("op_btn_cancel_op", lang), discord.ButtonStyle.danger, self.cancel)

    def _add(self, label: str, style: discord.ButtonStyle, handler) -> None:
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction) -> None:
            await handler(interaction)

        button.callback = callback  # type: ignore[assignment]
        self.add_item(button)

    async def _guard(self, interaction: discord.Interaction):
        op = await self.cog.bot.db.read(repo.op_get, self.op_id)
        member = interaction.user
        if not await self.cog.allowed_manage(interaction, op) or op["status"] not in (OPEN, RUNNING):
            await reply(interaction, t("err_no_perm", self.lang))
            return None
        return op

    async def start(self, interaction: discord.Interaction) -> None:
        if await self._guard(interaction) is None:
            return
        changed = await self.cog.mutate(interaction,self.op_id,ops.start,utcnow())
        text = t("op_started" if changed else "op_state_bad", self.lang)
        await interaction.response.edit_message(content=text, view=None)
        if changed:
            self.cog.request_refresh(self.op_id)

    async def finish(self, interaction):
        if await self._guard(interaction) is None:
            return
        await reply(interaction, 'Завершить операцию? Будут зачтены только подтверждённые участники и поставщики. '
                    'Сначала отметьте явку через /operation attendance. Неподтверждённая запись не засчитывается.',
                    view=FinishView(self.cog,self.op_id))

    async def cancel(self, interaction):
        if await self._guard(interaction) is not None:
            await interaction.response.send_modal(CancelModal(self.cog,self.op_id))

    async def recruiting(self, interaction):
        await interaction.response.defer(ephemeral=True)
        op = await self._guard(interaction)
        if op is None:
            return
        await self.cog.mutate(interaction,self.op_id,ops.set_recruiting,not op['signup_open'],utcnow())
        self.cog.request_refresh(self.op_id)
        await reply(interaction,'Набор закрыт.' if op['signup_open'] else 'Набор открыт.')

    async def roster(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if await self._guard(interaction) is None:
            return
        rows=await self.cog.bot.db.read(repo.op_signups,self.op_id)
        lines=[f"<@{r['user_id']}> — {'подтверждён' if r['confirmed'] and r['attended'] else 'не подтверждён'}" for r in rows]
        await reply(interaction, ('\n'.join(lines)[:1750] or 'Пока никто не записан.') +
                    f'\nЯвка: /operation attendance operation:{self.op_id} player:@игрок attended:True',allowed_mentions=discord.AllowedMentions.none())

    async def toggle_urgent(self, interaction: discord.Interaction) -> None:
        op = await self._guard(interaction)
        if op is None:
            return
        await self.cog.mutate(interaction,self.op_id,ops.guarded,lambda conn,oid:repo.op_update(conn,oid,urgent=0 if op["urgent"] else 1))
        await interaction.response.edit_message(content=t("op_urgent_ok", self.lang), view=None)
        self.cog.request_refresh(self.op_id)

    async def supply(self, interaction: discord.Interaction) -> None:
        if await self._guard(interaction) is None:
            return
        items = await self.cog.bot.db.read(repo.op_supply, self.op_id)
        if not items:
            await reply(interaction, t("op_supply_none", self.lang))
            return
        await interaction.response.send_modal(SupplyModal(self.cog, self.op_id, self.lang, [r["name"] for r in items]))

    async def editor(self, interaction, section):
        op=await self._guard(interaction)
        if op is None:
            return
        slots=await self.cog.bot.db.read(repo.op_slots,self.op_id)
        supply=await self.cog.bot.db.read(repo.op_supply,self.op_id)
        await interaction.response.send_modal(Editor(self.cog,op,slots,supply,section))

    async def edit_basics(self,interaction):
        await self.editor(interaction,'basics')

    async def edit_schedule(self,interaction):
        await self.editor(interaction,'schedule')

    async def edit_briefing(self,interaction):
        await self.editor(interaction,'details')

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction.client, interaction, error, "manage view")  # type: ignore[arg-type]


class SupplyModal(discord.ui.Modal):
    def __init__(self, cog: "OperationsCog", op_id: int, lang: str, names: list[str]) -> None:
        super().__init__(title=t("op_supply_title", lang), timeout=600)
        self.cog, self.op_id, self.lang, self.names = cog, op_id, lang, names
        self.f = discord.ui.TextInput(
            label=t("op_supply_label", lang)[:45], style=discord.TextStyle.paragraph, max_length=300,
            placeholder="\n".join(f"{n} +100" for n in names[:3]),
        )
        self.add_item(self.f)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        op = await self.cog.bot.db.read(repo.op_get, self.op_id)
        if not await self.cog.allowed_manage(interaction, op) or op["status"] not in (OPEN, RUNNING):
            await reply(interaction, t('err_no_perm', self.lang))
            return
        try:
            updates = parse_supply_progress(self.f.value, self.names)
            if any(increment for _,increment in updates.values()):
                await reply(interaction,"Укажите итоговый начальный запас без +. Это предотвращает двойной учёт при повторе формы.")
                return
        except ParseError as exc:
            await reply(interaction, parse_error_text(exc, self.lang))
            return
        await self.cog.mutate(interaction,self.op_id,ops.guarded,repo.supply_apply,(updates,))
        await reply(interaction, t("op_supply_ok", self.lang))
        self.cog.request_refresh(self.op_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, "supply modal")  # type: ignore[arg-type]


class BriefingModal(discord.ui.Modal):
    def __init__(self, cog: "OperationsCog", op_id: int, lang: str, op) -> None:
        super().__init__(title=t("op_modal2_title", lang), timeout=600)
        self.cog, self.op_id, self.lang = cog, op_id, lang
        para = discord.TextStyle.paragraph
        self.f_public = discord.ui.TextInput(label=t("op_f_public", lang), default=op["briefing_public"], required=False, style=para, max_length=1500)
        self.f_closed = discord.ui.TextInput(label=t("op_f_closed", lang), default=op["briefing_closed"], required=False, style=para, max_length=1500)
        self.add_item(self.f_public)
        self.add_item(self.f_closed)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        op = await self.cog.bot.db.read(repo.op_get, self.op_id)
        if not await self.cog.allowed_manage(interaction, op) or op["status"] not in (OPEN, RUNNING):
            await reply(interaction, t('err_no_perm', self.lang))
            return
        await self.cog.bot.db.write(
            lambda conn: repo.op_update(
                conn, self.op_id, briefing_public=self.f_public.value.strip(), briefing_closed=self.f_closed.value.strip(),
            )
        )
        await reply(interaction, t("op_edit_ok", self.lang))
        self.cog.request_refresh(self.op_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction.client, interaction, error, "briefing modal")  # type: ignore[arg-type]


# --------------------------------------------------------------------- cog --
class OperationsCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot
        self.archiver = OperationArchiver(bot)
        self.operation_locks = {}
        self.forum_tag_locks = {}
        self.drafts: dict[int, Draft] = {}
        self._pending: dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        # Repair already-acknowledged cards whose old tag ignored closed recruitment.
        await self.bot.db.write(lambda conn: conn.execute(
            "UPDATE operations SET card_revision=card_revision+1 "
            "WHERE status IN ('open','running') AND signup_open=0"))
        self.bot.add_dynamic_items(OpButton)
        self.tick.start()
        self.archive_tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.archive_tick.cancel()
        for task in self._pending.values():
            task.cancel()

    def actor(self, interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise ops.OperationError('Команда доступна только на сервере.')
        return ops.Actor(interaction.guild.id,interaction.user.id,can_staff(self.bot,interaction.user),
                         frozenset(member_role_keys(self.bot,interaction.user)))

    def op_lock(self,op_id):
        return self.operation_locks.setdefault(op_id,asyncio.Lock())

    async def mutate(self,interaction,op_id,fn,*args):
        async with self.op_lock(op_id):
            return await self.bot.db.write(fn,self.actor(interaction),op_id,*args)

    # -------------------------------------------------------------- helpers --
    @staticmethod
    def set_status_if(conn, op_id: int, expected: str, new: str) -> bool:
        cur = conn.execute("UPDATE operations SET status=? WHERE id=? AND status=?", (new, op_id, expected))
        return cur.rowcount == 1

    async def _thread_of(self, op_row) -> discord.Thread | None:
        thread_id = op_row["thread_id"]
        if not thread_id:
            return None
        guild = self.bot.get_guild(op_row["guild_id"])
        if guild is None:
            return None
        thread = guild.get_thread(thread_id)
        if thread is not None:
            return thread
        try:
            fetched = await guild.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def refresh_card(self, op_id: int) -> None:
        async with self.op_lock(op_id):
            await self._refresh_card(op_id)

    async def forum_tags(self, forum, view):
        names = wanted_tag_names(view)
        by_name = {tag.name: tag for tag in forum.available_tags}
        if CLOSED_SIGNUP_TAG in names and CLOSED_SIGNUP_TAG not in by_name:
            # Fetch under a per-forum lock: concurrent cards and a delayed gateway
            # update must not create duplicate tags or overwrite newer forum tags.
            async with self.forum_tag_locks.setdefault(forum.id, asyncio.Lock()):
                current = await forum.guild.fetch_channel(forum.id)
                by_name = {tag.name: tag for tag in current.available_tags}
                if CLOSED_SIGNUP_TAG not in by_name:
                    if len(current.available_tags) >= 20:
                        raise ops.OperationError('На доске операций заняты все 20 тегов. Освободите место для тега закрытого набора.')
                    by_name[CLOSED_SIGNUP_TAG] = await current.create_tag(
                        name=CLOSED_SIGNUP_TAG, reason='Отображение закрытого набора операции')
        return [by_name[name] for name in names if name in by_name]

    async def sync_rooms(self, op_id):
        community = self.bot.get_cog("CommunityCog")
        if community is not None:
            try:
                await community.sync_operation(op_id)
            except Exception:
                log.exception("Operation rooms %s await background retry", op_id)

    async def _refresh_card(self, op_id):
        await self.sync_rooms(op_id)
        view,op = await self.bot.db.read(lambda c:(load_view(c,op_id),repo.op_get(c,op_id)))
        if view is None or op is None:
            return
        thread = await self._thread_of(op)
        if thread is None or not op["message_id"]:
            return
        embed = card_embed(build_card(view))
        active = view.status in (OPEN, RUNNING)
        try:
            message = thread.get_partial_message(op["message_id"])
            await message.edit(embed=embed, view=op_view(op_id) if active else None)
            new_title=f"#{op_id} "+thread_title(view.region,view.title)[:85]
            if thread.name!=new_title:
                await thread.edit(name=new_title)
            forum = thread.parent
            if isinstance(forum, discord.ForumChannel):
                tags = await self.forum_tags(forum, view)
                if {x.id for x in tags} != {x.id for x in thread.applied_tags}:
                    await thread.edit(applied_tags=tags)
            await self.bot.db.write(repo.card_ack,op_id,op["card_revision"])
        except discord.HTTPException:
            log.warning("Could not refresh the card of operation %s", op_id, exc_info=True)

    def request_refresh(self, op_id: int) -> None:
        """Coalesce bursts of sign-ups into one edit (Discord rate-limits message edits)."""
        task = self._pending.get(op_id)
        if task is not None and not task.done():
            return

        async def run() -> None:
            await asyncio.sleep(1.5)
            try:
                await self.refresh_card(op_id)
            except Exception:  # noqa: BLE001 - never let a background edit crash silently
                log.exception("refresh_card failed for %s", op_id)

        self._pending[op_id] = asyncio.create_task(run())

    async def _say(self, op_row, content: str, user_ids: list[int] | None = None) -> None:
        thread = await self._thread_of(op_row)
        if thread is None:
            return
        mentions = " ".join(f"<@{u}>" for u in (user_ids or []))
        text = f"{mentions}\n{content}".strip() if mentions else content
        try:
            await thread.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.warning("Could not post to the thread of operation %s", op_row["id"], exc_info=True)

    # ------------------------------------------------------------ publishing --
    async def admitted_here(self, interaction, op):
        if (interaction.guild is None or op is None or op['guild_id'] != interaction.guild.id
                or not isinstance(interaction.user, discord.Member)):
            return False
        if can_staff(self.bot, interaction.user):
            return True
        user = await self.bot.db.read(repo.user_get, interaction.user.id)
        return bool(user and user['status'] == 'admitted' and is_admitted(self.bot, interaction.user))

    async def creator_clan(self, interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None
        if can_staff(self.bot, interaction.user):
            return {'clan_id': None}  # staff may organize a community operation
        clan = await self.bot.db.read(clans.representative, interaction.guild.id, interaction.user.id)
        if clan is None:
            return None
        required = 'regiment_co' if clan['duty'] == 'leader' else 'clan_delegate'
        return clan if required in member_role_keys(self.bot, interaction.user) else None

    async def allowed_manage(self, interaction, op):
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            return False
        return await self.bot.db.read(clans.may_manage, interaction.guild.id, member.id, op,
                                      can_staff(self.bot, member), member_role_keys(self.bot, member))

    async def user_tz(self, user_id: int) -> str:
        user = await self.bot.db.read(repo.user_get, user_id)
        return (user["tz"] if user is not None and user["tz"] else None) or self.bot.config.default_tz

    async def publish_draft(self, interaction: discord.Interaction, d: Draft) -> str | None:
        async with d.publish_lock:
            return await self._publish_draft(interaction,d)

    async def _publish_draft(self, interaction, d):
        guild = interaction.guild
        clan = await self.creator_clan(interaction)
        if (guild is None or guild.id != d.guild_id or interaction.user.id != d.organizer_id
                or clan is None or clan['clan_id'] != d.clan_id):
            raise ParseError('no_perm')
        if d.published_url:
            return d.published_url
        reserved=await self.bot.db.read(lambda c:c.execute('SELECT * FROM operations WHERE publication_key=?',(d.publication_key,)).fetchone())
        if reserved is not None:
            link=await self.recover_publication(reserved)
            if link:
                d.published_url=link
                return link
            raise ops.OperationError(f"Результат публикации #{reserved['id']} пока неизвестен. Бот сверяет доску; не создавайте копию. Проверка: /operation recover.")
        forum = get_text_channel(self.bot, guild, "ops_board")
        if not isinstance(forum, discord.ForumChannel):
            return None
        if d.start_at is None or not d.slots:
            raise ParseError("bad_datetime")
        if d.start_at <= utcnow():
            raise ParseError("past")
        now = utcnow()
        actor=self.actor(interaction)
        def reserve(conn):
            if not actor.staff:
                current=clans.representative(conn,guild.id,actor.user_id)
                if not current or current['clan_id']!=d.clan_id:
                    raise ops.OperationError('Полномочия изменились. Создайте черновик заново.')
            op_id = repo.op_create(
                conn, guild_id=guild.id, organizer_id=d.organizer_id, title=d.raw1["title"], region=d.raw1.get("region", ""),
                type=d.type, language=d.language, start_at=d.start_at, duration_min=d.duration_min or 60,
                requirements=d.raw1.get("requirements", ""), briefing_public=d.raw2.get("public", ""),
                briefing_closed=d.raw2.get("closed", ""), slots=d.slots, supply=d.supply, now=now, clan_id=d.clan_id,
            )
            repo.op_update(conn,op_id,timezone=d.tz_name,publication_key=d.publication_key,forum_id=forum.id)
            ops.record(conn,actor,op_id,'Создание операции',now)
            return op_id
        op_id = await self.bot.db.write(reserve)
        view = await self.bot.db.read(load_view, op_id)
        assert view is not None
        by_name = {tag.name: tag for tag in forum.available_tags}
        tags = [by_name[n] for n in wanted_tag_names(view) if n in by_name]
        ping_role = get_role(self.bot, guild, "ping_ops")
        content = ping_role.mention if ping_role is not None else None
        try:
            created = await forum.create_thread(
                name=f"#{op_id} " + thread_title(view.region, view.title)[:85], content=content, embed=card_embed(build_card(view)),
                view=op_view(op_id), applied_tags=tags[:5],
                allowed_mentions=discord.AllowedMentions(roles=[ping_role] if ping_role else False),
                reason=f"Operation #{op_id}",
            )
        except discord.HTTPException:
            # Discord may have created the post before a timeout; never erase its database identity.
            raise

        def link(conn):
            repo.op_update(conn, op_id, forum_id=forum.id, thread_id=created.thread.id, message_id=created.message.id)

        await self.bot.db.write(link)
        d.published_url=created.thread.jump_url
        await self.sync_rooms(op_id)
        return d.published_url

    async def recover_publication(self,op):
        if op['thread_id']:
            return f"https://discord.com/channels/{op['guild_id']}/{op['thread_id']}"
        guild=self.bot.get_guild(op['guild_id'])
        if guild is None:
            return None
        forum=guild.get_channel(op['forum_id'])
        if not isinstance(forum,discord.ForumChannel):
            return None
        candidates=await guild.active_threads()
        candidates += [t async for t in forum.archived_threads(limit=None)]
        for thread in candidates:
            if thread.parent_id==forum.id and thread.owner_id==self.bot.user.id and thread.name.startswith(f"#{op['id']} "):
                await self.bot.db.write(lambda c:repo.op_update(c,op['id'],thread_id=thread.id,message_id=thread.id))
                return thread.jump_url
        return None

    async def show_preview(self, interaction: discord.Interaction, d: Draft, lang: str) -> None:
        embed = card_embed(build_card(d.preview_view()))
        await respond_draft(interaction, t("op_draft_ready", lang), embed, DraftView(self, d, lang, "ready"))

    # -------------------------------------------------------- slash commands --
    operation = app_commands.Group(name="operation", description="Operations · Операции", guild_only=True)

    @operation.command(name="create", description="Create an operation · Создать операцию")
    @app_commands.describe(type="Operation type · Тип")
    @app_commands.choices(
        type=[app_commands.Choice(name=label.ru, value=key) for key, label in OP_TYPES.items()],
    )
    async def create(
        self, interaction: discord.Interaction, type: app_commands.Choice[str],
    ) -> None:
        lang = await user_language(interaction)
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_guild_only", lang))
            return
        clan = await self.creator_clan(interaction)
        if clan is None:
            await reply(interaction, t("err_no_perm", lang))
            return
        if not isinstance(get_text_channel(self.bot, interaction.guild, "ops_board"), discord.ForumChannel):
            await reply(interaction, t("op_no_forum", lang))
            return
        now = utcnow()
        for uid in [u for u, d in self.drafts.items() if now - d.created > DRAFT_TTL]:
            self.drafts.pop(uid, None)
        draft = Draft(
            guild_id=interaction.guild.id, organizer_id=member.id, type=type.value, language="ru",
            tz_name=await self.user_tz(member.id), created=now, clan_id=clan["clan_id"],
        )
        self.drafts[member.id] = draft
        await interaction.response.send_modal(BasicsModal(self, draft, lang))

    @operation.command(name='recover', description='Сверить незавершённую публикацию с доской')
    async def recover(self,interaction:discord.Interaction,operation:int):
        await interaction.response.defer(ephemeral=True)
        op=await self.bot.db.read(ops.get,self.actor(interaction),operation,True,False)
        link=await self.recover_publication(op)
        await reply(interaction,link or 'Публикация пока не найдена. Если на доске её нет, отмените эту операцию через /operation manage и создайте новую.')

    @operation.command(name='manage', description='Управлять операцией по её ID')
    async def manage_command(self,interaction:discord.Interaction,operation:int):
        await interaction.response.defer(ephemeral=True)
        await self.on_manage(interaction,operation)

    @operation.command(name='supplies', description='Показать ресурсы и обещания поставщиков')
    async def supplies_command(self,interaction:discord.Interaction,operation:int):
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.read(ops.get,self.actor(interaction),operation)
        rows=await self.bot.db.read(ops.supply_totals,operation)
        pledges=await self.bot.db.read(lambda c:c.execute('SELECT * FROM supply_pledges WHERE op_id=?',(operation,)).fetchall())
        lines=[f"#{r['id']} {r['name']}: нужно {r['needed']}, обещано {r['promised']}, подготовлено {r['prepared']}" for r in rows]
        lines += [f"<@{p['user_id']}>: ресурс #{p['supply_id']}, всего обещал {p['amount']}, доставлено {p['delivered']}" for p in pledges]
        text='\n'.join(lines) or 'Ресурсов пока нет.'
        for pos in range(0,len(text),1900):
            await reply(interaction,text[pos:pos+1900],allowed_mentions=discord.AllowedMentions.none())

    @operation.command(name='delivery', description='Подтвердить итоговое доставленное количество')
    async def delivery(self,interaction:discord.Interaction,operation:int,resource:int,player:discord.Member,
                       amount:app_commands.Range[int,0,10000000]):
        await interaction.response.defer(ephemeral=True)
        await self.mutate(interaction,operation,ops.deliver,resource,player.id,amount,utcnow())
        self.request_refresh(operation)
        await reply(interaction,'Доставка подтверждена. Участие будет учтено после завершения операции.')

    @delivery.autocomplete('resource')
    async def supply_autocomplete(self,interaction,current):
        op_id=getattr(interaction.namespace,'operation',None)
        if not op_id:
            return []
        try:
            await self.bot.db.read(ops.get,self.actor(interaction),op_id,True)
        except ops.OperationError:
            return []
        items=await self.bot.db.read(repo.op_supply,op_id)
        return [app_commands.Choice(name=x['name'],value=x['id']) for x in items if current.casefold() in x['name'].casefold()][:25]

    @operation.command(name="noshow", description="Record a no-show · Отметить неявку")
    @app_commands.describe(operation="Operation · Операция", player="Player who did not show up · Игрок")
    async def noshow(self, interaction: discord.Interaction, operation: int, player: discord.Member) -> None:
        lang = await user_language(interaction)
        member = interaction.user
        op = await self.bot.db.read(repo.op_get, operation)
        if op is None or op["guild_id"] != getattr(interaction.guild, "id", None):
            await reply(interaction, t("err_op_missing", lang))
            return
        if not await self.allowed_manage(interaction, op):
            await reply(interaction, t("err_no_perm", lang))
            return
        ok = await self.mutate(interaction,operation,ops.guarded,repo.mark_noshow,(player.id,),False)
        await reply(interaction, t("op_noshow_ok" if ok else "op_noshow_missing", lang, user=player.mention),
                    allowed_mentions=discord.AllowedMentions.none())

    @operation.command(name="attendance", description="Confirm actual participation or correct a completed operation")
    async def attendance(self, interaction: discord.Interaction, operation: int,
                         player: discord.Member, attended: bool = True) -> None:
        lang = await user_language(interaction)
        op = await self.bot.db.read(repo.op_get, operation)
        if op is None or op['guild_id'] != getattr(interaction.guild, 'id', None):
            await reply(interaction, t('err_op_missing', lang))
            return
        if not await self.allowed_manage(interaction, op):
            await reply(interaction, t('err_no_perm', lang))
            return
        changed = await self.mutate(interaction,operation,ops.guarded,repo.confirm_attendance,(player.id,attended),False)
        if not changed:
            await reply(interaction, "Игрок не записан или операция отменена." if lang == 'ru'
                        else "The player is not signed up or the operation was cancelled.")
            return
        await reply(interaction, "Участие отмечено." if lang == 'ru' else "Attendance recorded.")
        await audit(self.bot, interaction.guild, interaction.user, "Подтверждение участия",
                    f"<@{player.id}>", f"Операция #{operation}; присутствовал: {'да' if attended else 'нет'}")
        if op["status"] == DONE:
            await evaluate_member(self.bot, interaction.guild, player.id)

    @noshow.autocomplete("operation")
    async def noshow_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return []
        mine = None if can_staff(self.bot, interaction.user) else interaction.user.id
        rows = await self.bot.db.read(repo.ops_recent, interaction.guild.id, mine, 25)
        needle = current.lower().strip().lstrip("#")
        out = []
        for r in rows:
            label = f"#{r['id']} {r['title']}"
            if needle and needle not in label.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=r["id"]))
        return out[:25]

    # --------------------------------------------------------- button handlers --
    async def on_supply(self,interaction,op_id):
        try:
            await self.bot.db.read(ops.get,self.actor(interaction),op_id)
        except ops.OperationError as exc:
            await reply(interaction,str(exc)); return
        items=await self.bot.db.read(repo.op_supply,op_id)
        if not items:
            await reply(interaction,'Для операции ресурсы не указаны.'); return
        await reply(interaction,'Выберите ресурс. Введите итоговое обещанное количество; 0 отменяет недоставленное обещание.',view=SupplyPicker(self,op_id,items))

    async def on_signup(self, interaction: discord.Interaction, op_id: int) -> None:
        lang = await user_language(interaction)
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_guild_only", lang))
            return
        op = await self.bot.db.read(repo.op_get, op_id)
        if not await self.admitted_here(interaction, op):
            await reply(interaction, t("err_not_admitted", lang, verify=channel_mention(self.bot, guild.id, "verify")))
            return
        now = utcnow()
        view = await self.bot.db.read(load_view, op_id)
        if view is None:
            await reply(interaction, t("err_op_missing", lang))
            return
        if view.status not in (OPEN, RUNNING):
            await reply(interaction, t("op_signup_closed", lang))
            return
        history = await self.bot.db.read(repo.attendance_history, member.id, self.bot.config.noshow_limit)
        until = noshow_block_until(history, self.bot.config.noshow_limit, self.bot.config.noshow_block_days)
        if until is not None and until > now:
            await reply(interaction, t("op_signup_blocked", lang, until=f"<t:{int(until.timestamp())}:f>"))
            return
        if all(len(s.members) >= s.capacity for s in view.slots) and not any(member.id in s.members for s in view.slots):
            await reply(interaction, t("op_signup_all_full", lang))
            return
        current = next((s.id for s in view.slots if member.id in s.members), None)
        await reply(interaction, t("op_signup_title", lang), view=SlotSelectView(self, op_id, lang, view.slots, current))

    async def on_leave(self, interaction: discord.Interaction, op_id: int) -> None:
        lang = await user_language(interaction)
        op = await self.bot.db.read(repo.op_get,op_id)
        if op is None or op['guild_id'] != getattr(interaction.guild,'id',None):
            await reply(interaction,'Операция не найдена.'); return
        ok = await self.bot.db.write(repo.leave, op_id, interaction.user.id)
        await reply(interaction, t("op_left" if ok else "op_not_signed", lang))
        if ok:
            self.request_refresh(op_id)

    async def on_brief(self, interaction: discord.Interaction, op_id: int) -> None:
        lang = await user_language(interaction)
        op = await self.bot.db.read(repo.op_get, op_id)
        member = interaction.user
        if op is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_op_missing", lang))
            return
        if op['guild_id'] != getattr(interaction.guild,'id',None) or op['status'] not in (OPEN,RUNNING):
            await reply(interaction,'Операция закрыта или недоступна.'); return
        if not op["briefing_closed"].strip():
            await reply(interaction, t("op_brief_none", lang))
            return
        privileged = await self.allowed_manage(interaction, op)
        if not privileged and not await self.admitted_here(interaction, op):
            await reply(interaction, t('err_no_perm', lang))
            return
        signed = await self.bot.db.read(repo.op_signup_of, op_id, member.id)
        if not privileged and signed is None:
            await reply(interaction, t("op_brief_need_signup", lang))
            return
        opens = from_iso(op["start_at"]) - timedelta(minutes=self.bot.config.briefing_open_minutes)
        if not privileged and utcnow() < opens:
            await reply(interaction, t("op_brief_later", lang, minutes=self.bot.config.briefing_open_minutes, ts=int(opens.timestamp())))
            return
        embed = discord.Embed(title=t("op_brief_title", lang), description=op["briefing_closed"][:4000], color=0x2F6FB5)
        await reply(interaction, embed=embed)

    async def on_manage(self, interaction: discord.Interaction, op_id: int) -> None:
        lang = await user_language(interaction)
        op = await self.bot.db.read(repo.op_get, op_id)
        member = interaction.user
        if op is None or not await self.allowed_manage(interaction, op):
            await reply(interaction, t("err_no_perm", lang))
            return
        await reply(
            interaction, t("op_manage_title", lang, id=op_id),
            view=ManageView(self, op_id, lang, op["status"], bool(op["urgent"])),
        )

    # ------------------------------------------------------------- tick loop --
    @tasks.loop(seconds=30)
    async def tick(self):
        try:
            await self.bot.db.write(notifications.schedule,utcnow())
            for row in await self.bot.db.read(repo.dirty_cards):
                self.request_refresh(row["id"])
            for job in await self.bot.db.read(notifications.pending):
                async with self.op_lock(job['op_id']):
                    try:
                        op=await self.bot.db.read(repo.op_get,job['op_id'])
                        guild=self.bot.get_guild(op['guild_id'])
                        if guild is None:
                            continue
                        if not op['thread_id']:
                            await self.bot.db.write(lambda c:c.execute("UPDATE notifications SET state='source_missing' WHERE id=?",(job['id'],)))
                            continue
                        thread=guild.get_thread(op['thread_id']) or await guild.fetch_channel(op['thread_id'])
                        await notifications.send(self.bot,job,thread)
                        self.request_refresh(op['id'])
                    except discord.NotFound:
                        await self.bot.db.write(lambda c:c.execute("UPDATE notifications SET state='source_missing' WHERE id=?",(job['id'],)))
                    except Exception as exc:
                        await self.bot.db.write(notifications.failed,job['id'],type(exc).__name__)
                        log.warning('Notification %s pending: %s',job['id'],type(exc).__name__)
        except Exception:
            log.exception('Notification cycle failed; will retry')

    @tasks.loop(seconds=60)
    async def archive_tick(self):
        try:
            unpublished=await self.bot.db.read(lambda c:c.execute('SELECT * FROM operations WHERE publication_key IS NOT NULL AND thread_id IS NULL').fetchall())
            for op in unpublished:
                try:
                    await self.recover_publication(op)
                except Exception:
                    log.warning('Publication %s awaits recovery',op['id'])
            rows=await self.bot.db.read(archives.pending)
        except Exception:
            log.exception('Archive queue read failed; will retry')
            return
        for op in rows:
            async with self.op_lock(op['id']):
                try:
                    guild=self.bot.get_guild(op['guild_id'])
                    if guild is None:
                        continue
                    thread=None
                    if op['thread_id']:
                        try:
                            thread=guild.get_thread(op['thread_id']) or await guild.fetch_channel(op['thread_id'])
                        except discord.NotFound:
                            pass
                    await self.archiver.run(op,thread)
                except Exception:
                    log.exception('Archive %s pending; original retained',op['id'])

    @archive_tick.before_loop
    async def _before_archive(self):
        await self.bot.wait_until_ready()

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()



async def setup(bot: WardenBot) -> None:
    await bot.add_cog(OperationsCog(bot))
