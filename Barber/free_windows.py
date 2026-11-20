"""
"⚡ Вільно сьогодні": today's free times across all barbers, booked in one tap.

- Menu button (menu.ROUTES): for each barber their FREE_WINDOWS_SERVICE ("Чоловіча стрижка" by
  default; barbers without it are skipped), the next PER_STAFF free starts from Altegio's online
  booking (/book_times), at least 30 min from now, sorted by time. From EVENING_HOUR on, or when
  today is full, tomorrow is shown instead.
- Each time is a button bk:fw:{loc}:{staff}:{service}:{YYYYmmddHHMM} -> booking.start_free_window
  (the slot is re-checked, then straight to phone / confirmation).
- "🔔 Надсилати вільні вікна щоранку": opt-in subscription (hash bb:fwsubs). run_morning (inside
  the 10-minute cron) sends today's windows once a day between MORNING_HOURS, only when there are
  any; a chat that blocked the bot is unsubscribed.
- Each barber's matching service is cached for 6 hours (bb:fwsvc:{loc}:{staff}).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

import texts as T
from altegio import AltegioError

logger = logging.getLogger(__name__)

SERVICE_TITLE = os.getenv("FREE_WINDOWS_SERVICE", "Чоловіча стрижка")
PER_STAFF = 3
MAX_SLOTS = 15
LEAD = timedelta(minutes=30)
EVENING_HOUR = 19           # from 19:00 the screen shows tomorrow
MORNING_HOURS = (10, 11)    # [from, to): the morning message
SUBS = "bb:fwsubs"
SVC_TTL = 6 * 3600


def _norm(title: str) -> str:
    return " ".join((title or "").lower().split())


async def _service_for(client, links, loc: int, sid: int) -> Optional[dict]:
    """This barber's FREE_WINDOWS_SERVICE as {"id", "title"}; None when they don't offer it."""
    key = "bb:fwsvc:%d:%d" % (loc, sid)
    cached = (await links._pipe(["GET", key]))[0]
    if cached:
        return None if cached == "-" else json.loads(cached)
    svc = next((s for s in await client.list_services(loc, sid) if _norm(s.title) == _norm(SERVICE_TITLE)), None)
    value = {"id": svc.id, "title": svc.title} if svc else None
    await links._pipe(["SET", key, json.dumps(value, ensure_ascii=False) if value else "-", "EX", SVC_TTL])
    return value


async def collect(client, links, locations: Dict[int, str], day: date, now: datetime) -> List[dict]:
    """Free starts on `day`: [{"start", "loc", "staff", "name", "service"}], earliest first."""
    out = []
    for loc in locations:
        for staff in await client.list_staff(loc):
            svc = await _service_for(client, links, loc, staff.id)
            if svc is None:
                continue
            try:
                starts = await client.booking_times(loc, staff.id, day, [svc["id"]])
            except AltegioError as e:
                logger.warning("free windows: %s/%s: %s", loc, staff.id, e)
                continue
            starts = [s for s in starts if s >= now + LEAD][:PER_STAFF]
            out += [{"start": s, "loc": loc, "staff": staff.id, "name": staff.name, "service": svc["id"]}
                    for s in starts]
    out.sort(key=lambda x: (x["start"], x["name"]))
    return out[:MAX_SLOTS]


def slot_rows(slots: List[dict]) -> list:
    buttons = [InlineKeyboardButton(
        "%s · %s" % (x["start"].strftime("%H:%M"), (x["name"] or "").split()[0] if x["name"] else ""),
        callback_data="bk:fw:%d:%d:%d:%s" % (x["loc"], x["staff"], x["service"], x["start"].strftime("%Y%m%d%H%M")))
        for x in slots]
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


async def _subscribed(links, chat_id: int) -> bool:
    return bool((await links._pipe(["HMGET", SUBS, str(chat_id)]))[0][0])


def _markup(slots, subscribed: bool) -> InlineKeyboardMarkup:
    sub = InlineKeyboardButton(T.FREE_UNSUB, callback_data="fw:unsub") if subscribed \
        else InlineKeyboardButton(T.FREE_SUB, callback_data="fw:sub")
    return InlineKeyboardMarkup(slot_rows(slots) + [[InlineKeyboardButton(T.FREE_OTHER, callback_data="bk:new")], [sub]])


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The "⚡ Вільно сьогодні" menu button."""
    bd = context.application.bot_data
    client, links, locations = bd.get("altegio"), bd.get("links"), bd.get("locations") or {}
    chat = update.effective_chat
    if client is None or not locations:
        await chat.send_message(T.UNAVAILABLE, reply_markup=bd.get("main_menu"))
        return
    await chat.send_action(ChatAction.TYPING)
    now = datetime.now(client.tz)
    today = now.date()
    try:
        day = today if now.hour < EVENING_HOUR else today + timedelta(days=1)
        slots = await collect(client, links, locations, day, now)
        if not slots and day == today:
            day = today + timedelta(days=1)
            slots = await collect(client, links, locations, day, now)
    except AltegioError as e:
        logger.warning("free windows failed: %s", e)
        await chat.send_message(T.API_ERROR, reply_markup=bd.get("main_menu"))
        return
    subscribed = await _subscribed(links, chat.id)
    if not slots:
        await chat.send_message(T.FREE_NONE.format(service=SERVICE_TITLE), reply_markup=_markup([], subscribed))
        return
    head = T.FREE_TODAY if day == today else T.FREE_TOMORROW
    await chat.send_message(head.format(service=SERVICE_TITLE) + "\n" + T.FREE_HINT, reply_markup=_markup(slots, subscribed))


async def on_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """fw:sub / fw:unsub — the morning message on or off for this chat."""
    q = update.callback_query
    links = context.application.bot_data.get("links")
    on = q.data == "fw:sub"
    chat = str(update.effective_chat.id)
    if links is not None:
        await links._pipe(["HSET", SUBS, chat, "1"] if on else ["HDEL", SUBS, chat])
    await q.answer(T.FREE_SUB_DONE if on else T.FREE_UNSUB_DONE, show_alert=on)
    markup = getattr(q.message, "reply_markup", None)
    if markup is not None:  # flip the toggle in place, keep the time buttons
        rows = [list(r) for r in markup.inline_keyboard]
        if rows and rows[-1] and rows[-1][0].callback_data in ("fw:sub", "fw:unsub"):
            rows[-1] = [InlineKeyboardButton(T.FREE_UNSUB, callback_data="fw:unsub") if on
                        else InlineKeyboardButton(T.FREE_SUB, callback_data="fw:sub")]
            try:
                await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
            except BadRequest:
                pass


async def run_morning(client, links, locations: Dict[int, str], bot, now: Optional[datetime] = None,
                      force: bool = False) -> int:
    """Once a day between MORNING_HOURS: today's windows to the subscribers. Returns how many got it."""
    now = now or datetime.now(client.tz)
    if not force:
        if not (MORNING_HOURS[0] <= now.hour < MORNING_HOURS[1]):
            return 0
        if (await links._pipe(["SET", "bb:fwday:%s" % now.date().isoformat(), "1", "EX", 2 * 86400, "NX"]))[0] is None:
            return 0
    raw = (await links._pipe(["HGETALL", SUBS]))[0] or []
    subs = [int(raw[i]) for i in range(0, len(raw), 2) if str(raw[i]).lstrip("-").isdigit()]
    if not subs:
        return 0
    slots = await collect(client, links, locations, now.date(), now)
    if not slots:
        return 0
    text = T.FREE_MORNING.format(service=SERVICE_TITLE) + "\n" + T.FREE_HINT
    sent = 0
    for chat in subs:
        try:
            await bot.send_message(chat_id=chat, text=text, reply_markup=_markup(slots, True))
            sent += 1
        except Forbidden:
            await links._pipe(["HDEL", SUBS, str(chat)])  # blocked the bot: stop
        except TelegramError as e:
            logger.warning("free windows morning to %s failed: %s", chat, e)
    return sent


def handlers():
    return [CallbackQueryHandler(on_sub, pattern=r"^fw:(sub|unsub)$")]
