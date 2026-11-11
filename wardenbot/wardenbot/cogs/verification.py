"""Verification: button -> modal -> code -> screenshot -> review card -> decision."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from .. import repo
from ..bot import WardenBot
from ..codeword import generate_code
from ..i18n import lang_of, parse_error_text, t
from ..perms import can_screen
from ..services import BLUE, audit, channel_mention, get_text_channel, reply, safe_dm, set_rank_roles, utcnow
from ..steam import parse_steam
from ..timeutil import ParseError, from_iso, to_iso

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
STATUS_COLOR = {"approved": 0x3E8E5A, "rejected": 0x8B3A3A, "needs_info": 0xD9A441, "pending": BLUE}


def submission_block(user, latest, now, cooldown_hours: int) -> tuple[str, int] | None:
    """Rechecked inside the write transaction when a previously opened form submits."""
    if user is not None and user['status'] == 'banned':
        return 'err_no_perm', 0
    if user is not None and user['status'] == 'admitted':
        return 'ver_already', 0
    if latest is not None and latest['status'] == 'pending':
        return 'ver_pending', 0
    if user is not None and user['last_reject_at']:
        until = from_iso(user['last_reject_at']) + timedelta(hours=cooldown_hours)
        if until > now:
            return 'ver_cooldown', max(1, int((until - now).total_seconds() // 3600) + 1)
    return None


def build_verify_view() -> discord.ui.View:
    return VerifyPanelView()


class VerifyPanelView(discord.ui.View):
    """Static persistent view: one button with a fixed custom_id."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Пройти допуск · Verify", style=discord.ButtonStyle.primary, custom_id="wb:verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await begin_verification(interaction)


async def begin_verification(interaction: discord.Interaction) -> None:
    bot: WardenBot = interaction.client  # type: ignore[assignment]
    lang = lang_of(interaction)
    member = interaction.user
    if interaction.guild is None or not isinstance(member, discord.Member):
        await reply(interaction, t("err_guild_only", lang))
        return
    now = utcnow()

    def look(conn):
        return repo.user_get(conn, member.id), repo.req_latest_for_user(conn, member.id)

    user, latest = await bot.db.read(look)
    block = submission_block(user, latest, now, bot.config.reject_cooldown_hours)
    if block:
        await reply(interaction, t(block[0], lang, hours=block[1]))
        return
    await interaction.response.send_modal(VerifyModal(lang))


class VerifyModal(discord.ui.Modal):
    def __init__(self, lang: str) -> None:
        super().__init__(title=t("ver_modal_title", lang), timeout=900)
        self.lang = lang
        self.steam = discord.ui.TextInput(label=t("ver_f_steam", lang), placeholder="https://steamcommunity.com/profiles/7656…", max_length=200)
        self.name = discord.ui.TextInput(label=t("ver_f_name", lang), max_length=40)
        self.regiment = discord.ui.TextInput(label=t("ver_f_regiment", lang), required=False, max_length=40)
        self.voucher = discord.ui.TextInput(label=t("ver_f_voucher", lang), required=False, max_length=80)
        for item in (self.steam, self.name, self.regiment, self.voucher):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: WardenBot = interaction.client  # type: ignore[assignment]
        lang = self.lang
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_guild_only", lang))
            return
        try:
            ref = parse_steam(self.steam.value)
        except ParseError as exc:
            await reply(interaction, parse_error_text(exc, lang))
            return
        now = utcnow()
        code = generate_code()
        young = (now - member.created_at).days < bot.config.young_account_days

        def work(conn):
            user = repo.user_ensure(conn, member.id, now)
            block = submission_block(user, repo.req_latest_for_user(conn, member.id), now,
                                     bot.config.reject_cooldown_hours)
            if block:
                return block
            flags = []
            others = repo.steam_conflicts(conn, ref.key, member.id)
            if others:
                flags.append("dup_steam:" + ",".join(str(i) for i in others[:5]))
            if young:
                flags.append("young_account")
            if user["last_reject_at"]:
                flags.append("previously_rejected")
            repo.req_create(
                conn, interaction.guild.id, member.id, code, ref.key, ref.url,
                self.name.value.strip(), self.regiment.value.strip(), self.voucher.value.strip(),
                ";".join(flags), now,
            )
            repo.user_update(conn, member.id, now, status="pending")
            return None

        block = await bot.db.write(work)
        if block:
            await reply(interaction, t(block[0], lang, hours=block[1]))
            return
        await reply(interaction, t("ver_started", lang, code=code))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        from ..services import report_error
        await report_error(interaction.client, interaction, error, "verification modal")  # type: ignore[arg-type]


# ------------------------------------------------------------- review card --
def flag_lines(flags: str) -> list[str]:
    out = []
    for part in filter(None, flags.split(";")):
        if part.startswith("dup_steam:"):
            ids = part.split(":", 1)[1].split(",")
            out.append("⚠ Тот же Steam у других аккаунтов · Same Steam as: " + ", ".join(f"<@{i}>" for i in ids if i.isdigit()))
        elif part == "young_account":
            out.append("⚠ Новый аккаунт Discord · Young Discord account")
        elif part == "previously_rejected":
            out.append("⚠ Ранее отклонялся · Previously rejected")
    return out


def build_review_embed(req, member: discord.abc.User | None, status: str = "pending", note: str = "") -> discord.Embed:
    user_id = req["user_id"]
    embed = discord.Embed(title=f"Допуск #{req['id']} · Verification", color=STATUS_COLOR.get(status, BLUE))
    created = f"<t:{int(member.created_at.timestamp())}:R>" if member is not None else "?"
    embed.add_field(name="Игрок · Player", value=f"<@{user_id}>\nDiscord: {created}", inline=True)
    embed.add_field(name="Ник · Name", value=req["ingame_name"] or "—", inline=True)
    embed.add_field(name="Полк · Regiment", value=req["regiment"] or "—", inline=True)
    embed.add_field(name="Steam", value=req["steam_url"], inline=False)
    embed.add_field(name="Поручитель · Voucher", value=req["voucher_text"] or "—", inline=True)
    embed.add_field(name="Код · Code", value=f"`{req['code']}`", inline=True)
    flags = flag_lines(req["flags"] or "")
    if flags:
        embed.add_field(name="Флаги · Flags", value="\n".join(flags)[:1000], inline=False)
    if note:
        embed.add_field(name="Решение · Decision", value=note[:1000], inline=False)
    return embed


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wb:ver:(?P<id>\d+):(?P<action>approve|reject|info)"):
    LABELS = {
        "approve": ("Допустить · Approve", discord.ButtonStyle.success),
        "reject": ("Отклонить · Reject", discord.ButtonStyle.danger),
        "info": ("Нужно уточнение · Need info", discord.ButtonStyle.secondary),
    }

    def __init__(self, req_id: int, action: str) -> None:
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"wb:ver:{req_id}:{action}"))
        self.req_id = req_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: WardenBot = interaction.client  # type: ignore[assignment]
        lang = lang_of(interaction)
        if not isinstance(interaction.user, discord.Member) or not can_screen(bot, interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        if self.action == "approve":
            await interaction.response.defer(ephemeral=True)
            await decide(interaction, self.req_id, "approved", "")
        else:
            await interaction.response.send_modal(ReasonModal(self.req_id, self.action, lang, interaction.message))


def review_view(req_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("approve", "reject", "info"):
        view.add_item(ReviewButton(req_id, action))
    return view


class ReasonModal(discord.ui.Modal):
    def __init__(self, req_id: int, action: str, lang: str, message: discord.Message | None) -> None:
        super().__init__(title=t("ver_reason_title", lang), timeout=600)
        self.req_id, self.action, self.lang, self.message = req_id, action, lang, message
        self.reason = discord.ui.TextInput(
            label=t("ver_reason_label", lang), style=discord.TextStyle.paragraph, max_length=500,
            required=action == "reject",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        status = "rejected" if self.action == "reject" else "needs_info"
        await decide(interaction, self.req_id, status, self.reason.value.strip(), self.message)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        from ..services import report_error
        await report_error(interaction.client, interaction, error, "verification reason modal")  # type: ignore[arg-type]


async def decide(
    interaction: discord.Interaction, req_id: int, status: str, reason: str,
    message: discord.Message | None = None,
) -> None:
    bot: WardenBot = interaction.client  # type: ignore[assignment]
    lang = lang_of(interaction)
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not can_screen(bot, interaction.user):
        await reply(interaction, t("err_no_perm", lang))
        return
    message = message or interaction.message
    now = utcnow()

    def work(conn):
        req = repo.req_get(conn, req_id)
        if req is None or req['guild_id'] != guild.id:
            return None, False
        if message is None or req['review_message_id'] != message.id:
            return req, False
        done = repo.req_decide(conn, req_id, status, interaction.user.id, reason, now)
        if not done:
            return req, False
        uid = req["user_id"]
        if status == "approved":
            repo.user_update(
                conn, uid, now, status="admitted", rank="recruit", admitted_at=to_iso(now),
                admitted_by=interaction.user.id, steam_key=req["steam_key"], steam_url=req["steam_url"],
                ingame_name=req["ingame_name"], regiment=req["regiment"],
            )
        elif status == "rejected":
            repo.user_update(conn, uid, now, status="rejected", last_reject_at=to_iso(now))
        return req, True

    req, ok = await bot.db.write(work)
    if req is None or not ok:
        await interaction.followup.send(t("ver_decided", lang), ephemeral=True)
        return
    uid = req["user_id"]
    member = guild.get_member(uid)
    if status == "approved" and member is not None:
        try:
            await set_rank_roles(bot, member, "recruit", reason=f"Verification #{req_id}")
        except discord.HTTPException:
            log.warning("Could not grant recruit role to %s", uid, exc_info=True)
    if member is not None:
        key = {"approved": "ver_approved_dm", "rejected": "ver_rejected_dm", "needs_info": "ver_info_dm"}[status]
        text = f"{t(key, 'ru', reason=reason)}\n{t(key, 'en', reason=reason)}"
        await safe_dm(member, text)
    # Update the card: drop the buttons and the screenshot.
    if message is not None:
        note = f"{status} · <@{interaction.user.id}>" + (f"\n{reason}" if reason else "")
        embed = build_review_embed(req, member, status, note)
        try:
            await message.edit(embed=embed, view=None, attachments=[])
        except discord.HTTPException:
            log.warning("Could not update review card", exc_info=True)
    await audit(bot, guild, interaction.user, f"Verification {status}", f"<@{uid}>", reason)
    await interaction.followup.send(t("ver_done_ok", lang), ephemeral=True)


class VerificationCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyPanelView())
        self.bot.add_dynamic_items(ReviewButton)

    verify = app_commands.Group(name="verify", description="Verification · Допуск", guild_only=True)

    @verify.command(name="screenshot", description="Send the screenshot for your request · Отправить скриншот")
    @app_commands.describe(image="Screenshot with your code on it · Скриншот с вашим кодом")
    async def screenshot(self, interaction: discord.Interaction, image: discord.Attachment) -> None:
        lang = lang_of(interaction)
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await reply(interaction, t("err_guild_only", lang))
            return
        verify_ch = channel_mention(self.bot, guild.id, "verify")
        req = await self.bot.db.read(lambda conn: repo.req_latest_for_user(conn, member.id))
        if req is None or req['guild_id'] != guild.id or req["status"] not in ("awaiting_screenshot", "needs_info"):
            await reply(interaction, t("ver_no_open", lang, verify=verify_ch))
            return
        ctype = (image.content_type or "").lower()
        if not ctype.startswith("image/") or image.size > MAX_IMAGE_BYTES:
            await reply(interaction, t("ver_bad_image", lang))
            return
        channel = get_text_channel(self.bot, guild, "screening")
        if not isinstance(channel, discord.TextChannel):
            await reply(interaction, t("ver_no_review", lang))
            return
        await interaction.response.defer(ephemeral=True)
        file = await image.to_file(filename="screenshot" + (image.filename[image.filename.rfind("."):] if "." in image.filename else ".png"))
        embed = build_review_embed(req, member)
        embed.set_image(url=f"attachment://{file.filename}")
        message = await channel.send(embed=embed, file=file, view=review_view(req["id"]), allowed_mentions=discord.AllowedMentions.none())

        def work(conn):
            return repo.req_attach_review(conn, req['id'], req['status'], req['review_message_id'],
                                          file.filename, channel.id, message.id)

        if not await self.bot.db.write(work):
            # Another upload or a moderator decision won the race. Never leave
            # an extra screenshot/card available for a second decision.
            await message.delete()
            await interaction.followup.send(t('ver_no_open', lang, verify=verify_ch), ephemeral=True)
            return
        await interaction.followup.send(t("ver_sent", lang), ephemeral=True)

    @verify.command(name="status", description="Check your request · Статус заявки")
    async def status(self, interaction: discord.Interaction) -> None:
        lang = lang_of(interaction)
        req = await self.bot.db.read(lambda conn: repo.req_latest_for_user(conn, interaction.user.id))
        if req is None:
            await reply(interaction, t("ver_status_none", lang))
        else:
            await reply(interaction, t("ver_status", lang, status=req["status"]))


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(VerificationCog(bot))
