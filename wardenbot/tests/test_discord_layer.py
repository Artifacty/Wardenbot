"""Checks of the Discord-facing code against the installed discord.py (limits, permissions, flows).
Skipped when discord.py is not installed."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip("discord")

from wardenbot.cogs import operations

from wardenbot import i18n, repo  # noqa: E402
from wardenbot.bot import EXTENSIONS, WardenBot  # noqa: E402
from wardenbot.cogs import verification  # noqa: E402
from wardenbot.config import Config  # noqa: E402
from wardenbot.db import Database  # noqa: E402
from wardenbot.layout import ALL_CHANNELS, CATEGORIES, ROLES, viewer_roles  # noqa: E402
from wardenbot.opsmeta import ALL_FORUM_TAGS  # noqa: E402
from wardenbot.perm_layout import category_overwrites, channel_overwrites  # noqa: E402
from wardenbot.render import OpView, SlotView, SupplyView, build_card  # noqa: E402
from wardenbot.slots import SlotSpec, SupplySpec  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ layout --
def test_layout_fits_discord_limits():
    assert len(ROLES) <= 250
    for cat in CATEGORIES:
        assert len(cat.name) <= 100, cat.name
        for style in ("full", "ru", "en"):
            for ch in cat.channels:
                assert 1 <= len(ch.name(style)) <= 100, ch.name(style)
        for ch in cat.channels:
            assert len(ch.topic) <= 1024
            assert len(ch.guidelines) <= 4096
    assert len(ALL_FORUM_TAGS) <= 20
    for tag in ALL_FORUM_TAGS:
        assert len(tag) <= 20, tag


def test_every_overwrite_is_valid_and_secure():
    for cat in CATEGORIES:
        c = category_overwrites(cat)
        assert "bot" in c
        for ch in cat.channels:
            ow = channel_overwrites(ch)
            assert "bot" in ow and "everyone" in ow
            everyone = ow["everyone"]
            if ch.access == "public":
                assert everyone.view_channel is True
                assert everyone.send_messages is False or ch.mode == "chat"
            else:
                assert everyone.view_channel is False
                # newcomers and allies see only what the plan allows
                if ch.access in ("private", "sergeant", "staff", "screening", "command", "co"):
                    assert "recruit" not in ow, ch.key
                if not ch.allies:
                    assert "ally" not in ow, ch.key


def test_read_only_and_bot_forum_rules():
    by_key = {c.key: c for c in ALL_CHANNELS}
    ow = channel_overwrites(by_key["ops_board"])
    assert ow["recruit"].send_messages is False  # cannot create posts, only use the bot's buttons
    assert ow["recruit"].send_messages_in_threads is True
    assert ow["staff"].send_messages is True
    rules = channel_overwrites(by_key["rules"])
    assert rules["everyone"].send_messages is False
    assert rules["everyone"].add_reactions is False
    screening = channel_overwrites(by_key["screening"])
    assert set(screening) == {"everyone", "bot", *viewer_roles("screening")}


# ------------------------------------------------------------------ bot --
def make_bot():
    return WardenBot(Config(token="x"), Database(":memory:"))


def test_all_extensions_load_and_commands_are_valid():
    async def go():
        bot = make_bot()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        bot.get_cog("OperationsCog").tick.cancel()
        bot.get_cog("HousekeepingCog").promotions.cancel()
        names = set()
        for cmd in bot.tree.get_commands():
            d = cmd.to_dict(bot.tree)
            names.add(cmd.name)
            assert len(d["description"]) <= 100, d["description"]
            for opt in d.get("options", []):
                assert len(opt["description"]) <= 100, (cmd.name, opt["name"])
                for sub in opt.get("options", []):
                    assert len(sub["description"]) <= 100, (cmd.name, opt["name"], sub["name"])
        assert {"setup", "verify", "operation", "priority", "member", "timezone", "help"} <= names
        for cog in list(bot.cogs):
            await bot.remove_cog(cog)

    run(go())


def test_dynamic_item_templates_match_their_ids():
    for item in (operations.OpButton(12, "signup"), operations.OpButton(12, "manage"), verification.ReviewButton(7, "reject")):
        assert item.template.fullmatch(item.item.custom_id), item.item.custom_id
        assert len(item.item.custom_id) <= 100
    from wardenbot.cogs.selfroles import RoleButton
    assert RoleButton.__discord_ui_compiled_template__.fullmatch(RoleButton("ping_ops").item.custom_id)


# ---------------------------------------------------------------- modals --
@pytest.mark.parametrize("lang", ["ru", "en"])
def test_modals_respect_discord_limits(lang):
    cog = SimpleNamespace(bot=make_bot())
    draft = operations.Draft(1, 2, "offense", "both", "UTC", datetime.now(UTC))
    modals = [
        operations.BasicsModal(cog, draft, lang),
        operations.DetailsModal(cog, draft, lang),
        operations.SupplyModal(cog, 1, lang, ["BMats", "Fuel"]),
        operations.BriefingModal(cog, 1, lang, {"briefing_public": "a", "briefing_closed": "b"}),
        verification.VerifyModal(lang),
        verification.ReasonModal(1, "reject", lang, None),
    ]
    from wardenbot.cogs.priorities import PriorityModal
    modals.append(PriorityModal(SimpleNamespace(bot=cog.bot), 1, lang, None))
    for modal in modals:
        assert len(modal.title) <= 45, modal.title
        assert 1 <= len(modal.children) <= 5
        for field in modal.children:
            wrapped = isinstance(field, discord.ui.Label)
            if isinstance(field, discord.ui.Label):
                assert len(field.text) <= 45
                field = field.component
                if isinstance(field, discord.ui.Select):
                    assert len(field.options) <= 25
                    continue
            if wrapped:
                assert field.to_component_dict().get('label') is None
            else:
                assert len(field.label) <= 45, field.label
            if field.default:
                assert len(field.default) <= field.max_length, field.label
            if field.placeholder:
                assert len(field.placeholder) <= 100


def test_default_slot_templates_parse_and_fit():
    from wardenbot.opsmeta import DEFAULT_SLOTS, DEFAULT_SUPPLY
    from wardenbot.slots import parse_slots, parse_supply
    for key, text in DEFAULT_SLOTS.items():
        assert len(text) <= 600, key
        assert parse_slots(text)
    assert len(DEFAULT_SUPPLY) <= 300 and parse_supply(DEFAULT_SUPPLY)


# ---------------------------------------------------------------- embeds --
def test_worst_case_card_fits_in_one_embed():
    slots = [SlotView(i, "Очень длинное название слота " * 2, 200, list(range(100000000000000000 + i * 50, 100000000000000000 + i * 50 + 50)))
             for i in range(20)]
    op = OpView(
        id=1, title="T" * 80, region="R" * 40, type="offense", language="both",
        start_at=datetime(2026, 11, 8, tzinfo=UTC), duration_min=120, organizer_id=1, requirements="r" * 400,
        briefing_public="b" * 1500, status="open", urgent=True, slots=slots,
        supply=[SupplyView(f"Item {i}", 1000, 10) for i in range(10)],
    )
    embed = operations.card_embed(build_card(op))
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    for f in embed.fields:
        assert len(f.name) <= 256 and len(f.value) <= 1024


# ----------------------------------------------------------------- i18n --
def test_every_used_i18n_key_exists_and_both_languages_filled():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "wardenbot"
    used = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"""\bt\(\s*["']([a-z0-9_]+)["']""", text))
        used |= set(re.findall(r"""["'](op_[a-z0-9_]+|err_[a-z0-9_]+)["']""", text)) & set(i18n.S)
    missing = sorted(k for k in used if k not in i18n.S and not k.startswith("err_bad_") and not k.startswith("err_dup"))
    assert not missing, missing
    for key, entry in i18n.S.items():
        assert entry["ru"].strip() and entry["en"].strip(), key


def test_all_format_placeholders_are_formattable():
    import string
    for key, entry in i18n.S.items():
        ru_names = {f for _, f, _, _ in string.Formatter().parse(entry["ru"]) if f}
        en_names = {f for _, f, _, _ in string.Formatter().parse(entry["en"]) if f}
        assert ru_names == en_names, key


# ------------------------------------------------------------ flow tests --
def fake_member(user_id, role_ids=(), cls=discord.Member):
    m = MagicMock(spec=cls)
    m.id = user_id
    m.roles = [SimpleNamespace(id=r, name=f"role-{r}") for r in role_ids]
    m.guild = SimpleNamespace(id=1)
    m.guild_permissions = SimpleNamespace(administrator=False)
    return m


def fake_interaction(user, guild_id=1, bot=None):
    inter = MagicMock()
    inter.client = bot
    inter.user = user
    inter.locale = "en-US"
    inter.guild = SimpleNamespace(id=guild_id)
    inter.guild_id = guild_id
    inter.response.is_done.return_value = False
    async def defer(**kwargs):
        inter.response.is_done.return_value=True
    inter.response.defer = AsyncMock(side_effect=defer)
    inter.edit_original_response = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def test_signup_flow_end_to_end_with_capacity():
    async def go():
        bot = make_bot()
        def make_interaction(user):
            return fake_interaction(user, bot=bot)
        await bot.load_extension("wardenbot.cogs.operations")
        cog = bot.get_cog("OperationsCog")
        cog.tick.cancel()
        cog.archive_tick.cancel()
        bot.cfg(1)["role.recruit"] = "500"
        now = datetime.now(UTC)
        op_id = await bot.db.write(
            lambda c: repo.op_create(
                c, guild_id=1, organizer_id=99, title="Test", region="R", type="offense", language="both",
                start_at=now + timedelta(hours=3), duration_min=120, requirements="", briefing_public="",
                briefing_closed="secret plan", slots=[SlotSpec("Inf", 1), SlotSpec("AT", 2)], supply=[SupplySpec("BMats", 100)], now=now,
            )
        )
        for uid in (11, 12, 99):
            await bot.db.write(repo.user_update, uid, now, status="admitted", language="en")
        # an unverified user is turned away
        stranger = make_interaction(fake_member(10))
        await cog.on_signup(stranger, op_id)
        assert "допуск" in stranger.response.send_message.call_args.args[0].lower()

        # a recruit gets the slot picker
        u1 = make_interaction(fake_member(11, [500]))
        await cog.on_signup(u1, op_id)
        view = u1.response.send_message.call_args.kwargs["view"]
        assert type(view).__name__ == "SlotSelectView"  # load_extension re-imports the module
        slots = await bot.db.read(repo.op_slots, op_id)
        view.select._values = [str(slots[0]["id"])]  # type: ignore[attr-defined]
        pick = make_interaction(fake_member(11, [500]))
        await view.chosen(pick)
        assert "записаны" in pick.edit_original_response.call_args.kwargs["content"]

        # the 1-seat slot is now full for the next user
        u2 = make_interaction(fake_member(12, [500]))
        await cog.on_signup(u2, op_id)
        view2 = u2.response.send_message.call_args.kwargs["view"]
        view2.select._values = [str(slots[0]["id"])]  # type: ignore[attr-defined]
        pick2 = make_interaction(fake_member(12, [500]))
        await view2.chosen(pick2)
        assert "заполнен" in pick2.edit_original_response.call_args.kwargs["content"]

        # the closed briefing: not for strangers, not before T-60, yes for the organizer
        b1 = make_interaction(fake_member(11, [500]))
        await cog.on_brief(b1, op_id)
        assert "откроется" in b1.response.send_message.call_args.args[0]
        b2 = make_interaction(fake_member(12, [500]))
        await cog.on_brief(b2, op_id)
        assert "записавшимся" in b2.response.send_message.call_args.args[0]
        b3 = make_interaction(fake_member(99, [500]))
        await cog.on_brief(b3, op_id)
        assert b3.response.send_message.call_args.kwargs["embed"].description == "secret plan"

        # Revocation blocks a previously opened picker and the private briefing.
        await bot.db.write(repo.user_update, 12, now, status="rejected")
        stale = make_interaction(fake_member(12, [500]))
        await view2.chosen(stale)
        assert stale.response.send_message.called
        assert await bot.db.read(repo.op_signup_of, op_id, 12) is None
        await bot.db.write(repo.user_update, 11, now, status="rejected")
        denied = make_interaction(fake_member(11, [500]))
        await cog.on_brief(denied, op_id)
        assert "embed" not in denied.response.send_message.call_args.kwargs

        # leaving
        lv = make_interaction(fake_member(11, [500]))
        await cog.on_leave(lv, op_id)
        assert "отменена" in lv.response.send_message.call_args.args[0]
        for task in cog._pending.values():
            task.cancel()

    run(go())


def test_tick_enqueues_delivery_and_never_auto_closes():
    from wardenbot import notifications
    async def go():
        bot=make_bot()
        now=datetime.now(UTC)
        op_id=await bot.db.write(lambda c:repo.op_create(c,guild_id=1,organizer_id=99,title='Test',region='',type='offense',language='ru',start_at=now+timedelta(minutes=15),duration_min=60,requirements='',briefing_public='',briefing_closed='',slots=[SlotSpec('Inf',5)],supply=[],now=now))
        await bot.db.write(lambda c:repo.op_update(c,op_id,thread_id=123))
        await bot.db.write(notifications.schedule,now)
        jobs=await bot.db.read(notifications.pending)
        assert [j['kind'] for j in jobs]==['remind15']
        await bot.db.write(notifications.schedule,now+timedelta(days=3))
        assert (await bot.db.read(repo.op_get,op_id))['status']=='running'
        assert [j['kind'] for j in await bot.db.read(notifications.pending)]==['started']
        bot.db.close()
    run(go())


def test_review_card_flags_and_embed():
    flags = "dup_steam:111,222;young_account"
    lines = verification.flag_lines(flags)
    assert any("<@111>" in line and "<@222>" in line for line in lines)
    assert any("Новый аккаунт" in line for line in lines)
    req = {"id": 3, "user_id": 5, "ingame_name": "X", "regiment": "", "steam_url": "https://steamcommunity.com/id/x",
           "voucher_text": "", "code": "WH-ABCD", "flags": flags}
    embed = verification.build_review_embed(req, None)
    assert len(embed) < 6000 and "WH-ABCD" in embed.fields[5].value


# ---------------------------------------------------------------- invite --
def test_invite_permissions_cover_everything_setup_writes_and_exclude_admin():
    from wardenbot import invite
    from wardenbot.layout import ROLES

    needed = invite.required_permissions()
    assert not needed.administrator
    for cat in CATEGORIES:
        for ch in cat.channels:
            for ow in channel_overwrites(ch).values():
                allow, _ = ow.pair()
                assert allow.value & needed.value == allow.value, ch.key
    for rd in ROLES:
        granted = invite.role_permissions(rd)
        assert granted.value & needed.value == granted.value, rd.key
        assert not granted.administrator
    url = invite.invite_url(123)
    assert "client_id=123" in url and f"permissions={needed.value}" in url and "applications.commands" in url
