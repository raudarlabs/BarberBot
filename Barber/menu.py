"""
Main menu: the bottom reply keyboard and the screens behind its buttons.

    📅 Записатися        -> booking.py (entry point)
    👤 Мій запис         -> upcoming visits with "✅ Буду" / "🔁 Перенести"
    ✂️ Підібрати стрижку -> asks for a selfie (the photo itself is handled in bot.py)
    🎮 Ігри              -> inline web_app buttons into the arcade (hub + one per game)
    💈 Послуги й ціни    -> barber -> their services and prices -> "Записатися до ..."
    📍 Контакти          -> address, hours, phone, map

The arcade is always opened from INLINE (or chat-menu) buttons: Mini Apps launched
from a reply-keyboard button get no initData, and the leaderboard needs it.

Needs in application.bot_data (besides booking's): "arcade_url", "shop" (config.SHOP).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

import texts as T
from altegio import AltegioError, normalize_phone
from reminders import move_confirm_buttons

logger = logging.getLogger(__name__)

# id -> (emoji, title); ids match public/arcade/games/*.js and the ?game= deep link.
GAMES = [
    ("dryer", "💨", "Фен-пілот"),
    ("fade", "💈", "Ідеальний фейд"),
    ("beard", "🥷", "Борода-ніндзя"),
    ("snake", "🐍", "Змійка"),
    ("blocks", "🧩", "Укладка"),
]
VISITS_DAYS = 30
MAX_VISITS = 5


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(T.BOOK_BUTTON), KeyboardButton(T.MENU_VISITS)],
            [KeyboardButton(T.MENU_STYLE), KeyboardButton(T.MENU_GAMES)],
            [KeyboardButton(T.MENU_PRICES), KeyboardButton(T.MENU_CONTACTS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=T.MENU_PLACEHOLDER,
    )


def chat_menu_button(mode: str, arcade_url: str, booking_url: str, booking_text: str):
    """The button left of the message field: Altegio booking page, the arcade, or None (leave as is)."""
    if mode == "arcade":
        return MenuButtonWebApp(text=T.MENU_GAMES, web_app=WebAppInfo(url=arcade_url))
    if mode == "booking" and booking_url:
        return MenuButtonWebApp(text=booking_text, web_app=WebAppInfo(url=booking_url))
    return None


def game_url(base: str, game_id: str = "") -> str:
    """https://x.app/ + 'snake' -> https://x.app/?game=snake (keeps any existing query)."""
    if not game_id:
        return base
    parts = urlsplit(base)
    query = (parts.query + "&" if parts.query else "") + "game=" + game_id
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, parts.fragment))


def games_markup(base: str) -> InlineKeyboardMarkup:
    hub = [InlineKeyboardButton(T.GAMES_HUB_BUTTON, web_app=WebAppInfo(url=base))]
    games = [InlineKeyboardButton("%s %s" % (emoji, title), web_app=WebAppInfo(url=game_url(base, gid)))
             for gid, emoji, title in GAMES]
    return InlineKeyboardMarkup([hub] + [games[i:i + 2] for i in range(0, len(games), 2)])


def _bd(context):
    return context.application.bot_data


def _menu(context):
    return _bd(context).get("main_menu")


async def _reply(update: Update, text: str, markup=None):
    await update.effective_chat.send_message(text, reply_markup=markup)


# ------------------------------------------------------------------ simple screens

async def show_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, T.GAMES_TEXT, games_markup(_bd(context)["arcade_url"]))


async def show_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, T.STYLE_PROMPT, _menu(context))


async def show_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shop = _bd(context)["shop"]
    rows = [[InlineKeyboardButton(T.CONTACTS_MAP, url=shop["maps"])]]
    if shop.get("instagram"):
        rows.append([InlineKeyboardButton(T.CONTACTS_INSTAGRAM, url=shop["instagram"])])
    text = T.CONTACTS.format(address=shop["address"], hours=shop["hours"], phone=shop["phone_display"])
    await _reply(update, text, InlineKeyboardMarkup(rows))


# ------------------------------------------------------------------ prices

async def show_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = _bd(context)
    client, locations = bd.get("altegio"), bd.get("locations") or {}
    if client is None or not locations:
        await _reply(update, T.PRICES_UNAVAILABLE.format(phone=bd["shop"]["phone_display"]), _menu(context))
        return
    if len(locations) > 1:
        rows = [[InlineKeyboardButton("📍 " + name, callback_data="pr:loc:%d" % loc)]
                for loc, name in locations.items()]
        await _reply(update, T.PRICES_PICK_LOCATION, InlineKeyboardMarkup(rows))
        return
    await _prices_staff(update, context, next(iter(locations)))


async def _prices_staff(update, context, loc: int):
    client = _bd(context)["altegio"]
    try:
        staff = await client.list_staff(loc)
    except AltegioError as e:
        logger.warning("prices: staff failed: %s", e)
        await _edit_or_send(update, T.API_ERROR)
        return
    buttons = [InlineKeyboardButton("💈 " + s.name, callback_data="pr:stf:%d:%d" % (loc, s.id)) for s in staff]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await _edit_or_send(update, T.PRICES_PICK_STAFF if staff else T.NO_STAFF, InlineKeyboardMarkup(rows))


async def _edit_or_send(update, text, markup=None):
    q = update.callback_query
    if q is not None:
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await update.effective_chat.send_message(text, reply_markup=markup)


async def on_prices_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """pr:loc:{loc} -> barbers; pr:stf:{loc}:{staff} -> that barber's services and prices."""
    bd = _bd(context)
    client, locations = bd.get("altegio"), bd.get("locations") or {}
    parts = update.callback_query.data.split(":")
    try:
        loc = int(parts[2])
        staff_id = int(parts[3]) if parts[1] == "stf" else None
    except (IndexError, ValueError):
        await update.callback_query.answer()
        return
    if client is None or loc not in locations:
        await update.callback_query.answer(T.SESSION_EXPIRED, show_alert=True)
        return
    if staff_id is None:
        await _prices_staff(update, context, loc)
        return
    try:
        staff = {s.id: s.name for s in await client.list_staff(loc)}
        services = await client.list_services(loc, staff_id)
    except AltegioError as e:
        logger.warning("prices: services failed: %s", e)
        await _edit_or_send(update, T.API_ERROR)
        return
    name = staff.get(staff_id, "")
    if not services:
        text = T.NO_SERVICES.format(staff=name)
    else:
        lines = "\n".join(T.PRICES_LINE.format(title=s.title, price=" — " + s.price_label if s.price_label else "")
                          for s in services)
        text = T.PRICES_OF.format(staff=name, lines=lines)
    rows = []
    if services:
        rows.append([InlineKeyboardButton(T.PRICES_BOOK.format(staff=name.split()[0] if name else ""),
                                          callback_data="bk:go:%d:%d" % (loc, staff_id))])
    rows.append([InlineKeyboardButton(T.PRICES_BACK, callback_data="pr:loc:%d" % loc)])
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


# ------------------------------------------------------------------ my visits

async def show_visits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = _bd(context)
    client, links, locations = bd.get("altegio"), bd.get("links"), bd.get("locations") or {}
    chat_id = update.effective_chat.id
    if client is None or not locations:
        await _reply(update, T.UNAVAILABLE, _menu(context))
        return
    phone = await links.phone_for_chat(chat_id)
    if not phone:
        context.user_data["after_contact"] = "visits"
        kb = ReplyKeyboardMarkup([[KeyboardButton(T.SHARE_PHONE_BUTTON, request_contact=True)]],
                                 resize_keyboard=True, one_time_keyboard=True)
        await _reply(update, T.VISITS_ASK_PHONE, kb)
        return

    now = datetime.now(client.tz)
    visits = []
    for loc, address in locations.items():
        cid = await links.client_id_for(loc, phone)
        try:
            appts = await client.list_appointments(loc, now, now + timedelta(days=VISITS_DAYS), client_id=cid)
        except AltegioError as e:
            logger.warning("visits: list failed for %s: %s", loc, e)
            await _reply(update, T.API_ERROR, _menu(context))
            return
        staff_names = None
        for a in appts:
            if not a.active or a.start <= now:
                continue
            mine = (cid and a.client_id == cid) or (a.client_phone and a.client_phone == phone) \
                or await links.chat_for_appointment(loc, a.id) == chat_id
            if not mine:
                continue
            await links.remember_appointment(loc, a.id, chat_id)  # lets its buttons work here
            if a.client_id and not cid:
                await links.remember_client_id(loc, phone, a.client_id)
            if not a.staff_name:
                if staff_names is None:
                    try:
                        staff_names = {s.id: s.name for s in await client.list_staff(loc)}
                    except AltegioError:
                        staff_names = {}
                a.staff_name = staff_names.get(a.staff_id, "")
            visits.append((a, address))

    if not visits:
        await _reply(update, T.VISITS_NONE,
                     InlineKeyboardMarkup([[InlineKeyboardButton(T.VISITS_BOOK, callback_data="bk:new")]]))
        return
    visits.sort(key=lambda v: v[0].start)
    await _reply(update, T.VISITS_HEADER, _menu(context))
    for a, address in visits[:MAX_VISITS]:
        service = " · " + a.service_titles if a.service_titles else ""
        text = T.VISIT.format(staff=a.staff_name, service=service, when=T.when(a.start), location=address)
        await _reply(update, text, move_confirm_buttons(a))


async def on_contact_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A contact shared outside the booking flow (e.g. from "Мій запис")."""
    contact = update.message.contact
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(T.NOT_YOUR_CONTACT)
        return
    links = _bd(context).get("links")
    if links is None:
        return
    await links.link(update.effective_chat.id, normalize_phone(contact.phone_number))
    await update.message.reply_text(T.PHONE_SAVED, reply_markup=_menu(context))
    if context.user_data.pop("after_contact", None) == "visits":
        await show_visits(update, context)


# ------------------------------------------------------------------ wiring

ROUTES = {
    T.MENU_VISITS: show_visits,
    T.MENU_STYLE: show_style,
    T.LEGACY_STYLE: show_style,
    T.MENU_GAMES: show_games,
    T.LEGACY_GAMES: show_games,
    T.LEGACY_STYLE_UK: show_style,
    T.LEGACY_GAMES_UK: show_games,
    T.MENU_PRICES: show_prices,
    T.MENU_CONTACTS: show_contacts,
}


async def dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a menu button's text to its screen (also used when a button interrupts booking)."""
    label = (update.message.text or "").strip() if update.message else ""
    fn = ROUTES.get(label)
    if fn is not None:
        await fn(update, context)
        if label in (T.LEGACY_GAMES, T.LEGACY_GAMES_UK):
            # An old keyboard is still on this phone: the games reply carries only inline buttons,
            # so send the new bottom menu to replace it.
            await _reply(update, T.MENU_UPDATED, _menu(context))


def handlers():
    return [
        MessageHandler(filters.Regex(T.MENU_REGEX), dispatch),
        CallbackQueryHandler(on_prices_button, pattern=r"^pr:(loc:\d+|stf:\d+:\d+)$"),
        MessageHandler(filters.CONTACT, on_contact_link),
    ]
