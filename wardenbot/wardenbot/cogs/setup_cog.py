"""/setup: builds roles, categories, channels, permissions, forum tags, AutoMod and the info panels."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .. import repo, role_jobs
from ..bot import WardenBot
from ..i18n import user_language, t
from ..invite import role_permissions
from ..layout import CATEGORIES, ROLES, LEGACY_ROLE_KEYS, CategoryDef, ChannelDef, role_display_name
from ..invite import required_permission_names
from ..perm_layout import category_overwrites, channel_overwrites
from ..perms import is_admin
from ..services import BLUE, audit, get_text_channel, reply

log = logging.getLogger(__name__)
TEXTS = Path(__file__).resolve().parent.parent / "texts"

AUTOMOD_NAME = "Warden: stockpile codes"
AUTOMOD_REGEX = r"\b\d{6}\b"  # Discord uses Rust regex: no lookaround


def read_text(name: str) -> str:
    return (TEXTS / name).read_text(encoding="utf-8").strip()


class SetupCog(commands.Cog):
    def __init__(self, bot: WardenBot) -> None:
        self.bot = bot

    setup = app_commands.Group(
        name="setup", description="Set up the server · Настройка сервера",
        default_permissions=discord.Permissions(administrator=True), guild_only=True,
    )

    # --------------------------------------------------------------- roles --
    async def _ensure_roles(self, guild: discord.Guild, log_lines: list[str]) -> dict[str, discord.Role]:
        cfg = self.bot.cfg(guild.id)
        result: dict[str, discord.Role] = {}
        for rd in ROLES:  # highest first
            role = None
            stored = cfg.get(f"role.{rd.key}")
            if stored and stored.isdigit():
                role = guild.get_role(int(stored))
            if role is None:
                name = role_display_name(rd)
                role = discord.utils.get(guild.roles, name=name)
            if role is None:
                legacy_en = {'command': 'Command', 'staff': 'Staff', 'regiment_co': 'Regiment CO',
                             'lang_ru': 'Language: Russian', 'lang_en': 'Language: English',
                             'sergeant': 'Sergeant', 'private': 'Private', 'recruit': 'Recruit'}.get(rd.key, rd.en)
                legacy_name = f"{rd.symbol} {rd.ru} · {legacy_en}".strip()
                role = discord.utils.get(guild.roles, name=legacy_name)
            if role is None and rd.key in ('lang_ru', 'lang_en'):
                from ..languages import ROLE_NAMES
                language = 'ru' if rd.key == 'lang_ru' else 'en'
                role = next((r for r in guild.roles if r.name.casefold() in ROLE_NAMES[language]), None)
            if role is None and rd.key in LEGACY_ROLE_KEYS:
                continue
            perms = role_permissions(rd)
            if role is None:
                role = await guild.create_role(
                    name=role_display_name(rd), colour=discord.Colour(rd.color), hoist=rd.hoist,
                    mentionable=rd.mentionable, permissions=perms, reason="Warden setup",
                )
                log_lines.append(f"Создана роль: {role.name}")
            elif role.name != role_display_name(rd):
                role = await role.edit(name=role_display_name(rd), reason="Имена ролей на английском")
            await self.bot.set_cfg(guild.id, f"role.{rd.key}", role.id)
            result[rd.key] = role
        # Order: our roles go just under the bot's own role, highest first. This only works when
        # the bot role is placed high enough (at least one place per role below it).
        top = guild.me.top_role.position
        if top - len(result) < 1:
            log_lines.append("Поднимите роль бота выше остальных ролей и повторите /setup run")
        else:
            try:
                positions = {result[rd.key]: top - 1 - index for index, rd in enumerate(rd for rd in ROLES if rd.key in result)}
                await guild.edit_role_positions(positions, reason="Warden setup: order roles")
            except (discord.Forbidden, discord.HTTPException):
                log_lines.append("Порядок ролей не изменён: поднимите роль бота вручную")
        return result

    # ------------------------------------------------------------ channels --
    def _target_overwrites(
        self, mapping: dict[str, discord.PermissionOverwrite], roles: dict[str, discord.Role], guild: discord.Guild,
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        out: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        for key, ow in mapping.items():
            if key == "everyone":
                out[guild.default_role] = ow
            elif key == "bot":
                out[guild.me] = ow
            elif key in roles:
                out[roles[key]] = ow
        return out

    async def _ensure_category(
        self, guild: discord.Guild, cat: CategoryDef, roles: dict[str, discord.Role], log_lines: list[str],
    ) -> discord.CategoryChannel:
        cfg = self.bot.cfg(guild.id)
        existing = None
        stored = cfg.get(f"cat.{cat.key}")
        if stored and stored.isdigit():
            existing = guild.get_channel(int(stored))
        overwrites = self._target_overwrites(category_overwrites(cat), roles, guild)
        if isinstance(existing, discord.CategoryChannel):
            if cat.key in ("entry", "command"):
                await existing.edit(name=cat.name, overwrites=overwrites, reason="Язык и доступ категории")
            return existing
        created = await guild.create_category(name=cat.name, overwrites=overwrites, reason="Warden setup")
        await self.bot.set_cfg(guild.id, f"cat.{cat.key}", created.id)
        log_lines.append(f"Создана категория: {created.name}")
        return created

    async def _ensure_channel(
        self, guild: discord.Guild, category: discord.CategoryChannel, ch: ChannelDef,
        roles: dict[str, discord.Role], fix: bool, log_lines: list[str],
    ) -> discord.abc.GuildChannel:
        cfg = self.bot.cfg(guild.id)
        name = ch.name(self.bot.config.channel_name_style)
        existing = None
        stored = cfg.get(f"ch.{ch.key}")
        if stored and stored.isdigit():
            existing = guild.get_channel(int(stored))
        overwrites = self._target_overwrites(channel_overwrites(ch), roles, guild)
        if existing is not None:
            # Trading channels and their configured permissions are explicitly preserved.
            if ch.key in ("sell", "buy", "services"):
                return existing
            policy_channel = ch.access in ("public", "staff", "screening", "command") or ch.key == "news"
            if policy_channel:
                changes = dict(name=name, overwrites=overwrites, reason="Язык и доступ канала")
                if ch.kind != "voice":
                    changes['topic'] = ch.topic
                await existing.edit(**changes)
            elif fix:
                try:
                    await existing.edit(overwrites=overwrites, reason="Warden setup: fix permissions")
                    log_lines.append(f"Права восстановлены: {existing.name}")
                except discord.HTTPException:
                    log_lines.append(f"Не удалось восстановить права: {existing.name}")
            return existing
        if ch.kind == "text":
            created = await guild.create_text_channel(
                name=name, category=category, topic=ch.topic, overwrites=overwrites, reason="Warden setup",
            )
        elif ch.kind == "voice":
            created = await guild.create_voice_channel(
                name=name, category=category, overwrites=overwrites, reason="Warden setup",
            )
        elif ch.kind == "forum":
            tags = [discord.ForumTag(name=n) for n in ch.forum_tags]
            created = await guild.create_forum(
                name=name, category=category, topic=(ch.guidelines or ch.topic)[:4096], overwrites=overwrites,
                available_tags=tags, reason="Warden setup",
            )
        else:  # pragma: no cover
            raise ValueError(ch.kind)
        await self.bot.set_cfg(guild.id, f"ch.{ch.key}", created.id)
        log_lines.append(f"Создан канал: {created.name}")
        return created

    # ------------------------------------------------------------- automod --
    async def _ensure_automod(self, guild: discord.Guild, log_lines: list[str]) -> None:
        try:
            rules = await guild.fetch_automod_rules()
            if any(r.name == AUTOMOD_NAME for r in rules):
                return
            exempt_channels = [
                c for key in ("mod", "screening", "bot_admin", "verify")
                if (c := get_text_channel(self.bot, guild, key)) is not None
            ]
            exempt_roles = [r for key in ("staff", "command") if (r := guild.get_role(int(self.bot.cfg(guild.id).get(f"role.{key}", 0) or 0)))]
            await guild.create_automod_rule(
                name=AUTOMOD_NAME,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword, regex_patterns=[AUTOMOD_REGEX],
                ),
                actions=[discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message,
                                                   custom_message="Stockpile codes are not allowed here. Коды складов здесь запрещены.")],
                enabled=True,
                exempt_roles=exempt_roles,
                exempt_channels=exempt_channels,
                reason="Warden setup",
            )
            log_lines.append("Создан AutoMod для кодов складов")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("AutoMod rule not created: %s", exc)
            log_lines.append("AutoMod не создан: настройте правило вручную по README")

    # ---------------------------------------------------------- guild settings
    async def _guild_settings(self, guild: discord.Guild, log_lines: list[str]) -> None:
        try:
            await guild.edit(
                verification_level=discord.VerificationLevel.medium,
                explicit_content_filter=discord.ContentFilter.all_members,
                default_notifications=discord.NotificationLevel.only_mentions,
                reason="Warden setup",
            )
            log_lines.append("Настройки сервера: средняя проверка, фильтр для всех участников")
        except (discord.Forbidden, discord.HTTPException):
            log_lines.append("Настройки сервера не изменены")
        rules = get_text_channel(self.bot, guild, "rules")
        news = get_text_channel(self.bot, guild, "news")
        if guild.features and "COMMUNITY" in guild.features and isinstance(rules, discord.TextChannel) and isinstance(news, discord.TextChannel):
            try:
                await guild.edit(rules_channel=rules, public_updates_channel=news, reason="Warden setup")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # -------------------------------------------------------------- panels --
    async def _upsert(
        self, guild: discord.Guild, cfg_key: str, channel: discord.TextChannel, *,
        embeds: list[discord.Embed], view: discord.ui.View | None = None,
    ) -> discord.Message:
        stored = self.bot.cfg(guild.id).get(f"msg.{cfg_key}")
        if stored and stored.isdigit():
            try:
                msg = await channel.fetch_message(int(stored))
                await msg.edit(embeds=embeds, view=view)
                return msg
            except discord.NotFound:
                pass
        msg = await channel.send(embeds=embeds, view=view)
        await self.bot.set_cfg(guild.id, f"msg.{cfg_key}", msg.id)
        return msg

    async def post_panels(self, guild: discord.Guild) -> list[str]:
        from .verification import build_verify_view

        notes: list[str] = []
        rules = get_text_channel(self.bot, guild, "rules")
        start = get_text_channel(self.bot, guild, "start")
        verify = get_text_channel(self.bot, guild, "verify")
        if isinstance(rules, discord.TextChannel):
            en = [discord.Embed(description=read_text(f"rules_en_{i}.md"), color=BLUE) for i in (1, 2)]
            en[0].title = "Rules"
            await self._upsert(guild, "rules_en", rules, embeds=en)
            # Reuse the old Russian panel ID instead of deleting a message or
            # leaving pre-verification instructions in the wrong language.
            if self.bot.cfg(guild.id).get("msg.rules_ru"):
                await self._upsert(guild, "rules_ru", rules, embeds=[discord.Embed(
                    title="Verification", description="Read the rules above, then use Verify to apply.", color=BLUE)])
            notes.append("rules")
        if isinstance(start, discord.TextChannel):
            embed = discord.Embed(
                title="Welcome",
                description="A community for Warden players. Read the rules, then use Verify to apply. "
                            "Choose Russian or English in the verification form.",
                color=BLUE,
            )
            await self._upsert(guild, "start", start, embeds=[embed], view=build_verify_view())
            notes.append("start")
        if isinstance(verify, discord.TextChannel):
            embed = discord.Embed(
                title="Verification",
                description="Enter your in-game name, optional clan and preferred language. "
                            "Type your one-time code in the in-game chat. Upload ONE screenshot "
                            "showing both F1 and the chat with your code using /verify screenshot. "
                            "A moderator will review your request.",
                color=BLUE,
            )
            await self._upsert(guild, "verify", verify, embeds=[embed], view=build_verify_view())
            notes.append("verify")
        return notes

    # ------------------------------------------------------------ commands --
    @setup.command(name="run", description="Create or repair roles and channels · Создать или починить роли и каналы")
    @app_commands.describe(
        include_later="Also create the channels planned for later stages · Создать и поздние каналы",
        guild_settings="Apply recommended server security settings · Применить настройки безопасности",
        fix_permissions="Reset permissions of existing channels to the plan · Вернуть права каналов к плану",
    )
    async def setup_run(
        self, interaction: discord.Interaction, include_later: bool = False,
        guild_settings: bool = True, fix_permissions: bool = False,
    ) -> None:
        lang = "ru"
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        missing = [p for p in required_permission_names() if not getattr(guild.me.guild_permissions, p)]
        if missing and not guild.me.guild_permissions.administrator:
            await reply(interaction, t("setup_missing_perms", lang, perms=", ".join(missing)))
            return
        if "COMMUNITY" not in guild.features:
            await reply(interaction, t("setup_need_community", lang))
            return
        await interaction.response.send_message(t("setup_started", lang), ephemeral=True)
        lines: list[str] = []
        try:
            roles = await self._ensure_roles(guild, lines)
            for cat in CATEGORIES:
                chans = [c for c in cat.channels if c.launch or include_later]
                if not chans:
                    continue
                category = await self._ensure_category(guild, cat, roles, lines)
                for ch in chans:
                    await self._ensure_channel(guild, category, ch, roles, fix_permissions, lines)
            await self._ensure_automod(guild, lines)
            if guild_settings:
                await self._guild_settings(guild, lines)
            def queue_roles(conn):
                for member in guild.members:
                    if member.bot:
                        continue
                    user = repo.user_get(conn, member.id)
                    target = (user['rank'] or 'recruit') if user and user['status'] == 'admitted' else (
                        'rejected' if user and user['status'] in ('rejected', 'banned') else 'unverified')
                    repo.role_job_set(conn, guild.id, member.id, target)
                    if user and user['status'] == 'admitted':
                        from ..languages import member_language
                        language = member_language(self.bot.cfg(guild.id), member, user['language'])
                        repo.user_update(conn, member.id, discord.utils.utcnow(), language=language)
                        role_jobs.enqueue(conn, guild.id, member.id, 'language', language)
            await self.bot.db.write(queue_roles)
            notes = await self.post_panels(guild)
            lines.append("Панели: " + ", ".join(notes))
        except discord.Forbidden as exc:
            await interaction.followup.send(f"Forbidden: {exc}. {t('setup_missing_perms', lang, perms='?')}", ephemeral=True)
            return
        await audit(self.bot, guild, interaction.user, "Настройка сервера", details="; ".join(lines)[:900])
        body = "\n".join(f"• {line}" for line in lines[:40]) or "—"
        await interaction.followup.send(f"{t('setup_done', lang)}\n{body}"[:1900], ephemeral=True)

    @setup.command(name="panels", description="Re-post rules, start and verification messages · Обновить сообщения")
    async def setup_panels(self, interaction: discord.Interaction) -> None:
        lang = "ru"
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        await interaction.response.defer(ephemeral=True)
        await self.post_panels(interaction.guild)
        await interaction.followup.send(t("setup_panels_done", lang), ephemeral=True)

    @setup.command(name="status", description="Show what is configured · Показать состояние")
    async def setup_status(self, interaction: discord.Interaction) -> None:
        lang = "ru"
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await reply(interaction, t("err_no_perm", lang))
            return
        cfg = self.bot.cfg(guild.id)
        lines = []
        for rd in ROLES:
            rid = cfg.get(f"role.{rd.key}")
            if not rid and rd.key in LEGACY_ROLE_KEYS:
                continue
            ok = bool(rid and rid.isdigit() and guild.get_role(int(rid)))
            lines.append(f"{'✓' if ok else '✗'} role {rd.key}")
        for cat in CATEGORIES:
            for ch in cat.channels:
                cid = cfg.get(f"ch.{ch.key}")
                if cid and cid.isdigit() and guild.get_channel(int(cid)):
                    lines.append(f"✓ #{ch.key}")
                elif ch.launch:
                    lines.append(f"✗ #{ch.key}")
        await reply(interaction, "\n".join(lines)[:1900])


async def setup(bot: WardenBot) -> None:
    await bot.add_cog(SetupCog(bot))
