"""Operations: create (two modals + preview), the persistent card buttons, reminders and the tick loop."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import repo, clans
from ..bot import WardenBot
from ..i18n import user_language, parse_error_text, t
from ..opsmeta import (
    DEFAULT_SLOTS, DEFAULT_SUPPLY, DONE, LANG_TAG, LANGS, OP_TYPES, OPEN, RUNNING,
    STATUS_TAG, TYPE_TAG, URGENT_TAG,
)
from ..opsplan import Action, OpTimes, plan_actions
from ..perms import member_role_keys, can_staff, is_admitted
from ..ranks import noshow_block_until
from ..render import Card, OpView, SlotView, SupplyView, build_card, thread_title
from ..services import audit, channel_mention, evaluate_member, get_role, get_text_channel, report_error, reply, utcnow
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
    supply = [SupplyView(r["name"], r["needed"], r["collected"]) for r in repo.op_supply(conn, op_id)]
    return OpView(
        id=op["id"], title=op["title"], region=op["region"], type=op["type"], language=op["language"],
        start_at=from_iso(op["start_at"]), duration_min=op["duration_min"], organizer_id=op["organizer_id"],
        requirements=op["requirements"], briefing_public=op["briefing_public"], status=op["status"],
        urgent=bool(op["urgent"]), slots=slots, supply=supply,
    )


def wanted_tag_names(op: OpView) -> list[str]:
    names = [STATUS_TAG[op.status]]
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
        super().__init__(title=t("op_modal_title", lang), timeout=900)
        self.cog, self.draft, self.lang = cog, draft, lang
        raw = draft.raw1
        self.f_title = discord.ui.TextInput(label=t("op_f_title", lang), default=raw.get("title"), max_length=80)
        self.f_region = discord.ui.TextInput(label=t("op_f_region", lang), default=raw.get("region"), required=False, max_length=40)
        self.f_start = discord.ui.TextInput(
            label=t("op_f_start", lang), default=raw.get("start"), max_length=30, placeholder="08.11 20:00 · 2026-11-08 20:00",
        )
        self.f_duration = discord.ui.TextInput(label=t("op_f_duration", lang), default=raw.get("duration") or "2", max_length=12)
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
        self.f_slots = discord.ui.TextInput(label=t("op_f_slots", lang), default=default_slots[:600], style=para, max_length=600)
        self.f_supply = discord.ui.TextInput(label=t("op_f_supply", lang), default=default_supply[:300], required=False, style=para, max_length=300)
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
    for action in ("signup", "leave", "brief", "manage"):
        view.add_item(OpButton(op_id, action))
    return view


class OpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wb:op:(?P<id>\d+):(?P<action>signup|leave|brief|manage)"):
    STYLE = {
        "signup": discord.ButtonStyle.success, "leave": discord.ButtonStyle.secondary,
        "brief": discord.ButtonStyle.primary, "manage": discord.ButtonStyle.secondary,
    }
    LABEL = {"signup": "op_btn_signup", "leave": "op_btn_leave", "brief": "op_btn_brief", "manage": "op_btn_manage"}

    def __init__(self, op_id: int, action: str) -> None:
        super().__init__(discord.ui.Button(label=t(self.LABEL[action], "ru"), style=self.STYLE[action], custom_id=f"wb:op:{op_id}:{action}"))
        self.op_id, self.action = op_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: OperationsCog = interaction.client.get_cog("OperationsCog")  # type: ignore[assignment]
        handler = {
            "signup": cog.on_signup, "leave": cog.on_leave, "brief": cog.on_brief, "manage": cog.on_manage,
        }[self.action]
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
        result = await self.cog.bot.db.write(repo.signup, self.op_id, interaction.user.id, slot_id, utcnow())
        lang = self.lang
        key = {
            "ok": "op_signup_ok", "moved": "op_signup_moved", "already": "op_signup_already",
            "full": "op_signup_full", "closed": "op_signup_closed", "no_slot": "err_generic",
        }.get(result.status, "err_generic")
        if result.status == "ok":
            op = await self.cog.bot.db.read(repo.op_get, self.op_id)
            if op is not None and op["status"] == RUNNING:
                key = "op_signup_ok_late"
        text = t(key, lang, slot=result.slot_name)
        await interaction.response.edit_message(content=text, view=None if result.status != "full" else self)
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
            self._add(t("op_btn_supply", lang), discord.ButtonStyle.secondary, self.supply)
            self._add(t("op_btn_edit", lang), discord.ButtonStyle.secondary, self.edit_briefing)
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
        if not await self.cog.allowed_manage(interaction, op):
            await reply(interaction, t("err_no_perm", self.lang))
            return None
        return op

    async def start(self, interaction: discord.Interaction) -> None:
        if await self._guard(interaction) is None:
            return
        changed = await self.cog.bot.db.write(self.cog.set_status_if, self.op_id, OPEN, RUNNING)
        text = t("op_started" if changed else "op_state_bad", self.lang)
        await interaction.response.edit_message(content=text, view=None)
        if changed:
            self.cog.request_refresh(self.op_id)

    async def finish(self, interaction: discord.Interaction) -> None:
        if await self._guard(interaction) is None:
            return
        await interaction.response.defer()
        attendees = await self.cog.finish_op(self.op_id, interaction.guild)
        key = "op_state_bad" if attendees is None else "op_finished"
        await interaction.edit_original_response(content=t(key, self.lang, n=len(attendees or [])), view=None)

    async def cancel(self, interaction: discord.Interaction) -> None:
        if await self._guard(interaction) is None:
            return
        await interaction.response.defer()
        ok = await self.cog.cancel_op(self.op_id)
        await interaction.edit_original_response(content=t("op_cancelled" if ok else "op_state_bad", self.lang), view=None)

    async def toggle_urgent(self, interaction: discord.Interaction) -> None:
        op = await self._guard(interaction)
        if op is None:
            return
        await self.cog.bot.db.write(lambda conn: repo.op_update(conn, self.op_id, urgent=0 if op["urgent"] else 1))
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

    async def edit_briefing(self, interaction: discord.Interaction) -> None:
        op = await self._guard(interaction)
        if op is None:
            return
        await interaction.response.send_modal(BriefingModal(self.cog, self.op_id, self.lang, op))

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
        if not await self.cog.allowed_manage(interaction, op):
            await reply(interaction, t('err_no_perm', self.lang))
            return
        try:
            updates = parse_supply_progress(self.f.value, self.names)
        except ParseError as exc:
            await reply(interaction, parse_error_text(exc, self.lang))
            return
        await self.cog.bot.db.write(repo.supply_apply, self.op_id, updates)
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
        if not await self.cog.allowed_manage(interaction, op):
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
        self.drafts: dict[int, Draft] = {}
        self._pending: dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(OpButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        for task in self._pending.values():
            task.cancel()

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
        view = await self.bot.db.read(load_view, op_id)
        op = await self.bot.db.read(repo.op_get, op_id)
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
            forum = thread.parent
            if isinstance(forum, discord.ForumChannel):
                by_name = {tag.name: tag for tag in forum.available_tags}
                tags = [by_name[n] for n in wanted_tag_names(view) if n in by_name]
                if {x.id for x in tags} != {x.id for x in thread.applied_tags}:
                    await thread.edit(applied_tags=tags)
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

    async def finish_op(self, op_id: int, guild: discord.Guild | None) -> list[int] | None:
        before = await self.bot.db.read(repo.op_get, op_id)
        if before is None or before["status"] not in (OPEN, RUNNING):
            return None
        attendees = await self.bot.db.write(repo.op_finish, op_id, utcnow())
        op = await self.bot.db.read(repo.op_get, op_id)
        if op is None or op["status"] != DONE:
            return None
        self.request_refresh(op_id)
        await self._say(op, t("op_summary", "ru", n=len(attendees)) + "\n" + t("op_summary", "en", n=len(attendees)))
        guild = guild or self.bot.get_guild(op["guild_id"])
        if guild is not None:
            for uid in attendees:
                await evaluate_member(self.bot, guild, uid)
        return attendees

    async def cancel_op(self, op_id: int) -> bool:
        signups = await self.bot.db.read(repo.op_signups, op_id)
        ok = await self.bot.db.write(repo.op_cancel, op_id, utcnow())
        if not ok:
            return False
        op = await self.bot.db.read(repo.op_get, op_id)
        self.request_refresh(op_id)
        if op is not None:
            await self._say(op, f"{t('op_cancelled', 'ru')}\n{t('op_cancelled', 'en')}", [s["user_id"] for s in signups][:80])
        return True

    # ------------------------------------------------------------ publishing --
    async def admitted_here(self, interaction, op):
        if (interaction.guild is None or op is None or op['guild_id'] != interaction.guild.id
                or not isinstance(interaction.user, discord.Member)):
            return False
        user = await self.bot.db.read(repo.user_get, interaction.user.id)
        return bool(user and user['status'] == 'admitted' and is_admitted(self.bot, interaction.user))

    async def creator_clan(self, interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None
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
        guild = interaction.guild
        clan = await self.creator_clan(interaction)
        if (guild is None or guild.id != d.guild_id or interaction.user.id != d.organizer_id
                or clan is None or clan['clan_id'] != d.clan_id):
            raise ParseError('no_perm')
        forum = get_text_channel(self.bot, guild, "ops_board")
        if not isinstance(forum, discord.ForumChannel):
            return None
        if d.start_at is None or not d.slots:
            raise ParseError("bad_datetime")
        if d.start_at <= utcnow():
            raise ParseError("past")
        now = utcnow()
        op_id = await self.bot.db.write(
            lambda conn: repo.op_create(
                conn, guild_id=guild.id, organizer_id=d.organizer_id, title=d.raw1["title"], region=d.raw1.get("region", ""),
                type=d.type, language=d.language, start_at=d.start_at, duration_min=d.duration_min or 60,
                requirements=d.raw1.get("requirements", ""), briefing_public=d.raw2.get("public", ""),
                briefing_closed=d.raw2.get("closed", ""), slots=d.slots, supply=d.supply, now=now, clan_id=d.clan_id,
            )
        )
        view = await self.bot.db.read(load_view, op_id)
        assert view is not None
        by_name = {tag.name: tag for tag in forum.available_tags}
        tags = [by_name[n] for n in wanted_tag_names(view) if n in by_name]
        ping_role = get_role(self.bot, guild, "ping_ops")
        content = ping_role.mention if ping_role is not None else None
        try:
            created = await forum.create_thread(
                name=thread_title(view.region, view.title), content=content, embed=card_embed(build_card(view)),
                view=op_view(op_id), applied_tags=tags[:5],
                allowed_mentions=discord.AllowedMentions(roles=[ping_role] if ping_role else False),
                reason=f"Operation #{op_id}",
            )
        except discord.HTTPException:
            await self.bot.db.write(repo.op_delete, op_id)
            raise

        def link(conn):
            repo.op_update(conn, op_id, forum_id=forum.id, thread_id=created.thread.id, message_id=created.message.id)

        await self.bot.db.write(link)
        return created.thread.jump_url

    async def show_preview(self, interaction: discord.Interaction, d: Draft, lang: str) -> None:
        embed = card_embed(build_card(d.preview_view()))
        await respond_draft(interaction, t("op_draft_ready", lang), embed, DraftView(self, d, lang, "ready"))

    # -------------------------------------------------------- slash commands --
    operation = app_commands.Group(name="operation", description="Operations · Операции", guild_only=True)

    @operation.command(name="create", description="Create an operation · Создать операцию")
    @app_commands.describe(type="Operation type · Тип", language="Language of the operation · Язык операции")
    @app_commands.choices(
        type=[app_commands.Choice(name=label.both, value=key) for key, label in OP_TYPES.items()],
        language=[app_commands.Choice(name=label.both, value=key) for key, label in LANGS.items()],
    )
    async def create(
        self, interaction: discord.Interaction, type: app_commands.Choice[str], language: app_commands.Choice[str],
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
            guild_id=interaction.guild.id, organizer_id=member.id, type=type.value, language=language.value,
            tz_name=await self.user_tz(member.id), created=now, clan_id=clan["clan_id"],
        )
        self.drafts[member.id] = draft
        await interaction.response.send_modal(BasicsModal(self, draft, lang))

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
        ok = await self.bot.db.write(repo.mark_noshow, operation, player.id)
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
        changed = await self.bot.db.write(repo.confirm_attendance, operation, player.id, attended)
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
        if not privileged and op["status"] == OPEN and utcnow() < opens:
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
    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        now = utcnow()
        rows = await self.bot.db.read(repo.ops_active)
        by_id = {r["id"]: r for r in rows}
        times = [
            OpTimes(r["id"], r["status"], from_iso(r["start_at"]), r["duration_min"],
                    bool(r["reminder60_sent"]), bool(r["reminder10_sent"]))
            for r in rows
        ]
        for action in plan_actions(times, now, self.bot.config.auto_close_hours):
            try:
                await self.run_action(action, by_id[action.op_id])
            except Exception:  # noqa: BLE001 - one broken operation must not stop the loop
                log.exception("tick action %s failed", action)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def run_action(self, action: Action, op) -> None:
        op_id = action.op_id
        if action.kind in ("remind60", "remind10"):
            # Mark first: a failed post must not repeat every minute.
            fields = {"reminder60_sent": 1} if action.kind == "remind60" else {"reminder60_sent": 1, "reminder10_sent": 1}
            await self.bot.db.write(lambda conn: repo.op_update(conn, op_id, **fields))
            signups = await self.bot.db.read(repo.op_signups, op_id)
            key = "op_remind_60" if action.kind == "remind60" else "op_remind_10"
            await self._say(op, f"{t(key, 'ru')}\n{t(key, 'en')}", [s["user_id"] for s in signups][:80])
        elif action.kind == "start":
            if await self.bot.db.write(self.set_status_if, op_id, OPEN, RUNNING):
                self.request_refresh(op_id)
                await self._say(op, f"{t('op_started', 'ru')}\n{t('op_started', 'en')}")
        elif action.kind == "autoclose":
            if await self.bot.db.write(repo.op_close_unconfirmed, op_id, utcnow()):
                self.request_refresh(op_id)
                await self._say(op, f"{t('op_auto_closed', 'ru')}\n{t('op_auto_closed', 'en')}")


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(OperationsCog(bot))
