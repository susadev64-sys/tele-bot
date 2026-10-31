"""
bot.py - Anonymous Messaging & Social Hub Bot  (python-telegram-bot >= 21.8, job-queue extra)
Runs on Railway; ALL data lives in Supabase (PostgreSQL) via DATABASE_URL.

Flow overview
-------------
  /start                 -> personal link  t.me/<bot>?start=TOKEN   (Pro: custom slug)
  someone opens the link -> sees the owner's link-in-bio hub (bio, buttons, Pro: photo/video/GIF)
                            and their next message is delivered anonymously
  sender gets            -> "Message sent!" with  Send one more / Gift / Edit (Pro) / Delete
  "Send one more"        -> keeps the sender in the `sending` state: just type the next message
  owner receives a card  -> Reply / Block / Report + reactions (Pro: same-sender tag #U7383a)
  Inbox (/inbox)         -> filterable, paginated history of received messages + reactions + replies
  Reports                -> admins can ban the sender by hash (never seeing who it is)
  Free tier              -> receive messages + FREE_LINK_LIMIT social links
  Pro (Stars, monthly)   -> no ads, edit sent messages, sender tags, custom link, media hub,
                            unlimited links, extra icons, welcome text, thank-you sticker, themes
  Gifts (Stars)          -> senders can attach a virtual gift to their message

User "state" (what the bot waits for) lives in the DB so it survives restarts:
  sending | replying | editing | add_link | set_bio | set_welcome | set_sticker | set_slug | set_media
"""
import html
import logging
import re
import time
from datetime import datetime, timezone
from math import ceil
from typing import Optional
from urllib.parse import quote, urlparse

from telegram import (
    BotCommand,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    LabeledPrice,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import config
from database import Database, TAG_RE

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("anon_hub")

db: Database  # created in main()

# =============================================================================
# Static content
# =============================================================================
PLATFORMS = {
    "telegram":  dict(label="Telegram",  icon="✈️", tpl="https://t.me/{h}",              hosts=("t.me", "telegram.me"), pro=False),
    "instagram": dict(label="Instagram", icon="📸", tpl="https://instagram.com/{h}",     hosts=("instagram.com",),      pro=False),
    "tiktok":    dict(label="TikTok",    icon="🎵", tpl="https://tiktok.com/@{h}",       hosts=("tiktok.com",),         pro=False),
    "x":         dict(label="X",         icon="🐦", tpl="https://x.com/{h}",             hosts=("x.com", "twitter.com"), pro=False),
    "youtube":   dict(label="YouTube",   icon="▶️", tpl="https://youtube.com/@{h}",      hosts=("youtube.com", "youtu.be"), pro=False),
    "facebook":  dict(label="Facebook",  icon="📘", tpl="https://facebook.com/{h}",      hosts=("facebook.com", "fb.com"), pro=True),
    "snapchat":  dict(label="Snapchat",  icon="👻", tpl="https://snapchat.com/add/{h}",  hosts=("snapchat.com",),       pro=True),
    "github":    dict(label="GitHub",    icon="🐙", tpl="https://github.com/{h}",        hosts=("github.com",),         pro=True),
    "linkedin":  dict(label="LinkedIn",  icon="💼", tpl="https://linkedin.com/in/{h}",   hosts=("linkedin.com",),       pro=True),
    "twitch":    dict(label="Twitch",    icon="🎮", tpl="https://twitch.tv/{h}",         hosts=("twitch.tv",),          pro=True),
    "whatsapp":  dict(label="WhatsApp",  icon="💬", tpl="https://wa.me/{h}",             hosts=("wa.me",),              pro=True),
    "spotify":   dict(label="Spotify",   icon="🎧", tpl=None,                            hosts=("spotify.com",),        pro=True),
    "discord":   dict(label="Discord",   icon="🗨️", tpl=None,                            hosts=("discord.gg", "discord.com"), pro=True),
    "website":   dict(label="Website",   icon="🌐", tpl=None,                            hosts=None,                    pro=True),
}

# theme -> (header emoji, divider line, display name, pro_only)
THEMES = {
    "classic": ("✨", "━━━━━━━━━━━━━━", "Classic ✨", False),
    "neon":    ("⚡", "▰▰▰▰▰▰▰▰▰▰▰▰", "Neon ⚡",    True),
    "love":    ("💖", "❥❥❥❥❥❥❥❥❥❥",   "Love 💖",    True),
    "minimal": ("✉️", "──────────────", "Minimal ✉️", True),
}

REACTIONS = ["❤️", "🔥", "😂", "😮"]
KIND_LABEL = {"text": "💬 Text", "photo": "🖼 Photo", "video": "🎬 Video",
              "voice": "🎙 Voice message", "sticker": "🎭 Sticker", "gif": "🎞 GIF"}
KIND_ICON = {"text": "💬", "photo": "🖼", "video": "🎬", "voice": "🎙", "sticker": "🎭", "gif": "🎞"}
REPORT_REASONS = {"sp": "Spam", "ha": "Harassment / bullying", "th": "Threats", "ot": "Other"}

# inbox categories: key -> label
INBOX_FILTERS = {"all": "📥 All", "text": "💬 Text", "media": "🖼 Media",
                 "replied": "↩️ Replied", "noreply": "⏳ Unanswered", "reacted": "❤️ Reacted"}

HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,30}$")            # 3-31 chars
RESERVED_SLUGS = {"start", "help", "admin", "bot", "telegram", "support", "menu", "pro", "null", "none"}

PRO_FEATURES = (
    "🚫 <b>No ads</b> — clean, distraction-free experience\n"
    "✏️ <b>Edit sent messages</b> — fix typos even after delivery\n"
    "👤 <b>Sender tags</b> — spot repeat senders (<code>#U7383a</code>) without revealing who\n"
    "🔗 <b>Custom link</b> — <code>t.me/yourbot?start=your-name</code>\n"
    "🎬 <b>Media hub</b> — photo, video or GIF on your link-in-bio\n"
    f"♾ Unlimited social links (Free: {config.FREE_LINK_LIMIT}) + extra icons\n"
    "👋 Custom welcome text · 🎁 thank-you sticker · 🎨 premium themes"
)

TERMS_TEXT = (
    "📜 <b>Rules of use</b>\n\n"
    "• Be kind. No harassment, bullying, threats, hate or illegal content.\n"
    "• Never send sexual content involving minors.\n"
    "• Receivers can block and report any message. Reported messages are reviewed by the "
    "bot's admins and abusers are banned.\n"
    "• Anonymous doesn't mean untraceable by Telegram or the law — abuse can be reported to them.\n"
    "• Gifts and Pro are digital goods paid with Telegram Stars. Pro is a 30-day plan: it renews "
    "automatically only if you choose the subscription, and you can cancel any time in "
    "Telegram → Settings → My Stars → Subscriptions."
)

PRIVACY_TEXT = (
    "🔒 <b>Privacy</b>\n\n"
    "• Message text and media are <b>not stored</b> — they are passed straight to the receiver. "
    "Your inbox only keeps basic info (type, date, reaction, reply count) and re-opens messages "
    "from your own chat.\n"
    "• The sender's Telegram ID is stored <b>encrypted</b>, only so receivers can reply or react. "
    "It is never shown to anyone.\n"
    "• Pro receivers see a <b>sender tag</b> (e.g. <code>#U7383a</code>). It is a one-way code that is "
    "different for every receiver, so it can't identify anyone or be matched across people.\n"
    "• If a receiver <b>reports</b> a message, its content is shared with the bot's admins for review. "
    "Admins see a message number, not who sent it.\n"
    "• You can delete your own sent message with the 🗑 button right after sending.\n"
    "• /deleteme removes your profile, links and received-message records. Payment records are kept "
    "for accounting."
)


# =============================================================================
# Small helpers
# =============================================================================
def esc(text: Optional[str]) -> str:
    """Escape user-supplied text for Telegram HTML (prevents markup injection)."""
    return html.escape(text or "", quote=False)


def is_pro(user) -> bool:
    """Pro is active when flagged AND not expired. pro_until NULL = lifetime / admin grant."""
    if not user or not user["is_pro"]:
        return False
    until = user["pro_until"]
    return until is None or until > time.time()


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def theme_of(user):
    key = user["theme"] if is_pro(user) and user["theme"] in THEMES else "classic"
    return THEMES[key]


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%d %b %Y · %H:%M UTC")


def fmt_day(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def link_key(user) -> str:
    """Custom slug for Pro users (if set), otherwise the random token."""
    if is_pro(user) and user["custom_slug"]:
        return user["custom_slug"]
    return user["token"]


def bot_link(context: ContextTypes.DEFAULT_TYPE, user) -> str:
    return f"https://t.me/{context.bot.username}?start={link_key(user)}"


def resolve_owner(key: str):
    """Find the owner of a deep-link key. A custom slug only works while its owner is Pro."""
    owner = db.get_user_by_token(key)
    if owner and key != owner["token"] and not is_pro(owner):
        return None
    return owner


def ad_footer(user) -> str:
    """Sponsor line for FREE users only. Pro = 'No ads'."""
    return f"\n\n📢 <i>{esc(config.AD_TEXT)}</i>" if config.AD_TEXT and not is_pro(user) else ""


def plan_label(user) -> str:
    if not is_pro(user):
        return f"🆓 <b>Free</b> · links {db.count_links(user['user_id'])}/{config.FREE_LINK_LIMIT}"
    if user["pro_until"] is None:
        return "💎 <b>Pro</b> · lifetime"
    return f"💎 <b>Pro</b> · until {fmt_day(user['pro_until'])}"


def kind_of(msg) -> Optional[str]:
    if msg.text:
        return "text"
    if msg.photo:
        return "photo"
    if msg.sticker:
        return "sticker"
    if msg.voice:
        return "voice"
    if msg.video:
        return "video"
    if msg.animation:
        return "gif"
    return None


def normalize_link(key: str, raw: str) -> Optional[str]:
    """Turn user input (@handle or URL) into a safe https URL, or None if invalid."""
    spec = PLATFORMS[key]
    raw = (raw or "").strip()
    if not raw or len(raw) > 200 or any(c.isspace() for c in raw):
        return None
    if "/" in raw and "://" not in raw:
        raw = "https://" + raw
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or "." not in host:
            return None
        if spec["hosts"] is not None and not any(host == h or host.endswith("." + h) for h in spec["hosts"]):
            return None
        return "https://" + raw.split("://", 1)[1]
    if not spec["tpl"]:
        return None
    handle = raw.lstrip("@")
    return spec["tpl"].format(h=handle) if HANDLE_RE.match(handle) else None


async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                  markup: Optional[Markup] = None) -> None:
    """Edit the current message if triggered by a button, otherwise send a new one."""
    query = update.callback_query
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=markup)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            # not editable (too old / media message) -> fall through and send a fresh one
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=markup)


def own_sent_message(user, arg: str):
    """Message row only if THIS user is the one who sent it."""
    if not str(arg).isdigit():
        return None
    row = db.get_message(int(arg))
    if row and row["sender_hash"] == db.sender_hash(user["user_id"]):
        return row
    return None


def own_received_message(user, arg: str):
    """Message row only if THIS user received it (and hasn't removed it from the inbox)."""
    if not str(arg).isdigit():
        return None
    row = db.get_message(int(arg))
    if row and row["receiver_id"] == user["user_id"] and not row["hidden"]:
        return row
    return None


# =============================================================================
# Keyboards & screens
# =============================================================================
def main_menu_kb(user) -> Markup:
    rows = [
        [Btn("📥 Inbox", callback_data="m:inbox"), Btn("🔗 My link", callback_data="m:link")],
        [Btn("🌐 Hub preview", callback_data="m:hub"), Btn("🗂 Manage links", callback_data="m:links")],
        [Btn("➕ Add social link", callback_data="m:addlink"), Btn("⚙️ Settings", callback_data="m:settings")],
        [Btn("📊 Stats", callback_data="m:stats")],
    ]
    rows.append([Btn("💎 Pro plan" if is_pro(user) else "⭐ Upgrade to Pro", callback_data="m:pro")])
    return Markup(rows)


def back_kb() -> Markup:
    return Markup([[Btn("🏠 Menu", callback_data="m:home")]])


async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, user, welcome: bool = False) -> None:
    link = bot_link(context, user)
    greeting = f"👋 Welcome, <b>{esc(user['first_name'])}</b>!\n\n" if welcome else "🏠 <b>Your dashboard</b>\n\n"
    text = (
        f"{greeting}"
        "Your anonymous inbox is ready 🎉\n\n"
        f"🔗 <b>Your personal link</b>\n<code>{link}</code>\n\n"
        "📌 <b>Put this link in your bio</b> — Instagram, TikTok, X, Telegram… "
        "Anyone who taps it can message you, and you'll never see who it was. 🔒\n\n"
        f"Plan: {plan_label(user)}{ad_footer(user)}"
    )
    await respond(update, context, text, main_menu_kb(user))


# ---- link-in-bio hub (with optional Pro media) ---------------------------------
def build_hub(owner) -> tuple[str, list]:
    icon, line, _, _ = theme_of(owner)
    prompt = esc(owner["welcome_text"]) if is_pro(owner) and owner["welcome_text"] else \
        "Send me an anonymous message! 💌\nJust type it below — I'll never know it was you. 🔒"
    text = f"{icon} <b>{esc(owner['first_name'])}</b> {icon}\n"
    if owner["bio"]:
        text += f"<i>{esc(owner['bio'])}</i>\n"
    text += f"\n{line}\n{prompt}\n{line}"
    rows = [[Btn(f"{PLATFORMS[l['platform']]['icon']}  {PLATFORMS[l['platform']]['label']}", url=l["url"])]
            for l in db.list_links(owner["user_id"]) if l["platform"] in PLATFORMS]
    return text, rows


def has_hub_media(owner) -> bool:
    return bool(is_pro(owner) and owner["hub_media_id"] and owner["hub_media_type"])


async def send_hub(context, chat_id: int, owner, tail_rows: list) -> None:
    """Send the hub as a photo/video/GIF with caption (Pro media hub), or as plain text."""
    text, rows = build_hub(owner)
    markup = Markup(rows + tail_rows)
    if has_hub_media(owner):
        senders = {"photo": context.bot.send_photo, "video": context.bot.send_video,
                   "animation": context.bot.send_animation}
        send = senders.get(owner["hub_media_type"])
        if send:
            try:
                await send(chat_id, owner["hub_media_id"], caption=text, reply_markup=markup)
                return
            except TelegramError:
                log.warning("Hub media failed for owner %s — falling back to text", owner["user_id"])
    await context.bot.send_message(chat_id, text, reply_markup=markup)


async def show_hub_to_sender(update: Update, context: ContextTypes.DEFAULT_TYPE, owner) -> None:
    """Someone opened an owner's link: show the hub and wait for their message."""
    db.set_state(update.effective_user.id, "sending", str(owner["user_id"]))
    tail = [[Btn("❌ Cancel", callback_data="m:cancel")]]
    if has_hub_media(owner):
        await send_hub(context, update.effective_chat.id, owner, tail)
    else:
        text, rows = build_hub(owner)
        await respond(update, context, text, Markup(rows + tail))


UPSELL_TEXT = ("⭐ <b>This is a Pro feature</b>\n\n" + PRO_FEATURES)


async def upsell(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = UPSELL_TEXT) -> None:
    await respond(update, context, text, Markup([
        [Btn("💎 See Pro", callback_data="m:pro")],
        [Btn("🏠 Menu", callback_data="m:home")],
    ]))


# ---- the "Message sent!" screen the sender sees --------------------------------
SENT_TEXT = ("✅ <b>Message sent!</b>\n"
             "Now just wait for their reply! 💌\n"
             "🔒 <i>You stay 100% anonymous.</i>")


def sent_kb(owner_token: str, mid: int, sender) -> Markup:
    edit_label = "✏️ Edit" if is_pro(sender) else "🔒 Edit"
    return Markup([
        [Btn("🔁 Send one more", callback_data=f"sm:{owner_token}")],
        [Btn("🎁 Send a gift", callback_data=f"gf:{mid}")],
        [Btn(edit_label, callback_data=f"ed:{mid}"), Btn("🗑 Delete", callback_data=f"dm:{mid}")],
    ])


async def show_sent_screen(update, context, user, mid: int) -> None:
    row = db.get_message(mid)
    owner = db.get_user(row["receiver_id"]) if row else None
    if not owner:
        await respond(update, context, "⚠️ This link is no longer valid.")
        return
    await respond(update, context, SENT_TEXT, sent_kb(owner["token"], mid, user))


def promo_text(link: str, user) -> str:
    """Viral loop shown to FREE senders after sending (Pro users never see it)."""
    return (
        "🚀 <b>Start receiving anonymous messages right now</b>\n\n"
        f"Your link 👇\n<code>{link}</code>\n\n"
        "Add this link ☝️ to your profile bio on Telegram / TikTok / Instagram "
        f"to start receiving anonymous messages 💬{ad_footer(user)}"
    )


def share_url(link: str) -> str:
    return f"https://t.me/share/url?url={quote(link)}&text={quote('Send me an anonymous message 💌')}"


# =============================================================================
# Commands
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = db.upsert_user(user.id, user.username, user.first_name)

    if context.args:  # opened via someone's link: /start TOKEN-or-slug
        owner = resolve_owner(context.args[0])
        if owner and owner["user_id"] != user.id:
            await show_hub_to_sender(update, context, owner)
            return
        if not owner:
            await update.effective_message.reply_text(
                "⚠️ That link is invalid or has been replaced by its owner.")
    db.clear_state(user.id)
    await show_dashboard(update, context, row, welcome=True)


async def _me(update: Update):
    u = update.effective_user
    return db.upsert_user(u.id, u.username, u.first_name)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _me(update)
    db.clear_state(row["user_id"])
    await show_dashboard(update, context, row)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_menu(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "ℹ️ <b>How it works</b>\n\n"
        "1️⃣ /start → get your personal link\n"
        "2️⃣ Put it in your social bios\n"
        "3️⃣ People message you anonymously — you can reply, react, block or report\n"
        "4️⃣ /inbox → browse everything you received\n\n"
        "/menu · /inbox · /link · /links · /settings · /pro · /stats · /terms · /privacy · /deleteme · /cancel",
        reply_markup=back_kb(),
    )


async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(TERMS_TEXT, reply_markup=back_kb())


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(PRIVACY_TEXT, reply_markup=back_kb())


async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_inbox(update, context, await _me(update))


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_link(update, context, await _me(update))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_links(update, context, await _me(update))


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_settings(update, context, await _me(update))


async def cmd_pro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_pro(update, context, await _me(update))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_stats(update, context, await _me(update))


async def cmd_deleteme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_delete_confirm(update, context, await _me(update))


async def cmd_grantpro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /grantpro <user_id> [days]   (no days = lifetime). Handy for testing."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /grantpro <user_id> [days]")
        return
    target = int(context.args[0])
    days = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else None
    if not db.get_user(target):
        await update.effective_message.reply_text("User not found (they must /start the bot first).")
        return
    db.set_pro(target, days)
    await update.effective_message.reply_text(
        f"✅ User {target} is now Pro ({f'{days} days' if days else 'lifetime'}).")


async def _ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE, ban: bool) -> None:
    """Admin only: /ban <message_id> or /unban <message_id> — acts on the message's sender hash."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(f"Usage: /{'ban' if ban else 'unban'} <message_id>")
        return
    row = db.get_message(int(context.args[0]))
    if not row:
        await update.effective_message.reply_text("Message not found.")
        return
    if ban:
        db.ban_sender(row["sender_hash"], f"manual via message #{row['id']}")
    else:
        db.unban_sender(row["sender_hash"])
    await update.effective_message.reply_text(
        f"{'🔨 Sender banned' if ban else '✅ Sender unbanned'} (message #{row['id']}).")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban_command(update, context, ban=True)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban_command(update, context, ban=False)


# =============================================================================
# Screens used by both commands and buttons
# =============================================================================
async def show_link(update, context, user) -> None:
    link = bot_link(context, user)
    custom = is_pro(user) and user["custom_slug"]
    note = ("✨ <i>Custom link active.</i>" if custom else
            "💎 <i>Pro tip: get a custom link like</i> <code>?start=your-name</code>")
    await respond(update, context,
                  f"🔗 <b>Your anonymous link</b>\n\n<code>{link}</code>\n\n"
                  "Paste it in your Instagram / TikTok / X / Telegram bio. "
                  f"Tap the link above to copy it.\n\n{note}",
                  Markup([[Btn("📤 Share link", url=share_url(link))],
                          [Btn("✨ Custom link", callback_data="st:slug")],
                          [Btn("♻️ Generate new link", callback_data="m:newlink")],
                          [Btn("🏠 Menu", callback_data="m:home")]]))


async def show_links(update, context, user) -> None:
    links = db.list_links(user["user_id"])
    limit = "∞" if is_pro(user) else str(config.FREE_LINK_LIMIT)
    text = f"🗂 <b>Your social links</b> ({len(links)}/{limit})\n\n"
    rows = []
    if links:
        text += "Tap ❌ to remove a link."
        for l in links:
            spec = PLATFORMS.get(l["platform"], {"icon": "🔗", "label": l["platform"]})
            rows.append([Btn(f"{spec['icon']} {spec['label']}", url=l["url"]),
                         Btn("❌ Remove", callback_data=f"dl:{l['id']}")])
    else:
        text += "You haven't added any links yet."
    rows.append([Btn("➕ Add link", callback_data="m:addlink")])
    rows.append([Btn("🏠 Menu", callback_data="m:home")])
    await respond(update, context, text, Markup(rows))


async def show_add_link(update, context, user) -> None:
    if not is_pro(user) and db.count_links(user["user_id"]) >= config.FREE_LINK_LIMIT:
        await upsell(update, context,
                     f"🔒 <b>Free plan limit reached</b>\n\nFree accounts can add {config.FREE_LINK_LIMIT} links. "
                     "Go Pro for unlimited links!")
        return
    rows, row = [], []
    for key, spec in PLATFORMS.items():
        locked = spec["pro"] and not is_pro(user)
        row.append(Btn(f"{'🔒' if locked else spec['icon']} {spec['label']}", callback_data=f"al:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("🏠 Menu", callback_data="m:home")])
    await respond(update, context,
                  "➕ <b>Add a social link</b>\n\nChoose a platform. 🔒 = extra icons for Pro users.",
                  Markup(rows))


async def show_settings(update, context, user) -> None:
    lock = "" if is_pro(user) else " 🔒"
    media = {"photo": "🖼 photo", "video": "🎬 video", "animation": "🎞 GIF"}.get(
        user["hub_media_type"] if has_hub_media(user) else None, "— none —")
    slug = f"<code>{esc(user['custom_slug'])}</code>" if user["custom_slug"] else "— none —"
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"📝 Bio: <i>{esc(user['bio']) or '— not set —'}</i>\n"
        f"👋 Welcome text: <i>{esc(user['welcome_text']) or '— default —'}</i>{lock}\n"
        f"🎁 Thank-you sticker: {'✅ set' if user['thank_sticker'] else '— none —'}{lock}\n"
        f"🎨 Theme: {theme_of(user)[2]}{lock}\n"
        f"🔗 Custom link: {slug}{lock}\n"
        f"🎬 Hub media: {media}{lock}"
    )
    await respond(update, context, text, Markup([
        [Btn("📝 Edit bio", callback_data="st:bio")],
        [Btn(f"👋 Welcome text{lock}", callback_data="st:welcome")],
        [Btn(f"🎁 Thank-you sticker{lock}", callback_data="st:sticker")],
        [Btn(f"🎨 Theme{lock}", callback_data="st:themes")],
        [Btn(f"🔗 Custom link{lock}", callback_data="st:slug")],
        [Btn(f"🎬 Hub photo / video / GIF{lock}", callback_data="st:media")],
        [Btn("🗑 Delete my account", callback_data="m:deleteme")],
        [Btn("🏠 Menu", callback_data="m:home")],
    ]))


async def show_stats(update, context, user) -> None:
    s = db.stats(user["user_id"])
    await respond(update, context,
                  "📊 <b>Your stats</b>\n\n"
                  f"📬 Messages received: <b>{s['total']}</b>\n"
                  f"🗓 Last 7 days: <b>{s['week']}</b>\n"
                  f"🎁 Gifts received: <b>{s['gifts']}</b>\n"
                  f"🚫 Blocked senders: <b>{s['blocked']}</b>\n"
                  f"🔗 Links: <b>{db.count_links(user['user_id'])}</b>\n"
                  f"Plan: {plan_label(user)}",
                  back_kb())


async def show_delete_confirm(update, context, user) -> None:
    warn = "\n⚠️ This also removes your <b>Pro</b> status." if is_pro(user) else ""
    await respond(update, context,
                  "🗑 <b>Delete your account?</b>\n\n"
                  "Your profile, social links and received-message records will be erased, and your old "
                  f"link will stop working. This can't be undone.{warn}",
                  Markup([[Btn("✅ Yes, delete everything", callback_data="m:deleteme_yes")],
                          [Btn("↩️ Keep my account", callback_data="m:home")]]))


# =============================================================================
# Inbox / message history
# =============================================================================
async def show_inbox(update, context, user, flt: str = "all", page: int = 0) -> None:
    """Filterable, paginated list of received messages. Content is NOT stored — see 'Show message'."""
    uid = user["user_id"]
    tag_view = bool(TAG_RE.match(flt))
    if tag_view and not is_pro(user):
        await upsell(update, context)
        return
    if not tag_view and flt not in INBOX_FILTERS:
        flt = "all"

    total = db.count_inbox(uid, flt)
    size = config.INBOX_PAGE_SIZE
    pages = max(1, ceil(total / size))
    page = min(max(page, 0), pages - 1)
    items = db.list_inbox(uid, flt, size, page * size)

    rows = []
    if tag_view:
        title = f"👤 Messages from sender <code>#{flt}</code>"
        rows.append([Btn("⬅️ All messages", callback_data="ib:all:0")])
    else:
        counts = db.inbox_counts(uid)
        title = f"📥 <b>Inbox</b> — {INBOX_FILTERS[flt]}"
        chips = [Btn(("• " if k == flt else "") + f"{label} {counts.get(k, 0)}", callback_data=f"ib:{k}:0")
                 for k, label in INBOX_FILTERS.items()]
        rows += [chips[0:3], chips[3:6]]

    for m in items:
        bits = [f"{KIND_ICON.get(m['kind'], '💬')} #{m['id']}"]
        if is_pro(user) and m["sender_tag"] and not tag_view:
            bits.append(m["sender_tag"])
        if m["reaction"]:
            bits.append(m["reaction"])
        if m["replies"]:
            bits.append(f"↩️{m['replies']}")
        bits.append(datetime.fromtimestamp(m["created_at"] or 0, timezone.utc).strftime("%d %b"))
        rows.append([Btn(" · ".join(bits), callback_data=f"io:{m['id']}")])

    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Newer", callback_data=f"ib:{flt}:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("Older ➡️", callback_data=f"ib:{flt}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("🏠 Menu", callback_data="m:home")])

    body = "Tap a message to open it, react, reply, block or report." if items else \
        "Nothing here yet. Share your link to start receiving messages! 💌"
    await respond(update, context, f"{title}  ({total})\n\n{body}\n<i>Page {page + 1}/{pages}</i>"
                  f"{ad_footer(user)}", Markup(rows))


async def show_inbox_item(update, context, user, arg: str) -> None:
    msg = own_received_message(user, arg)
    if not msg:
        await respond(update, context, "⚠️ Message not found.",
                      Markup([[Btn("⬅️ Inbox", callback_data="ib:all:0")]]))
        return
    pro = is_pro(user)
    mid = msg["id"]
    lines = [
        f"📨 <b>Message #{mid}</b>",
        "━━━━━━━━━━━━━━",
        f"Type: {KIND_LABEL.get(msg['kind'], msg['kind'])}",
        f"🗓 {fmt_ts(msg['created_at'])}",
    ]
    if pro and msg["sender_tag"]:
        lines.append(f"👤 Sender tag: <code>#{msg['sender_tag']}</code>")
    lines += [
        f"Reaction: {msg['reaction'] or '—'}",
        f"Replies: {msg['replies']}/{config.MAX_REPLIES_PER_MESSAGE}",
    ]
    if msg["edits"]:
        lines.append(f"✏️ Edited by the sender ×{msg['edits']}")
    lines.append("🔒 <i>The sender's identity is never shown.</i>")

    rows = [
        [Btn("📄 Show message", callback_data=f"is:{mid}")],
        [Btn("↩️ Reply", callback_data=f"rp:{mid}"), Btn("🚫 Block", callback_data=f"bl:{mid}"),
         Btn("🚩 Report", callback_data=f"rt:{mid}")],
        [Btn(e, callback_data=f"rx:{mid}:{i}") for i, e in enumerate(REACTIONS)],
    ]
    if pro and msg["sender_tag"]:
        n = db.count_inbox(user["user_id"], msg["sender_tag"])
        rows.append([Btn(f"👥 Same sender ({n})", callback_data=f"ib:{msg['sender_tag']}:0")])
    rows.append([Btn("🗑 Remove from inbox", callback_data=f"ih:{mid}"),
                 Btn("⬅️ Inbox", callback_data="ib:all:0")])
    await respond(update, context, "\n".join(lines), Markup(rows))


async def show_original(update, context, user, arg: str) -> None:
    """Re-open a message from the receiver's OWN chat (the bot never stored the content)."""
    msg = own_received_message(user, arg)
    if not msg or not msg["card_msg_id"]:
        await context.bot.send_message(user["user_id"], "⚠️ Message not found.")
        return
    try:
        await context.bot.copy_message(user["user_id"], user["user_id"], msg["card_msg_id"],
                                       reply_markup=card_kb(msg["id"]))
        if msg["media_msg_id"]:
            await context.bot.copy_message(user["user_id"], user["user_id"], msg["media_msg_id"])
    except TelegramError:
        await context.bot.send_message(
            user["user_id"],
            "😕 The original was removed from this chat, and the bot doesn't keep message content, "
            "so it can't be shown again.")


# =============================================================================
# Pro plan: monthly subscription (Telegram Stars)
# =============================================================================
def _pay_field(pay, name: str):
    """Read newer SuccessfulPayment fields even if your PTB version exposes them only via api_kwargs."""
    val = getattr(pay, name, None)
    if val is None:
        val = (getattr(pay, "api_kwargs", None) or {}).get(name)
    return val


def _to_ts(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return int(val.timestamp())
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def show_pro(update, context, user) -> None:
    price = config.PRO_PRICE_STARS
    if is_pro(user):
        until = user["pro_until"]
        rows = [[Btn("🏠 Menu", callback_data="m:home")]]
        if until is None:
            status = "♾ <b>Lifetime access</b> — nothing to renew. Thank you! 💛"
        else:
            if user["auto_renew"]:
                status = f"🔁 Auto-renew <b>ON</b> — renews around <b>{fmt_day(until)}</b> ({price} ⭐)."
                rows.insert(0, [Btn("⏸ Turn off auto-renew", callback_data="ar:off")])
            elif user["sub_charge_id"]:
                status = f"⏸ Auto-renew <b>OFF</b> — Pro ends on <b>{fmt_day(until)}</b>."
                rows.insert(0, [Btn("🔁 Turn auto-renew back on", callback_data="ar:on")])
            else:
                status = f"📅 Pro active until <b>{fmt_day(until)}</b> (one-time pass, no auto-renew)."
                rows.insert(0, [Btn(f"🔁 Subscribe monthly · {price} ⭐", callback_data="buy:sub")])
            rows.insert(0, [Btn(f"➕ Add {config.PRO_PERIOD_DAYS} days · {price} ⭐", callback_data="buy:once")])
        await respond(update, context, f"💎 <b>You're Pro!</b>\n\n{status}\n\n{PRO_FEATURES}", Markup(rows))
        return
    await respond(update, context,
                  f"💎 <b>Upgrade to Pro</b> — {price} ⭐ / month\n━━━━━━━━━━━━━━\n{PRO_FEATURES}\n━━━━━━━━━━━━━━\n"
                  "🔁 <b>Monthly subscription</b> renews automatically every 30 days. "
                  "Cancel any time here or in Telegram → Settings → My Stars → Subscriptions.\n"
                  f"📅 Prefer no renewal? Pay once for {config.PRO_PERIOD_DAYS} days.",
                  Markup([[Btn(f"🔁 Subscribe · {price} ⭐/month", callback_data="buy:sub")],
                          [Btn(f"📅 Pay once · {config.PRO_PERIOD_DAYS} days", callback_data="buy:once")],
                          [Btn("🏠 Menu", callback_data="m:home")]]))


async def handle_buy(update, context, user, mode: str) -> None:
    price = config.PRO_PRICE_STARS
    if is_pro(user) and user["pro_until"] is None:           # lifetime: nothing to buy
        await show_pro(update, context, user)
        return
    if mode == "once":
        await context.bot.send_invoice(
            chat_id=user["user_id"],
            title=f"Pro — {config.PRO_PERIOD_DAYS} days",
            description="No ads, edit sent messages, sender tags, custom link, media hub, unlimited links "
                        "and more. One-time payment, no auto-renew.",
            payload=f"pass:{user['user_id']}",
            provider_token="",            # must be empty for Telegram Stars
            currency="XTR",
            prices=[LabeledPrice(f"Pro · {config.PRO_PERIOD_DAYS} days", price)],
        )
        return
    if is_pro(user) and user["auto_renew"]:
        await show_pro(update, context, user)
        return
    # Recurring subscriptions must be created with an invoice LINK (sendInvoice can't do them).
    try:
        link = await context.bot.create_invoice_link(
            title="Pro — Monthly",
            description="Renews every 30 days. No ads, edit sent messages, sender tags, custom link, "
                        "media hub, unlimited links and more.",
            payload=f"sub:{user['user_id']}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("Pro · 1 month", price)],
            api_kwargs={"subscription_period": config.SUBSCRIPTION_PERIOD},
        )
    except TelegramError:
        log.exception("create_invoice_link failed")
        await respond(update, context, "⚠️ Couldn't create the subscription right now. Please try again later.",
                      back_kb())
        return
    await respond(update, context,
                  f"🔁 <b>Pro monthly subscription</b>\n\n{price} ⭐ every 30 days. Tap below to confirm "
                  "the payment in Telegram. You can cancel any time.",
                  Markup([[Btn(f"⭐ Subscribe · {price} ⭐/month", url=link)],
                          [Btn("🏠 Menu", callback_data="m:home")]]))


async def toggle_auto_renew(update, context, user, arg: str) -> None:
    """Cancel / re-enable the Stars subscription via editUserStarSubscription (needs a recent PTB)."""
    cancel = arg == "off"
    fn = getattr(context.bot, "edit_user_star_subscription", None)
    ok = False
    if fn and user["sub_charge_id"]:
        try:
            ok = bool(await fn(user_id=user["user_id"], telegram_payment_charge_id=user["sub_charge_id"],
                               is_canceled=cancel))
        except TelegramError:
            log.exception("editUserStarSubscription failed")
    if ok:
        db.set_auto_renew(user["user_id"], not cancel)
        await show_pro(update, context, db.get_user(user["user_id"]))
    else:
        await respond(update, context,
                      "ℹ️ Please manage your subscription in Telegram → <b>Settings → My Stars → Subscriptions</b>.",
                      Markup([[Btn("💎 Back to Pro", callback_data="m:pro")]]))


async def pro_maintenance(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: warn non-renewing users before expiry, and tell them when Pro ended."""
    ts = int(time.time())
    kb = Markup([[Btn("💎 Renew Pro", callback_data="m:pro")]])
    for u in db.pro_expiring(ts, config.PRO_EXPIRY_WARNING_DAYS * 86400):
        db.set_pro_notice(u["user_id"], 1)
        try:
            await context.bot.send_message(
                u["user_id"],
                f"⏰ <b>Your Pro plan ends on {fmt_day(u['pro_until'])}.</b>\n\nRenew now to keep your custom "
                "link, media hub, sender tags and ad-free experience.", reply_markup=kb)
        except TelegramError:
            pass
    for u in db.pro_expired(ts):
        db.set_pro_notice(u["user_id"], 2)
        try:
            await context.bot.send_message(
                u["user_id"],
                "💔 <b>Your Pro plan has ended.</b>\n\nYour custom link and hub media are paused (your "
                "random link still works). Everything comes back when you renew.", reply_markup=kb)
        except TelegramError:
            pass


# =============================================================================
# Inline-button router
# =============================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    tg_user = update.effective_user
    user = db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name)
    action, _, arg = (query.data or "").partition(":")

    # Reactions answer the query themselves (they show a toast), everything else acks here.
    if action != "rx":
        await query.answer()

    # ---- main menu ----------------------------------------------------------
    if action == "m":
        if arg in ("home", "cancel"):
            db.clear_state(user["user_id"])
            await show_dashboard(update, context, user)
        elif arg == "inbox":
            await show_inbox(update, context, user)
        elif arg == "link":
            await show_link(update, context, user)
        elif arg == "hub":
            if has_hub_media(user):
                await send_hub(context, user["user_id"], user, [[Btn("🏠 Menu", callback_data="m:home")]])
            else:
                text, rows = build_hub(user)
                rows.append([Btn("🏠 Menu", callback_data="m:home")])
                await respond(update, context, "👀 <b>Preview — this is what visitors see:</b>\n\n" + text,
                              Markup(rows))
        elif arg == "links":
            await show_links(update, context, user)
        elif arg == "addlink":
            await show_add_link(update, context, user)
        elif arg == "settings":
            await show_settings(update, context, user)
        elif arg == "stats":
            await show_stats(update, context, user)
        elif arg == "pro":
            await show_pro(update, context, user)
        elif arg == "newlink":
            await respond(update, context,
                          "♻️ <b>Generate a new link?</b>\n\nYour old random link will stop working everywhere "
                          "you posted it. (A Pro custom link is not affected — change it in Settings.)",
                          Markup([[Btn("✅ Yes, replace it", callback_data="m:newlink_yes"),
                                   Btn("↩️ Keep", callback_data="m:link")]]))
        elif arg == "newlink_yes":
            db.regenerate_token(user["user_id"])
            await show_link(update, context, db.get_user(user["user_id"]))
        elif arg == "deleteme":
            await show_delete_confirm(update, context, user)
        elif arg == "deleteme_yes":
            db.delete_account(user["user_id"])
            await respond(update, context,
                          "🗑 <b>Your account has been deleted.</b>\n\nSend /start any time to create a new one.")

    # ---- inbox ----------------------------------------------------------------
    elif action == "ib":
        flt, _, page = arg.partition(":")
        await show_inbox(update, context, user, flt, int(page) if page.isdigit() else 0)

    elif action == "io":
        await show_inbox_item(update, context, user, arg)

    elif action == "is":
        await show_original(update, context, user, arg)

    elif action == "ih":
        msg = own_received_message(user, arg)
        if msg:
            db.hide_message(msg["id"])
        await show_inbox(update, context, user)

    # ---- add / remove social links -----------------------------------------
    elif action == "al":
        spec = PLATFORMS.get(arg)
        if not spec:
            return
        if spec["pro"] and not is_pro(user):
            await upsell(update, context, f"🔒 <b>{spec['label']}</b> is a Pro icon.\n\nUpgrade to unlock all icons.")
            return
        if not is_pro(user) and db.count_links(user["user_id"]) >= config.FREE_LINK_LIMIT:
            await show_add_link(update, context, user)
            return
        db.set_state(user["user_id"], "add_link", arg)
        example = "@username" if spec["tpl"] else "https://…"
        await respond(update, context,
                      f"{spec['icon']} <b>Add {spec['label']}</b>\n\nSend your {'@username or ' if spec['tpl'] else ''}"
                      f"link, e.g. <code>{example}</code>",
                      Markup([[Btn("❌ Cancel", callback_data="m:cancel")]]))

    elif action == "dl":
        if arg.isdigit():
            db.delete_link(int(arg), user["user_id"])
        await show_links(update, context, user)

    # ---- settings -----------------------------------------------------------
    elif action == "st":
        cancel_kb = Markup([[Btn("❌ Cancel", callback_data="m:cancel")]])
        if arg == "bio":
            db.set_state(user["user_id"], "set_bio")
            await respond(update, context, "📝 Send your new bio (max 150 characters). Send <code>-</code> to clear it.",
                          cancel_kb)
        elif not is_pro(user):
            await upsell(update, context)
        elif arg == "welcome":
            db.set_state(user["user_id"], "set_welcome")
            await respond(update, context,
                          "👋 Send the text visitors see on your hub (max 200 characters). "
                          "Send <code>-</code> to restore the default.", cancel_kb)
        elif arg == "sticker":
            db.set_state(user["user_id"], "set_sticker")
            await respond(update, context,
                          "🎁 Send me a <b>sticker</b>. Senders get it as a thank-you after delivering a message.\n"
                          "Send <code>-</code> to remove it.", cancel_kb)
        elif arg == "slug":
            db.set_state(user["user_id"], "set_slug")
            await respond(update, context,
                          "🔗 <b>Custom link</b>\n\nSend the name you want, e.g. <code>john-destiny</code>\n"
                          f"Your link becomes <code>t.me/{context.bot.username}?start=john-destiny</code>\n\n"
                          "3–31 characters: lowercase letters, numbers, <code>-</code> and <code>_</code>. "
                          "Send <code>-</code> to remove it.", cancel_kb)
        elif arg == "media":
            db.set_state(user["user_id"], "set_media")
            await respond(update, context,
                          "🎬 Send a <b>photo, video or GIF</b>. It will be shown on top of your link-in-bio hub.\n"
                          "Send <code>-</code> to remove it.", cancel_kb)
        elif arg == "themes":
            rows = [[Btn(("✅ " if user["theme"] == k else "") + v[2], callback_data=f"th:{k}")]
                    for k, v in THEMES.items()]
            rows.append([Btn("⬅️ Back", callback_data="m:settings")])
            await respond(update, context, "🎨 <b>Choose your message-card theme</b>", Markup(rows))

    elif action == "th":
        if not is_pro(user):
            await upsell(update, context)
        elif arg in THEMES:
            db.set_field(user["user_id"], "theme", arg)
            await show_settings(update, context, db.get_user(user["user_id"]))

    # ---- sender flow --------------------------------------------------------
    elif action == "sn":  # open the hub ("Write back" on a reply)
        owner = db.get_user_by_token(arg)
        if owner and owner["user_id"] != user["user_id"]:
            await show_hub_to_sender(update, context, owner)
        else:
            await query.message.reply_text("⚠️ That link is no longer valid.")

    elif action == "sm":  # "Send one more": stay in `sending` so the next message goes straight through
        owner = db.get_user_by_token(arg)
        if owner and owner["user_id"] != user["user_id"]:
            db.set_state(user["user_id"], "sending", f"{owner['user_id']}:more")
            await context.bot.send_message(
                user["user_id"],
                f"✍️ <b>Type your next message to {esc(owner['first_name'])}</b>\n"
                "<i>Text, photo, video, voice, sticker or GIF — delivered instantly &amp; anonymously.</i>",
                reply_markup=Markup([[Btn("✅ Done", callback_data="m:cancel")]]))
        else:
            await query.message.reply_text("⚠️ That link is no longer valid.")

    elif action == "cf":  # back to the "Message sent!" screen
        row = own_sent_message(user, arg)
        if row:
            await show_sent_screen(update, context, user, row["id"])

    elif action == "dm":
        await handle_delete_message(update, context, user, arg)

    elif action == "ed":
        await handle_edit_start(update, context, user, arg)

    elif action == "gf":
        await show_gift_shop(update, context, user, arg)

    elif action == "gp":
        await send_gift_invoice(update, context, user, arg)

    # ---- receiver actions on a message card ----------------------------------
    elif action in ("rp", "bl", "rx", "rt", "rr"):
        await handle_card_action(update, context, user, action, arg)

    # ---- admin actions on a report -------------------------------------------
    elif action in ("ab", "ad"):
        await handle_admin_report_action(update, context, user, action, arg)

    elif action == "x":  # dismiss a helper prompt
        try:
            await query.message.delete()
        except TelegramError:
            pass

    # ---- Pro purchase / renewal -----------------------------------------------
    elif action == "buy":
        await handle_buy(update, context, user, arg or "sub")   # bare "buy" = old button -> subscription

    elif action == "ar":
        await toggle_auto_renew(update, context, user, arg)


# =============================================================================
# Sender: delete / edit message & gifts
# =============================================================================
async def handle_delete_message(update, context, user, arg: str) -> None:
    """The 🗑 button: remove the message (and media) from the receiver's chat."""
    query = update.callback_query
    row = own_sent_message(user, arg)
    if not row:
        await query.message.reply_text("⚠️ You can only delete messages you sent.")
        return

    failed = False
    if not row["deleted"]:
        for ref in (row["card_msg_id"], row["media_msg_id"]):
            if ref:
                try:
                    await context.bot.delete_message(row["receiver_id"], ref)
                except TelegramError:
                    failed = True   # too old (>48h), or the receiver already removed it
        db.mark_deleted(row["id"])

    owner = db.get_user(row["receiver_id"])
    kb = Markup([[Btn("🔁 Send one more", callback_data=f"sm:{owner['token']}")]]) if owner else None
    note = "\n<i>It may still be visible if it was sent a long time ago.</i>" if failed else ""
    await respond(update, context, f"🗑 <b>Message deleted.</b>{note}", kb)


async def handle_edit_start(update, context, user, arg: str) -> None:
    """✏️ Pro feature: the sender fixes a typo; the receiver's card is edited in place."""
    row = own_sent_message(user, arg)
    if not row:
        return
    if not is_pro(user):
        await upsell(update, context,
                     "✏️ <b>Editing sent messages is a Pro feature</b>\n\n"
                     "Fix typos even after your message was delivered.\n\n" + PRO_FEATURES)
        return
    q = update.callback_query
    if row["kind"] != "text":
        await q.message.reply_text("✏️ Only text messages can be edited. You can 🗑 delete and resend instead.")
        return
    if row["deleted"] or not row["delivered"]:
        await q.message.reply_text("⚠️ This message can't be edited any more.")
        return
    if row["edits"] >= config.MAX_EDITS_PER_MESSAGE:
        await q.message.reply_text(f"ℹ️ You can edit a message up to {config.MAX_EDITS_PER_MESSAGE} times.")
        return
    db.set_state(user["user_id"], "editing", str(row["id"]))
    await q.message.reply_text(
        f"✏️ <b>Editing message #{row['id']}</b>\n\nSend the corrected text. It replaces the old one "
        "in their chat and is marked “edited”.",
        reply_markup=Markup([[Btn("❌ Cancel", callback_data="m:cancel")]]))


async def deliver_edit(update, context, sender, mid: int) -> None:
    msg = update.effective_message
    row = own_sent_message(sender, str(mid))
    if not row or row["kind"] != "text" or row["deleted"] or not is_pro(sender):
        db.clear_state(sender["user_id"])
        await msg.reply_text("⚠️ This message can't be edited.")
        return
    if not msg.text:
        await msg.reply_text("✍️ Please send the corrected text. /cancel to stop.")
        return
    if len(msg.text) > config.MAX_MESSAGE_LEN:
        await msg.reply_text(f"✂️ Too long! Keep it under {config.MAX_MESSAGE_LEN} characters.")
        return
    if row["edits"] >= config.MAX_EDITS_PER_MESSAGE:
        db.clear_state(sender["user_id"])
        await msg.reply_text(f"ℹ️ You can edit a message up to {config.MAX_EDITS_PER_MESSAGE} times.")
        return

    owner = db.get_user(row["receiver_id"])
    # If the receiver blocked this sender meanwhile, pretend success (same shadow-block rule as sending).
    live = owner and row["delivered"] and row["card_msg_id"] and not db.is_blocked(owner["user_id"], row["sender_hash"])
    if live:
        tag = row["sender_tag"] if is_pro(owner) else None
        try:
            await context.bot.edit_message_text(
                build_card(owner, mid, "text", msg.text, tag=tag, edited=True, ts=row["created_at"]),
                chat_id=owner["user_id"], message_id=row["card_msg_id"], reply_markup=card_kb(mid))
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                await msg.reply_text("ℹ️ That's identical to the current text. Send a different version.")
                return
            db.clear_state(sender["user_id"])
            await msg.reply_text("😕 Couldn't edit — they may have deleted the message.")
            return
        except TelegramError:
            db.clear_state(sender["user_id"])
            await msg.reply_text("😕 Couldn't edit this message right now.")
            return
    db.bump_edits(mid)
    db.clear_state(sender["user_id"])
    await msg.reply_text("✅ <b>Message edited!</b>",
                         reply_markup=sent_kb(owner["token"], mid, sender) if owner else None)


def gift_allowed(row) -> bool:
    """Gifts only make sense for a delivered, non-deleted message whose sender isn't blocked."""
    return bool(row) and row["delivered"] == 1 and row["deleted"] == 0 and \
        not db.is_blocked(row["receiver_id"], row["sender_hash"]) and db.get_user(row["receiver_id"]) is not None


async def show_gift_shop(update, context, user, arg: str) -> None:
    row = own_sent_message(user, arg)
    if not row:
        return
    back = [Btn("↩️ Back", callback_data=f"cf:{row['id']}")]
    if not gift_allowed(row):
        await respond(update, context, "🎁 Gifts can't be sent for this message.", Markup([back]))
        return
    gifts = [Btn(f"{emoji} {name} · {stars}⭐", callback_data=f"gp:{row['id']}:{code}")
             for code, (emoji, name, stars) in config.GIFTS.items()]
    rows = [gifts[i:i + 2] for i in range(0, len(gifts), 2)] + [back]
    await respond(update, context,
                  "🎁 <b>Send a gift</b>\n\n"
                  "Surprise them with a virtual gift. It shows up next to your message and "
                  "you stay anonymous. Paid with Telegram Stars ⭐",
                  Markup(rows))


async def send_gift_invoice(update, context, user, arg: str) -> None:
    mid_str, _, code = arg.partition(":")
    row = own_sent_message(user, mid_str)
    if not row or code not in config.GIFTS or not gift_allowed(row):
        await respond(update, context, "🎁 Gifts can't be sent for this message.",
                      Markup([[Btn("↩️ Back", callback_data=f"cf:{mid_str}")]]) if row else None)
        return
    emoji, name, stars = config.GIFTS[code]
    await context.bot.send_invoice(
        chat_id=user["user_id"],
        title=f"{emoji} {name}",
        description="A virtual gift for the person you messaged. They'll see it with your message — "
                    "you stay anonymous. Gifts are digital and non-refundable.",
        payload=f"gift:{row['id']}:{code}:{user['user_id']}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(f"{emoji} {name}", stars)],
    )


# =============================================================================
# Receiver: Reply / Block / Report / React on a message card
# =============================================================================
async def handle_card_action(update, context, user, action: str, arg: str) -> None:
    query = update.callback_query
    mid_str, _, extra = arg.partition(":")
    msg = db.get_message(int(mid_str)) if mid_str.isdigit() else None

    # Security: only the receiver of this message may act on it.
    if not msg or msg["receiver_id"] != user["user_id"]:
        if action == "rx":
            await query.answer("Not available.", show_alert=True)
        return

    if action == "rp":
        if msg["replies"] >= config.MAX_REPLIES_PER_MESSAGE:
            await query.message.reply_text("ℹ️ You've reached the reply limit for this message.")
            return
        db.set_state(user["user_id"], "replying", str(msg["id"]))
        await query.message.reply_text(
            f"↩️ <b>Replying to message #{msg['id']}</b>\n\nType your reply (text only). Your name will be shown.",
            reply_markup=Markup([[Btn("❌ Cancel", callback_data="m:cancel")]]))

    elif action == "bl":
        db.block(user["user_id"], msg["sender_hash"])
        await query.message.reply_text(
            "🚫 <b>Sender blocked.</b>\nThey won't know — their future messages are silently dropped.")

    elif action == "rt":  # step 1 of reporting: ask for a reason
        rows = [[Btn(label, callback_data=f"rr:{msg['id']}:{code}")] for code, label in REPORT_REASONS.items()]
        rows.append([Btn("❌ Cancel", callback_data="x:")])
        await query.message.reply_text(
            "🚩 <b>Report this message</b>\n\nWhy are you reporting it? "
            "The sender will also be blocked for you.",
            reply_markup=Markup(rows))

    elif action == "rr":  # step 2: reason chosen -> store, block, notify admins
        if extra not in REPORT_REASONS:
            return
        report_id = db.add_report(msg["id"], user["user_id"], extra)
        db.block(user["user_id"], msg["sender_hash"])
        if report_id is None:
            await respond(update, context, "ℹ️ You already reported this message. The sender is blocked.")
            return
        await notify_admins(context, report_id, msg, extra)
        await respond(update, context, "✅ <b>Thanks — report sent.</b>\nWe also blocked this sender for you.")

    elif action == "rx":
        if not extra.isdigit() or int(extra) >= len(REACTIONS):
            await query.answer()
            return
        sender_id = db.decrypt_sender(msg["sender_enc"])
        if sender_id is None:
            await query.answer("This sender is no longer reachable.", show_alert=True)
            return
        emoji = REACTIONS[int(extra)]
        if not db.mark_reacted(msg["id"], emoji):
            await query.answer("You already reacted to this message.", show_alert=True)
            return
        try:
            await context.bot.send_message(
                sender_id, f"{emoji} <b>{esc(user['first_name'])}</b> reacted to your anonymous message!")
            await query.answer(f"{emoji} Sent!")
        except TelegramError:
            await query.answer("Couldn't deliver the reaction.", show_alert=True)


async def notify_admins(context, report_id: int, msg, reason_code: str) -> None:
    """
    Send the reported content to every admin with Ban / Dismiss buttons.
    The content is copied from the receiver's own chat (works from the card AND from the inbox).
    """
    if not config.ADMIN_IDS:
        log.warning("Report #%s received but ADMIN_IDS is empty — nobody was notified.", report_id)
        return
    header = (f"🚩 <b>Report #{report_id}</b> · message #{msg['id']} ({msg['kind']})\n"
              f"Reason: <b>{REPORT_REASONS[reason_code]}</b>\n"
              "━━━━━━━━━━━━━━\n👇 Reported content below")
    kb = Markup([[Btn("🔨 Ban sender", callback_data=f"ab:{report_id}"),
                  Btn("✅ Dismiss", callback_data=f"ad:{report_id}")]])
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, header, reply_markup=kb)
            try:
                if msg["card_msg_id"]:
                    await context.bot.copy_message(admin_id, msg["receiver_id"], msg["card_msg_id"])
                if msg["media_msg_id"]:
                    await context.bot.copy_message(admin_id, msg["receiver_id"], msg["media_msg_id"])
            except TelegramError:
                await context.bot.send_message(admin_id, "<i>(content no longer available in the receiver's chat)</i>")
        except TelegramError:
            log.warning("Could not notify admin %s about report #%s", admin_id, report_id)


async def handle_admin_report_action(update, context, user, action: str, arg: str) -> None:
    if not is_admin(user["user_id"]):
        return
    report = db.get_report(int(arg)) if arg.isdigit() else None
    if not report:
        await respond(update, context, "⚠️ Report not found.")
        return
    if action == "ab":
        row = db.get_message(report["message_id"])
        if row:
            db.ban_sender(row["sender_hash"], f"report #{report['id']}")
        db.set_report_status(report["id"], "banned")
        await respond(update, context, f"🔨 Report #{report['id']}: sender banned.")
    else:
        db.set_report_status(report["id"], "dismissed")
        await respond(update, context, f"✅ Report #{report['id']} dismissed.")


# =============================================================================
# Free-form messages (state machine)
# =============================================================================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    msg = update.effective_message
    user = db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name)
    state, data = user["state"], user["state_data"]

    if state == "sending":
        await deliver_anonymous(update, context, user, data)
    elif state == "replying":
        await deliver_reply(update, context, user, int(data))
    elif state == "editing":
        await deliver_edit(update, context, user, int(data))
    elif state == "add_link":
        await save_link(update, context, user, data)
    elif state in ("set_bio", "set_welcome"):
        await save_text_setting(update, context, user, state)
    elif state == "set_sticker":
        await save_sticker(update, context, user)
    elif state == "set_slug":
        await save_slug(update, context, user)
    elif state == "set_media":
        await save_hub_media(update, context, user)
    else:
        await msg.reply_text("👋 Use the menu below, or open someone's link to message them.",
                             reply_markup=main_menu_kb(user))


async def deliver_anonymous(update, context, sender, data: str) -> None:
    """`data` is "<owner_id>" (opened via link) or "<owner_id>:more" (after 'Send one more')."""
    msg = update.effective_message
    owner_id_str, _, flag = (data or "").partition(":")
    more = flag == "more"
    owner = db.get_user(int(owner_id_str)) if owner_id_str.isdigit() else None
    if not owner or owner["user_id"] == sender["user_id"]:
        db.clear_state(sender["user_id"])
        await msg.reply_text("⚠️ This link is no longer valid.")
        return
    owner_id = owner["user_id"]

    kind = kind_of(msg)
    if not kind:
        await msg.reply_text("😕 I can send text, photos, videos, voice notes, stickers and GIFs.")
        return
    if kind == "text" and len(msg.text) > config.MAX_MESSAGE_LEN:
        await msg.reply_text(f"✂️ Too long! Keep it under {config.MAX_MESSAGE_LEN} characters.")
        return

    s_hash = db.sender_hash(sender["user_id"])
    if db.is_banned(s_hash):
        db.clear_state(sender["user_id"])
        await msg.reply_text("🚫 You've been restricted from sending anonymous messages because of reports. "
                             "See /terms.")
        return
    if db.recent_message_count(s_hash, config.RATE_LIMIT_WINDOW) >= config.RATE_LIMIT_COUNT:
        await msg.reply_text("⏳ Slow down a little — try again in a minute.")
        return

    # Shadow-block: a blocked sender still sees "Message sent!" so they can't tell.
    blocked = db.is_blocked(owner_id, s_hash)
    mid = db.add_message(owner_id, sender["user_id"], kind, delivered=not blocked)

    if not blocked:
        tag = db.sender_tag(owner_id, sender["user_id"]) if is_pro(owner) else None
        try:
            card = await context.bot.send_message(
                owner_id, build_card(owner, mid, kind, msg.text if kind == "text" else None, tag=tag),
                reply_markup=card_kb(mid))
            media_id = None
            if kind != "text":
                # copy_message re-sends the content WITHOUT any "forwarded from" info.
                copied = await context.bot.copy_message(owner_id, msg.chat_id, msg.message_id)
                media_id = copied.message_id
            db.set_message_refs(mid, card.message_id, media_id)
        except Forbidden:  # receiver blocked the bot
            db.mark_undelivered(mid)
            db.clear_state(sender["user_id"])
            await msg.reply_text("😕 Sorry, this person can't receive messages right now.")
            return
        except TelegramError:
            log.exception("Delivery failed")
            db.mark_undelivered(mid)
            await msg.reply_text("⚠️ Something went wrong. Please try again.")
            return

    db.clear_state(sender["user_id"])

    # 1) "Message sent!" with Send one more / gift / edit / delete
    await msg.reply_text(SENT_TEXT, reply_markup=sent_kb(owner["token"], mid, sender))

    if more:
        return  # follow-up messages: no promo / sticker again

    # 2) Promo for FREE senders only (Pro = no ads)
    if not is_pro(sender):
        own_link = bot_link(context, sender)
        await context.bot.send_message(
            sender["user_id"], promo_text(own_link, sender),
            reply_markup=Markup([[Btn("📤 Share link", url=share_url(own_link))]]))

    # 3) Pro perk (owner's): thank-you sticker for the sender.
    if not blocked and is_pro(owner) and owner["thank_sticker"]:
        try:
            await context.bot.send_sticker(sender["user_id"], owner["thank_sticker"])
        except TelegramError:
            pass


def build_card(owner, mid: int, kind: str, text: Optional[str], *, tag: Optional[str] = None,
               edited: bool = False, ts=None) -> str:
    """The notification the receiver sees — themed for Pro users, with an optional sender tag."""
    icon, line, _, _ = theme_of(owner)
    body = f"<blockquote>{esc(text)}</blockquote>" if text else f"{KIND_LABEL[kind]} — see below 👇"
    tag_line = f"👤 Sender tag: <code>#{tag}</code>\n" if tag else ""
    return (
        f"{icon} <b>New anonymous message</b> {icon}\n"
        f"{line}\n\n"
        f"{body}\n\n"
        f"{line}\n"
        f"🔒 <i>Sender is 100% anonymous</i>\n"
        f"{tag_line}"
        f"🗓 {fmt_ts(ts)} · <code>#{mid}</code>{' · ✏️ edited' if edited else ''}"
    )


def card_kb(mid: int) -> Markup:
    return Markup([
        [Btn("↩️ Reply", callback_data=f"rp:{mid}"),
         Btn("🚫 Block", callback_data=f"bl:{mid}"),
         Btn("🚩 Report", callback_data=f"rt:{mid}")],
        [Btn(e, callback_data=f"rx:{mid}:{i}") for i, e in enumerate(REACTIONS)],
        [Btn("📥 Inbox", callback_data="ib:all:0")],
    ])


async def deliver_reply(update, context, owner, mid: int) -> None:
    msg = update.effective_message
    original = db.get_message(mid)
    if not original or original["receiver_id"] != owner["user_id"]:
        db.clear_state(owner["user_id"])
        await msg.reply_text("⚠️ Can't reply to that message.")
        return
    if not msg.text:
        await msg.reply_text("✍️ Replies must be text. Please type your reply.")
        return
    if len(msg.text) > config.MAX_MESSAGE_LEN:
        await msg.reply_text(f"✂️ Too long! Keep it under {config.MAX_MESSAGE_LEN} characters.")
        return
    sender_id = db.decrypt_sender(original["sender_enc"])
    if sender_id is None:
        db.clear_state(owner["user_id"])
        await msg.reply_text("😕 This sender is no longer reachable.", reply_markup=back_kb())
        return
    if not db.bump_replies(mid, config.MAX_REPLIES_PER_MESSAGE):
        db.clear_state(owner["user_id"])
        await msg.reply_text("ℹ️ You've reached the reply limit for this message.")
        return

    _, line, _, _ = theme_of(owner)
    try:
        await context.bot.send_message(
            sender_id,
            f"💌 <b>{esc(owner['first_name'])}</b> replied to your anonymous message!\n{line}\n\n"
            f"<blockquote>{esc(msg.text)}</blockquote>\n\n{line}",
            reply_markup=Markup([[Btn("✉️ Write back", callback_data=f"sn:{owner['token']}")]]))
        await msg.reply_text("✅ <b>Reply sent!</b>",
                             reply_markup=Markup([[Btn("📥 Inbox", callback_data="ib:all:0"),
                                                   Btn("🏠 Menu", callback_data="m:home")]]))
    except TelegramError:
        await msg.reply_text("😕 Couldn't deliver your reply — they may have left the bot.", reply_markup=back_kb())
    db.clear_state(owner["user_id"])


async def save_link(update, context, user, platform: str) -> None:
    msg = update.effective_message
    spec = PLATFORMS.get(platform)
    if not spec or not msg.text:
        db.clear_state(user["user_id"])
        return
    if (spec["pro"] and not is_pro(user)) or \
            (not is_pro(user) and db.count_links(user["user_id"]) >= config.FREE_LINK_LIMIT):
        db.clear_state(user["user_id"])
        await upsell(update, context)
        return
    url = normalize_link(platform, msg.text)
    if not url:
        await msg.reply_text(f"❌ That doesn't look like a valid {spec['label']} "
                             f"{'username or ' if spec['tpl'] else ''}link. Try again, or /cancel.")
        return
    db.clear_state(user["user_id"])
    added = db.add_link(user["user_id"], platform, url)
    await msg.reply_text("✅ Link added!" if added else "ℹ️ You already added that link.")
    await show_links(update, context, user)


async def save_text_setting(update, context, user, state: str) -> None:
    msg = update.effective_message
    if not msg.text:
        await msg.reply_text("Please send text. /cancel to stop.")
        return
    field, limit = ("bio", 150) if state == "set_bio" else ("welcome_text", 200)
    if field == "welcome_text" and not is_pro(user):
        db.clear_state(user["user_id"])
        await upsell(update, context)
        return
    text = msg.text.strip()
    if len(text) > limit:
        await msg.reply_text(f"✂️ Too long — max {limit} characters.")
        return
    db.set_field(user["user_id"], field, None if text == "-" else text)
    db.clear_state(user["user_id"])
    await msg.reply_text("✅ Saved!")
    await show_settings(update, context, db.get_user(user["user_id"]))


async def save_sticker(update, context, user) -> None:
    msg = update.effective_message
    if not is_pro(user):
        db.clear_state(user["user_id"])
        await upsell(update, context)
        return
    if msg.sticker:
        db.set_field(user["user_id"], "thank_sticker", msg.sticker.file_id)
    elif msg.text and msg.text.strip() == "-":
        db.set_field(user["user_id"], "thank_sticker", None)
    else:
        await msg.reply_text("Please send a sticker (or <code>-</code> to remove). /cancel to stop.")
        return
    db.clear_state(user["user_id"])
    await msg.reply_text("✅ Saved!")
    await show_settings(update, context, db.get_user(user["user_id"]))


async def save_slug(update, context, user) -> None:
    """Pro: custom link suffix, e.g. t.me/yourbot?start=john-destiny"""
    msg = update.effective_message
    if not is_pro(user):
        db.clear_state(user["user_id"])
        await upsell(update, context)
        return
    if not msg.text:
        await msg.reply_text("Please send the name as text. /cancel to stop.")
        return
    raw = msg.text.strip()
    if raw == "-":
        db.set_slug(user["user_id"], None)
        db.clear_state(user["user_id"])
        await msg.reply_text("✅ Custom link removed.")
        await show_link(update, context, db.get_user(user["user_id"]))
        return
    slug = raw.lower().split("start=")[-1].lstrip("@")      # tolerate pasted full links
    if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
        await msg.reply_text("❌ Use 3–31 characters: lowercase letters, numbers, <code>-</code> or "
                             "<code>_</code> (starting with a letter or number). Try another, or /cancel.")
        return
    if db.set_slug(user["user_id"], slug) != "ok":
        await msg.reply_text("😕 That name is already taken. Try another one.")
        return
    db.clear_state(user["user_id"])
    await msg.reply_text("✅ <b>Custom link saved!</b>")
    await show_link(update, context, db.get_user(user["user_id"]))


async def save_hub_media(update, context, user) -> None:
    """Pro: photo / video / GIF shown on top of the link-in-bio hub."""
    msg = update.effective_message
    if not is_pro(user):
        db.clear_state(user["user_id"])
        await upsell(update, context)
        return
    if msg.text and msg.text.strip() == "-":
        db.set_hub_media(user["user_id"], None, None)
    elif msg.animation:                                     # check GIF first: it also carries a document
        db.set_hub_media(user["user_id"], "animation", msg.animation.file_id)
    elif msg.video:
        db.set_hub_media(user["user_id"], "video", msg.video.file_id)
    elif msg.photo:
        db.set_hub_media(user["user_id"], "photo", msg.photo[-1].file_id)
    else:
        await msg.reply_text("Please send a photo, video or GIF (or <code>-</code> to remove). /cancel to stop.")
        return
    db.clear_state(user["user_id"])
    await msg.reply_text("✅ Hub media saved! Check it with “🌐 Hub preview”.")
    await show_settings(update, context, db.get_user(user["user_id"]))


# =============================================================================
# Telegram Stars payments (Pro subscription / 30-day pass + gifts)
# =============================================================================
def check_payment(q) -> Optional[str]:
    """Validate a pre-checkout query. Returns an error message, or None if OK."""
    if q.currency != "XTR":
        return "Invalid payment request."
    uid = q.from_user.id
    user = db.get_user(uid)
    if not user:
        return "Please send /start to the bot first."

    kind, _, rest = q.invoice_payload.partition(":")
    if kind in ("sub", "pass", "pro"):                     # "pro" = legacy payload from the old one-time plan
        if rest != str(uid):
            return "Invalid payment request."
        if q.total_amount != config.PRO_PRICE_STARS:
            return "The price changed — please try again."
        if kind != "sub" and user["is_pro"] and user["pro_until"] is None:
            return "You already have lifetime Pro! 💎"
        return None

    if kind == "gift":
        parts = rest.split(":")                      # message_id : gift_code : sender_id
        if len(parts) != 3 or not parts[0].isdigit() or parts[2] != str(uid) or parts[1] not in config.GIFTS:
            return "Invalid payment request."
        row = db.get_message(int(parts[0]))
        if not row or row["sender_hash"] != db.sender_hash(uid) or not gift_allowed(row):
            return "This gift can't be sent any more."
        if q.total_amount != config.GIFTS[parts[1]][2]:
            return "The price changed — please try again."
        return None

    return "Invalid payment request."


async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram asks 'may I charge this user?' — we must answer within 10 seconds."""
    q = update.pre_checkout_query
    error = check_payment(q)
    if error:
        await q.answer(ok=False, error_message=error)
    else:
        await q.answer(ok=True)


async def on_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Money has moved — activate/extend Pro or deliver the gift (idempotent via the unique charge id)."""
    pay = update.effective_message.successful_payment
    uid = update.effective_user.id
    kind, _, rest = pay.invoice_payload.partition(":")

    if pay.currency == "XTR" and kind in ("sub", "pass", "pro") and rest == str(uid):
        await handle_pro_payment(update, context, pay, kind)
    elif pay.currency == "XTR" and kind == "gift":
        await handle_gift_payment(update, context, pay, rest)
    else:
        log.warning("Unexpected payment from %s: %s", uid, pay)


async def handle_pro_payment(update, context, pay, kind: str) -> None:
    uid = update.effective_user.id
    subscription = kind == "sub"
    until = None
    if subscription:
        # Telegram tells us when the paid period ends; fall back to now + 30 days.
        until = _to_ts(_pay_field(pay, "subscription_expiration_date")) or \
            int(time.time()) + config.SUBSCRIPTION_PERIOD
    is_renewal = bool(_pay_field(pay, "is_recurring")) and not _pay_field(pay, "is_first_recurring")

    new = db.record_pro_payment(uid, pay.telegram_payment_charge_id, pay.total_amount, pay.invoice_payload,
                                until=until, days=config.PRO_PERIOD_DAYS, subscription=subscription)
    if not new:
        return  # duplicate delivery of the same payment
    user = db.get_user(uid)
    log.info("Pro %s for %s until %s (charge %s)", "renewed" if is_renewal else "activated", uid,
             user["pro_until"], pay.telegram_payment_charge_id)

    if is_renewal:
        text = f"🔁 <b>Pro renewed!</b> 💎\n\nActive until <b>{fmt_day(user['pro_until'])}</b>. Thank you 💛"
    else:
        extra = ("\n🔁 It renews automatically every 30 days — cancel any time in /pro." if subscription
                 else f"\n📅 Active until <b>{fmt_day(user['pro_until'])}</b>.")
        text = f"🎉 <b>Welcome to Pro!</b> 💎\n\nAll Pro features are now unlocked.{extra}"
    await update.effective_message.reply_text(text, reply_markup=main_menu_kb(user))


async def handle_gift_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, pay, rest: str) -> None:
    uid = update.effective_user.id
    parts = rest.split(":")
    if len(parts) != 3 or not parts[0].isdigit() or parts[1] not in config.GIFTS or parts[2] != str(uid):
        log.error("Malformed gift payment from %s: %s", uid, pay)
        return
    mid, code = int(parts[0]), parts[1]
    row = db.get_message(mid)
    if not row:
        log.error("Gift paid for missing message #%s (charge %s)", mid, pay.telegram_payment_charge_id)
        return
    emoji, name, _ = config.GIFTS[code]

    new = db.record_gift(uid, pay.telegram_payment_charge_id, pay.total_amount, pay.invoice_payload,
                         mid, row["receiver_id"], code)
    if not new:
        return  # duplicate delivery of the same payment

    try:
        await context.bot.send_message(
            row["receiver_id"],
            f"🎁 <b>You received a gift!</b>\n\n{emoji} <b>{name}</b> — sent anonymously with "
            f"message <code>#{mid}</code> 💕")
    except TelegramError:
        log.warning("Could not show gift %s to receiver %s", code, row["receiver_id"])

    owner = db.get_user(row["receiver_id"])
    kb = Markup([[Btn("🔁 Send one more", callback_data=f"sm:{owner['token']}")]]) if owner else None
    await update.effective_message.reply_text(
        f"🎉 <b>Gift sent!</b>\n{emoji} {name} was delivered — you stay anonymous. 🔒", reply_markup=kb)


# =============================================================================
# App wiring
# =============================================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Get your anonymous link"),
        BotCommand("menu", "Open the dashboard"),
        BotCommand("inbox", "My received messages"),
        BotCommand("link", "Show my link"),
        BotCommand("links", "Manage social links"),
        BotCommand("settings", "Bio, theme & more"),
        BotCommand("pro", "Pro plan ⭐"),
        BotCommand("stats", "My stats"),
        BotCommand("terms", "Rules of use"),
        BotCommand("privacy", "How your data is handled"),
        BotCommand("deleteme", "Delete my account"),
        BotCommand("cancel", "Cancel current action"),
        BotCommand("help", "How it works"),
    ])
    try:  # the text people see before pressing "Start"
        await app.bot.set_my_description(
            "📩 Receive anonymous messages on Telegram!\n\n"
            "Get your personal link, put it in your Instagram / TikTok / Telegram bio and let people "
            "message you — you'll never see who. Add your social links and build your own link-in-bio hub. 🔗")
        await app.bot.set_my_short_description("Anonymous messages + your own link-in-bio hub 💌")
    except TelegramError:
        log.warning("Could not set the bot description")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: abuse reports will not reach anyone. Set it in .env.")
    if app.job_queue:   # Pro expiry reminders (needs: pip install "python-telegram-bot[job-queue]")
        app.job_queue.run_repeating(pro_maintenance, interval=6 * 3600, first=30)
    else:
        log.warning("JobQueue unavailable — Pro expiry reminders disabled. Install python-telegram-bot[job-queue].")
    log.info("Bot ready: @%s", app.bot.username)


async def post_shutdown(app: Application) -> None:
    """Railway sends SIGTERM on every redeploy: close DB connections cleanly. Data stays in Supabase."""
    db.close()
    log.info("Database pool closed")


def main() -> None:
    global db
    config.validate()
    db = Database(config.DATABASE_URL, config.SECRET_KEY)   # Supabase PostgreSQL

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML,
                           link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    private = filters.ChatType.PRIVATE
    for name, fn in [("start", cmd_start), ("menu", cmd_menu), ("cancel", cmd_cancel), ("help", cmd_help),
                     ("inbox", cmd_inbox), ("link", cmd_link), ("links", cmd_links),
                     ("settings", cmd_settings), ("pro", cmd_pro), ("stats", cmd_stats),
                     ("terms", cmd_terms), ("privacy", cmd_privacy), ("deleteme", cmd_deleteme),
                     ("grantpro", cmd_grantpro), ("ban", cmd_ban), ("unban", cmd_unban)]:
        app.add_handler(CommandHandler(name, fn, filters=private))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_payment))
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT, on_message))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
